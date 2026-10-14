# usage-server

Tracks how people use internal tools. **Clients** built into tools send **events** to a small
**write-only HTTP API**, a **server** stores them in Postgres, and Grafana dashboards aggregate them.
The first tool tracked is localdev itself: clusters created and destroyed, plus ad-hoc events
attached to a cluster (app deployed, test suite run, ...).

Two localdev components, coupled only by the API: **usage-server** (this folder: the contract and the
ingest server) and **usage-client** (`../usage-client`: the in-cluster event emitter). The client
reads the API from here, pinned to a frozen version in `api/`. Each has its own `.mise.toml`.

```
openapi.yaml       the contract (OpenAPI 3.1): the version you edit
api/               frozen versions, openapi-<info.version>.yaml (checked in; clients pin one)
examples/          request bodies: *.json valid, invalid/*.json must be rejected
scripts/           check_examples.py (examples + frozen versions), api_versions.py (freeze)
redocly.yaml       lint rules
manifests/ docker/ build.sh VERSION tests/     the server component
../usage-client/   in-cluster event emitter: stdlib Python pod in kube-system (infra component)
```

## Tasks

```bash
mise run api:lint     # Redocly lint
mise run api:check    # examples/ and the spec's inline examples vs. the schemas; frozen versions unchanged
mise run api:docs     # build/api.html
mise run api:test     # lint + check
mise run api:freeze   # copy openapi.yaml to api/openapi-<info.version>.yaml
```

`mise install` provides node (for Redocly) and uv (for the checker).

## API versions (`api/`)

`openapi.yaml` is the spec you edit; `info.version` is its version. `mise run api:freeze` copies it to
`api/openapi-<version>.yaml`, which is committed and never changes again: `api:check` fails when
`openapi.yaml` differs from the frozen file of the same version, so a changed spec needs a new
`info.version` (then a new freeze). `mise run release` requires the current version to be frozen.
Clients pin a frozen file: usage-client's contract test reads `api/openapi-1.0.0.yaml`, and moving
it to a newer version is a deliberate change in the client.

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

