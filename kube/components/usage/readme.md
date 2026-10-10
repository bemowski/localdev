# usage

Tracks how people use internal tools. **Clients** built into tools send **events** to a small
**write-only HTTP API**, a **server** stores them in Postgres, and Grafana dashboards aggregate them.
The first tool tracked is localdev itself: clusters created and destroyed, plus ad-hoc events
attached to a cluster (app deployed, test suite run, ...).

This folder is self-contained, with its own `.mise.toml`. `client/` and `server/` are independent and
coupled only by `openapi.yaml`. It lives in `kube/components/usage/`, outside localdev, and is plugged
into a localdev cluster only while you work on it (see Server → deploy).

```
openapi.yaml       the contract (OpenAPI 3.1)
examples/          request bodies: *.json valid, invalid/*.json must be rejected
scripts/           check_examples.py (validates examples against the spec)
redocly.yaml       lint rules
server/            ingest server, a complete component: manifests/, docker/, build.sh, own .mise.toml
cluster-agent/     in-cluster event emitter: stdlib Python pod in kube-system (infra component)
client/            host-side CLI + local spool, stdlib Python   (later)
```

## Tasks

```bash
mise run api:lint     # Redocly lint
mise run api:check    # examples/ and the spec's inline examples vs. the schemas
mise run api:docs     # build/api.html
mise run api:test     # lint + check
```

`mise install` provides node (for Redocly) and uv (for the checker).

## API at a glance

| method | path | |
|---|---|---|
| `POST` | `/v1/events` | batch of 1–500 events → `202 {accepted, duplicates}`; 400 / 413 / 422 / 503 |
| `GET` | `/healthz` | `{status: ok \| unavailable}` |
| `GET` | `/openapi.yaml` | the spec |

Base URL: `http://localhost/usage/api` (desktop) or `http://usage.usage.svc` (in the cluster).
Clients read it from `USAGE_URL`.

- **Write-only.** Reporting reads Postgres directly.
- **Batches are all-or-nothing.** One invalid event rejects the batch (422, with per-event `index`, `path` and `message`).
- **Idempotent.** `event_id` (a client-generated uuid) is the key, so resending is safe and is counted as `duplicates`.
- **No auth yet.** Internal only.

Every event has the envelope `event_id, type, occurred_at, username, source{name,version}, data`.
`type` picks the `data` schema:

| type | data |
|---|---|
| `cluster.created` | `cluster_id, cluster_type (localdev/ephemeral/durable), cluster_role (dev/test/stage/prod/perftest), platform (k3d, eks, ...), kubernetes_version`; optional `platform_version, arch`, and for localdev `profile, components[], os, os_variant, os_version` |
| `cluster.heartbeat` | `cluster_id, nodes, cores, memory_bytes, pods, namespaces` (capacity summed over nodes) |
| `cluster.event` | `cluster_id, name (e.g. tests.run), outcome? (success/failure/cancelled), duration_ms?, attributes? (flat, ≤50 keys)` |
| `cluster.destroyed` | `cluster_id` |

Cluster facts are sent once, in `cluster.created`. The other cluster events only carry `cluster_id`.

## Server (`server/`)

FastAPI + psycopg. Requests are validated against `openapi.yaml` itself (jsonschema), so the server
can't drift from the spec. Each batch is written in one transaction:
- **`events`** gets every accepted event as received (jsonb `data`). It's the source of truth.
- **Typed projections** are filled by handlers in `store.py`, normalized on `cluster_id`:
  - `clusters`: one row per cluster, with type, role, platform, versions, the localdev facts,
    `created_at`, `destroyed_at` and `last_seen_at` (the latest event of any kind). It comes out
    right whatever order events arrive in.
  - `cluster_events`: ad-hoc events (`app.deployed`, `tests.run`, ...).
  - `cluster_heartbeats`: one row per heartbeat, with the size stats.
- Duplicates are skipped before projection, so retries never double-count.

| | |
|---|---|
| deploy | only while working on it, via your gitignored `kube/localdev/local.toml` (below), then `mise run cluster:profile usage`. `mise run build usage` rebuilds |
| URLs | `http://localhost/usage/api` (public Ingress, prefix stripped), `http://usage.usage.svc` |
| database | `usage` on `pg/postgres`. The init container creates roles `usage` (owner) and `usage_ro` (read-only, Grafana) from the secret store (`server/secrets.defaults.toml`). The server runs `usage_server/migrations/*.sql` at startup |
| Grafana | datasource **Usage** (uid `usage-pg`, read-only), provisioned through an ExternalSecret in `monitoring` |
| metrics | `usage_events_total{type,result}`, `usage_batches_total{status}` on port `metrics` (9090) |
| logs | logfmt, one line per batch |

