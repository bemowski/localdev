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
| `GET` | `/ping` | `{ping: pong}`, no database check |
| `GET` | `/healthz` | `{status: ok \| unavailable}` |
| `GET` | `/openapi.yaml` | the spec |

Base URL: `http://api.localhost/usage` or `http://localhost/api/usage` (desktop) or `http://usage.usage.svc` (in the cluster).
Clients read it from `USAGE_URL`.

- **Write-only.** Reporting reads Postgres directly.
- **Batches are all-or-nothing.** One invalid event rejects the batch (422, with per-event `index`, `path` and `message`).
- **Idempotent.** `event_id` (a client-generated uuid) is the key, so resending is safe and is counted as `duplicates`.
- **No auth yet.** Internal only.

Every event has the envelope `event_id, type, occurred_at, username, source{name,version}, data`.
`type` picks the `data` schema:

| type | data |
|---|---|
| `cluster.created` | `cluster_id, cluster_type (localdev/ephemeral/durable/unknown), cluster_role (dev/test/stage/prod/perftest/unknown), platform (k3d, eks, ...), kubernetes_version`; optional `platform_version, arch`, and for localdev `profile, components[], os, os_variant, os_version` |
| `cluster.updated` | `cluster_id` plus any of the facts above (not profile/components): the ones sent replace the stored ones. Username `unknown` never replaces a known one |
| `cluster.heartbeat` | `cluster_id, nodes, cores, memory_bytes, pods, namespaces` (capacity summed over nodes) |
| `cluster.event` | `cluster_id, name (e.g. tests.run), outcome? (success/failure/cancelled), duration_ms?, attributes? (flat, ≤50 keys)` |
| `cluster.destroyed` | `cluster_id` |

Cluster facts are sent in `cluster.created`. Facts the sender doesn't know yet are `unknown` (type,
role, username) or left out (optional ones), and `cluster.updated` fills them in later. The other
cluster events only carry `cluster_id`.

## Server (`server/`)

FastAPI + psycopg. Requests are validated against `openapi.yaml` itself (jsonschema), so the server
can't drift from the spec. Each batch is written in one transaction:
- **`events`** gets every accepted event as received (jsonb `data`). It's the source of truth.
- **Typed projections** are filled by handlers in `store.py`, normalized on `cluster_id`:
  - `clusters`: one row per cluster, with type, role, platform, versions, the localdev facts,
    `created_at`, `destroyed_at` and `last_seen_at` (the latest event of any kind). It comes out
    right whatever order events arrive in. `cluster.updated` patches the facts it carries.
  - `cluster_events`: ad-hoc events (`app.deployed`, `tests.run`, ...).
  - `cluster_heartbeats`: one row per heartbeat, with the size stats.
- Duplicates are skipped before projection, so retries never double-count.

| | |
|---|---|
| deploy | only while working on it, via your gitignored `kube/localdev/local.toml` (below), then `mise run cluster:profile usage`. `mise run build usage` rebuilds |
| URLs | `http://api.localhost/usage`, also `http://localhost/api/usage` (localdev's API host, prefix stripped; open, no API key yet), `http://usage.usage.svc` |
| database | `usage` on `postgres/postgres`. The server only reads Secret `usage-db-app` (`host`, `port`, `dbname`, `username`, `password`, `uri`: the keys of CloudNativePG's `<cluster>-app` secret) and runs `usage_server/migrations/*.sql` at startup. Provisioning is a separate Job, `usage-db-init` (`manifests/50-2-1-usage-db-init.yaml`): as the Postgres admin (Secret `usage-db-admin`, which only the Job reads) it creates roles `usage` (owner) and `usage_ro` (read-only, Grafana) and the database, from the secret store (`server/secrets.defaults.toml`). Idempotent; `mise run server:db-init` re-runs it. With CloudNativePG, that file is replaced (e.g. a Cluster `usage-db`, whose secret is `usage-db-app`) and the Deployment stays as is |
| Grafana | datasource **Usage** (uid `usage-pg`, read-only), provisioned through an ExternalSecret in `monitoring` |
| metrics | `usage_events_total{type,result}`, `usage_batches_total{status}` on port `metrics` (9090) |
| logs | logfmt, one line per batch |

```bash
cd server
mise run test     # validation tests + API tests on a throwaway DB (localdev Postgres; skipped if absent)
mise run dev      # run locally on :8080 against localdev's `usage` database
mise run db-init  # re-run the database init Job in localdev (idempotent; e.g. after a password change)
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

[profiles]
usage = { description = "Postgres + docs + the usage server", components = ["postgres", "docs", "usage"] }
```

Then `mise run cluster:profile usage` (or `cluster:create usage`). `mise run cluster:profile dev` removes it.

## Cluster agent (`cluster-agent/`)

One stdlib-only Python pod (`docker/agent.py`) in `kube-system` that watches the cluster and sends
events to `USAGE_URL` (default `http://usage.usage.svc`, set in the manifest). It's a localdev
**infra** component named `usage-agent` (`manifests/20-10-usage-agent.yaml`).

| event | when |
|---|---|
| `cluster.created` | once per cluster. `cluster_id` is the `kube-system` namespace UID, `occurred_at` its creation time |
| `cluster.updated` | when the facts change or become known (the backfill), and once at start with the known facts (its id comes from the facts, so the server drops it if nothing changed) |
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
  - **Best effort:** anything still missing is `unknown` (optional facts are left out), and the agent
    starts sending right away. When the ConfigMap appears or changes later, the next poll sends
    `cluster.updated` with what changed. Known facts are sticky: the ConfigMap going away doesn't turn
    them back into `unknown`, and a guessed platform never replaces a reported one.
- **Best-effort delivery:** one file per event in an `emptyDir` spool, POSTed in batches every 30 s.
  - 5xx or unreachable: kept and retried.
  - 422: only the rejected events are dropped.
  - The spool is capped at 1000 files and 3 days.
- **Restarts are harmless:** created and app events have ids derived from the cluster id (uuid5), so
  a restarted agent resends them and the server counts them as duplicates.
- **Deploy:** always on in every localdev cluster: `kube/localdev/profiles.toml` declares
  `[components.usage-agent]` (`path`, `layer = "infra"`, `always = true`). With no usage server it
  spools (and logs one warning). `USAGE_URL` in the manifest points at the in-cluster server for now;
  a public endpoint later. `mise run build usage-agent` rebuilds it. For other clusters, a Helm chart
  (published to an OCI registry) is the planned next step.
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