## Server

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
| deploy | the `usage` profile (`application-profiles.toml`, below): `mise run cluster:profile usage`. `mise run build usage-server` rebuilds |
| URLs | `http://api.localhost/usage`, also `http://localhost/api/usage` (localdev's API host, prefix stripped; open, no API key yet), `http://usage.usage.svc` |
| database | `usage` on `postgres/postgres`. The server only reads Secret `usage-db-app` (`host`, `port`, `dbname`, `username`, `password`, `uri`: the keys of CloudNativePG's `<cluster>-app` secret) and runs `usage_server/migrations/*.sql` at startup. Provisioning is a separate Job, `usage-db-init` (`manifests/50-2-1-usage-db-init.yaml`): as the Postgres admin (Secret `usage-db-admin`, which only the Job reads) it creates roles `usage` (owner) and `usage_ro` (read-only, Grafana) and the database, and sets both passwords. The passwords are generated in the cluster (ESO `Password` generators, `manifests/50-2-0-usage-secrets.yaml`): no secret-store entries, only the admin login comes from the store. Idempotent; `mise run db-init` re-runs it, needed after the passwords are regenerated (their Secrets deleted: `refreshPolicy: CreatedOnce`). With CloudNativePG, that file is replaced (e.g. a Cluster `usage-db`, whose secret is `usage-db-app`) and the Deployment stays as is |
| Grafana | datasource **Usage** (uid `usage-pg`, read-only), provisioned from Secret `usage-db-ro` in `usage` (label `grafana_datasource`, read by Grafana's sidecar) |
| metrics | `usage_events_total{type,result}`, `usage_batches_total{status}` on port `metrics` (9090) |
| logs | logfmt, one line per batch |
| image | `us-central1-docker.pkg.dev/scs-d-common-nonprod/docker-registry/tools/cluster/usage/server:<VERSION>`, version in `VERSION`. `mise run release` builds linux/amd64, pushes it and tags git `usage-server-v<VERSION>`; it refuses a dirty tree, an API version not frozen in `api/`, or a version already pushed. Shared clusters deploy it from argo-app-of-apps: `cluster-workloads/usage-server` (`stage-apps/usage-server.yaml` on stage, at https://api.stage.ops.dematic.dev/usage) |

```bash
mise run test     # validation tests + API tests on a throwaway DB (localdev Postgres; skipped if absent)
mise run dev      # run locally on :8080 against localdev's `usage` database
mise run db-init  # re-run the database init Job in localdev (idempotent; e.g. after the passwords are regenerated)
mise run image    # docker build (standalone: IMAGE=usage-server:dev)
mise run release  # bump VERSION and commit first; builds, pushes and tags (gcloud auth configure-docker us-central1-docker.pkg.dev)
```

Server layout: `manifests/` (applied by k3s), `docker/` (Dockerfile, requirements, `usage_server/`
code and migrations), `build.sh` (stages `docker/` + `openapi.yaml` as the build context), `tests/`.

**In localdev.** `application-profiles.toml` (checked in, next to `profiles.toml`) registers the
component (`requires = ["postgres"]`, `layer = "apps"`) and the `usage` profile (postgres, docs,
usage-server; the component includes usage-client-local, a client feeding this server, see Client below). `mise run cluster:profile usage` (or `cluster:create usage`) deploys it;
`mise run cluster:profile dev` removes it.

## Client (`../usage-client/`)

One stdlib-only Python pod (`docker/agent.py`) in `kube-system` that watches the cluster and sends
events to `USAGE_URL`, set in the manifest: the shared server, `https://api.stage.ops.dematic.dev/usage`
today (prod later). It's a localdev **infra** component named `usage-client` (`manifests/20-10-usage-client.yaml`).

**A second client for usage development:** this component also deploys `usage-client-local`
(`manifests/50-3-usage-client-local.yaml`), the same image in namespace `usage`
with `USAGE_URL=http://usage.usage.svc`, the local server (not `api.localhost`: in a pod that's the pod
itself). Both clients send the same events, each to its own server; the ids are derived from the
cluster id, so neither depends on the other. `USAGE_URL` in each manifest is the switch.

| event | when |
|---|---|
| `cluster.created` | once per cluster. `cluster_id` is the `kube-system` namespace UID, `occurred_at` its creation time |
| `cluster.updated` | when the facts change or become known (the backfill), and once at start with the known facts (its id comes from the facts, so the server drops it if nothing changed) |
| `cluster.heartbeat` | at start and every 15 min, with nodes, cores, memory, pods and namespaces. The agent dies with the cluster, so it can't send `destroyed`; heartbeats give "last seen" |
| `cluster.event` `profile.changed` | `cnav/localdev-profile` changes (profile or components) |
| `cluster.event` `app.deployed` | a Deployment in a user namespace is created or changed (new generation). User namespaces are those not created by localdev's manifests (no `objectset.rio.cattle.io/hash` label) and not `kube-*` |

- **Cluster facts:**
  - On localdev: from ConfigMap `kube-system/localdev-host`, which localdev's `cluster:create` /
    `cluster:profile` write (`hostfacts.py`). It holds username, `cluster_type`,
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
- **Deploy:** always on in every localdev cluster: `profiles.toml` declares
  `[components.usage-client]` (`layer = "infra"`, `always = true`). With no usage server it
  spools (and logs one warning), e.g. off the internal network. `mise run build usage-client` rebuilds
  it, and restarts usage-client-local too (label `app=usage-client`). For other clusters, a Helm chart
  (published to an OCI registry) is the planned next step.
- **Tests:** `cd ../usage-client && mise run test` (unittest, plus a contract test of every event against the pinned `api/openapi-<version>.yaml`).

## Adding an event type

1. Add a schema to `openapi.yaml` (copy one of the `Localdev*` schemas: `allOf` envelope plus
   `type` const plus `data`, `unevaluatedProperties: false`). Add it to `Event.oneOf` and the
   discriminator `mapping`.
2. Add `examples/<name>.json`, and an invalid one if it has rules worth pinning. Bump `info.version`
   (minor, for an additive change), run `mise run api:test`, then `mise run api:freeze`.
3. Server: nothing is required, since it's stored in `events` as soon as the spec has it. For a typed
   table, add a migration (`docker/usage_server/migrations/NNN_*.sql`) and a handler in `store.py`'s `HANDLERS`.
   Client: add an `emit` helper if needed, and move its pinned version (`SPEC` in `tests/test_agent.py`).

Removing or renaming a field, or making one required, is breaking: use a new type name (`….v2`).