```bash
cd server
mise run test     # validation tests + API tests on a throwaway DB (localdev Postgres; skipped if absent)
mise run dev      # run locally on :8080 against localdev's `usage` database
mise run image    # docker build (standalone: IMAGE=usage-server:dev)
```

`server/` layout: `manifests/` (applied by k3s), `docker/` (Dockerfile, requirements, `usage_server/`
code and migrations), `build.sh` (stages `docker/` + `../openapi.yaml` as the build context),
`secrets.defaults.toml` (secret-store entries), `tests/`.

**Plugging it into localdev.** Add to `kube/localdev/local.toml` (gitignored, so only clusters you
create while working on usage get it):

```toml
[components.usage]
path = "../components/usage/server"
requires = ["postgres"]
layer = "apps"

[components.usage-agent]
path = "../components/usage/cluster-agent"
layer = "infra"

[profiles]
usage = { description = "dev + usage server + agent", components = ["postgres", "nats", "docs", "usage", "usage-agent"] }
```

Then `mise run cluster:profile usage` (or `cluster:create usage`). `mise run cluster:profile dev` removes it.

## Cluster agent (`cluster-agent/`)

One stdlib-only Python pod (`docker/agent.py`) in `kube-system` that watches the cluster and sends
events to `USAGE_URL` (default `http://usage.usage.svc`, set in the manifest). It's a localdev
**infra** component named `usage-agent` (`manifests/20-10-usage-agent.yaml`).

| event | when |
|---|---|
| `cluster.created` | once per cluster. `cluster_id` is the `kube-system` namespace UID, `occurred_at` its creation time |
| `cluster.heartbeat` | at start and every 15 min, with nodes, cores, memory, pods and namespaces. The agent dies with the cluster, so it can't send `destroyed`; heartbeats give "last seen" |
| `cluster.event` `profile.changed` | `cnav/localdev-profile` changes (profile or components) |
| `cluster.event` `app.deployed` | a Deployment in a user namespace is created or changed (new generation). User namespaces are those not created by localdev's manifests (no `objectset.rio.cattle.io/hash` label) and not `kube-*` |

- **Cluster facts:**
  - On localdev: from ConfigMap `kube-system/localdev-host`, which localdev's `cluster:create` /
    `cluster:profile` write (`kube/localdev/hostfacts.py`). It holds username, `cluster_type`,
    `cluster_role` (set with `[cluster] role = "..."` in `local.toml`, default dev), host OS and k3d
    version. Profile and components come from `cnav/localdev-profile`.
  - On other clusters: `CLUSTER_TYPE`, `CLUSTER_ROLE`, `CLUSTER_USERNAME` and `CLUSTER_PLATFORM` env.
    The platform is otherwise guessed from the kubelet version.
  - Until a cluster type is known, the agent sends nothing.
- **Best-effort delivery:** one file per event in an `emptyDir` spool, POSTed in batches every 30 s.
  - 5xx or unreachable: kept and retried.
  - 422: only the rejected events are dropped.
  - The spool is capped at 1000 files and 3 days.
- **Restarts are harmless:** created and app events have ids derived from the cluster id (uuid5), so
  a restarted agent resends them and the server counts them as duplicates.
- **Deploy:** in `kube/localdev/local.toml`, `[components.usage-agent] path = "../components/usage/cluster-agent"`,
  `layer = "infra"`, and add `usage-agent` to your profile. `mise run build usage-agent` rebuilds it.
- **Tests:** `cd cluster-agent && mise run test` (unittest, plus a contract test of every event against `openapi.yaml`).

## Adding an event type

1. Add a schema to `openapi.yaml` (copy one of the `Localdev*` schemas: `allOf` envelope plus
   `type` const plus `data`, `unevaluatedProperties: false`). Add it to `Event.oneOf` and the
   discriminator `mapping`.
2. Add `examples/<name>.json`, and an invalid one if it has rules worth pinning. Then run `mise run api:test`.
3. Server: nothing is required, since it's stored in `events` as soon as the spec has it. For a typed
   table, add a migration (`server/usage_server/migrations/NNN_*.sql`) and a handler in `store.py`'s `HANDLERS`.
   Client: add an `emit` helper if needed.

Removing or renaming a field, or making one required, is breaking: use a new type name (`….v2`).
