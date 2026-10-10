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
client/            CLI + local spool, stdlib Python          (next)
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
| `localdev.cluster.created` | `cluster_id, profile, components[], platform (k3d), platform_version, kubernetes_version, os (linux/macos/windows), os_variant?, os_version, arch (amd64/arm64)` |
| `localdev.cluster.destroyed` | `cluster_id` |
| `localdev.cluster.event` | `cluster_id, name (e.g. tests.run), outcome? (success/failure/cancelled), duration_ms?, attributes? (flat, ≤50 keys)` |

## Server (`server/`)

FastAPI + psycopg. Requests are validated against `openapi.yaml` itself (jsonschema), so the server
can't drift from the spec. Each batch is written in one transaction:
- **`events`** gets every accepted event as received (jsonb `data`). It's the source of truth.
- **Typed projections** are filled by handlers in `store.py`: `localdev_clusters` (one row per cluster,
  correct whatever order created/destroyed/ad-hoc events arrive in) and `localdev_cluster_events`.
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

[profiles]
usage = { description = "dev + the usage server", components = ["postgres", "nats", "docs", "usage"] }
```

Then `mise run cluster:profile usage` (or `cluster:create usage`). `mise run cluster:profile dev` removes it.

## Adding an event type

1. Add a schema to `openapi.yaml` (copy one of the `Localdev*` schemas: `allOf` envelope plus
   `type` const plus `data`, `unevaluatedProperties: false`). Add it to `Event.oneOf` and the
   discriminator `mapping`.
2. Add `examples/<name>.json`, and an invalid one if it has rules worth pinning. Then run `mise run api:test`.
3. Server: nothing is required, since it's stored in `events` as soon as the spec has it. For a typed
   table, add a migration (`server/usage_server/migrations/NNN_*.sql`) and a handler in `store.py`'s `HANDLERS`.
   Client: add an `emit` helper if needed.

Removing or renaming a field, or making one required, is breaking: use a new type name (`….v2`).
