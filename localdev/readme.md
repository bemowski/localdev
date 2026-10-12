# kube/localdev

Ephemeral local Kubernetes (k3d) with path-based routing on `http://localhost/<app>`, behind Google SSO.

**Docs:** [http://localhost/docs/](http://localhost/docs/) when the cluster runs (source: [`docs/content/`](docs/content/)): concepts, getting started, moving from docker compose, cluster layout, components, building your own component (including from another project, via a gitignored `local.toml`), and secrets (External Secrets Operator with a local store: `secrets.store.toml`, `mise run secrets:sync`). This readme keeps the setup and reference details.

**Working with an AI coding agent?** [`AGENTS.md`](AGENTS.md) tells it how to integrate an app as a component without modifying localdev itself.

| path | app |
|---|---|
| `/` | Cluster navigator (cnav): cards linking to the apps below. It's also the catch-all, so unknown paths land here. |
| `/whoami` | SSO test app. It echoes the request headers, including `X-Auth-Request-Email`. |
| `/headlamp` | Kubernetes UI. No token login: Headlamp acts as its own cluster-admin account, so SSO is the only gate. |
| `/grafana` | Grafana. You're signed in from the SSO email header, as an org Admin. |
| `/prometheus` | Prometheus UI. Use `/prometheus/targets` for scrape health. |
| `/alertmanager` | Alertmanager UI |
| `/pgweb` | pgweb, a Postgres web UI. Each browser tab has its own connection: pick a database bookmark on the connect screen (one per database, automatic) |
| `/nats` | NUI, a NATS web UI: live subscribe and publish on subjects, plus JetStream streams, consumers, KV and object stores |
| `/keycloak/admin` | Keycloak admin console (an identity provider for apps), behind SSO. `/keycloak/realms/*` (login, token, discovery) is public |
| `/docs` | localdev docs (MkDocs, built from `docs/`) |
| `/registry` | Registry UI (joxit/docker-registry-ui), read-only, for browsing images in the k3d registry (`localhost:5005`) |
| *(no route)* | Loki holds every pod's logs, shipped by Alloy. Query them in Grafana → Explore → Loki, e.g. `{namespace="demo", app="whoami"}`. |
| `/oauth2/*` | oauth2-proxy sign-in and callback (`/oauth2/sign_out` to log out) |
| `http://api.localhost/<name>/`, `/api/<name>/` | The API host (also under `/api` on localhost), for APIs that tools call: no SSO, each API open or keyed (`Authorization: Bearer <key>`). Example: `/whoami/` with key `localdev-whoami-key`; `usage` is open. See [`components/api-auth/readme.md`](components/api-auth/readme.md) |

## Prerequisites

Supported platforms: **Linux**, **macOS with Docker Desktop or Rancher Desktop** (Intel or Apple Silicon), and **Windows in WSL2 with Docker Engine installed inside the distro** (not Docker Desktop). The per-platform setup is in [Getting started](docs/content/getting-started.md), and the quirks are in [Platform notes](docs/content/platform-notes.md).

- **Docker.** On Linux and WSL, your user must be in the `docker` group. On macOS, Docker Desktop or Rancher Desktop (with the `dockerd (moby)` engine), with VirtioFS file sharing and its own Kubernetes turned off.
- **Free host ports:** 80 (ingress), 5005 (local registry `localdev-registry`), and 5432 / 4222 on 127.0.0.1 (Postgres / NATS). Check with `ss -ltnp` (Linux, WSL) or `lsof -nP -iTCP -sTCP:LISTEN` (macOS).
- **About 6 GB of memory for Docker.**
- **Linux and WSL: raise the inotify limits.** Every k3d node shares the host kernel, and following container logs (Alloy → Loki) uses one inotify instance per stream. The default of 128 runs out, and pods then log `failed to create fsnotify watcher: too many open files` instead of their real output. Raise it once:
  ```bash
  printf 'fs.inotify.max_user_instances=1024\nfs.inotify.max_user_watches=1048576\n' | sudo tee /etc/sysctl.d/99-localdev.conf
  sudo sysctl --system
  ```
- **mise.** Run `mise install` in this directory to get k3d, kubectl, kn, yq and Python.
- **git, curl, gpg.** gpg is only needed to decrypt `secrets.store.toml.asc`.
- **`secrets.store.toml` or `secrets.store.toml.asc`**: your secrets (Google OAuth client, cookie secret, admin password), from the Google OAuth setup below. All secrets are mastered in the secret store and synced by External Secrets: see the docs ([Secrets](docs/content/secrets.md)). Have an old `secrets.env`? `mise run secrets:migrate` converts it.

## Usage

```bash
# cluster lifecycle
mise run cluster:create [profile]           # create cluster with a profile (lite|dev|knative|auth|full), load secrets, build images
mise run cluster:profile [profile]          # list profiles / switch the running cluster (adds/removes tools live)
mise run cluster:status                     # profile, nodes, helm install jobs, ingresses
mise run secrets:sync                       # (re)load secrets.store.toml into the secret store (cluster:secrets does the same)
mise run secrets:list                       # store entries/keys (no values) and ExternalSecret status
mise run cluster:destroy                    # delete cluster + .kubeconfig (runtime.data is kept)
mise run cluster:recreate [profile]         # destroy + create
mise run cluster:clean                      # delete runtime.data (asks first; cluster must be destroyed; keeps the profile)

# apps and tools
mise run build <component>                  # rebuild a component's image (cnav, docs), push to the local registry, restart it
mise run pg:psql                            # psql shell in the postgres pod
mise run kn:forward                         # Knative internal gateway on localhost:8081 (call with a Host header)
mise run keycloak:apply                     # apply components/keycloak/realms/*.json to Keycloak (idempotent)
mise run keycloak:export [realm]            # Keycloak UI changes -> components/keycloak/realms/<realm>.json

# grafana dashboards
mise run grafana:list                       # dashboards in Grafana
mise run grafana:export "<title|uid>"       # Grafana UI -> grafana/dashboards/<folder>/
mise run grafana:import <file|dir> [folder]
mise run grafana:fetch <grafana.com id> <folder>
mise run grafana:check                      # JSON valid + uids unique (a duplicate freezes ALL provisioning)
```

## Profiles, layout and components

See the docs: [Getting started](docs/content/getting-started.md) (profiles), [Cluster layout](docs/content/cluster-layout.md) (layers, `runtime.profile/`, manifest rules) and [Components](docs/content/components.md) (component folders, `profiles.toml` metadata, adding one).

**What the cluster knows about its profile and host.** `cluster:create` and `cluster:profile` publish two ConfigMaps for pods to read: `cnav/localdev-profile` (profile name, enabled components) and `kube-system/localdev-host` (username, `cluster_type` = localdev, `cluster_role`, host `os` / `os_variant` / `os_version`, k3d version, from `hostfacts.py`). The role defaults to `dev`; set it in your `local.toml` with `[cluster]` / `role = "perftest"` (one of dev, test, stage, prod, perftest), then `mise run cluster:profile`.

**Signing in.** Every app goes through oauth2-proxy (each namespace has an `sso` forwardAuth Middleware). Its sign-in page offers two ways in:
- **Sign in with Google.** Only accounts on the **allow-list** get in, and it applies to every app. The list is `[sso-access]` in `secrets.store.toml`: `emails` for exact addresses and `domains` for whole domains (`"example.com"`, or `".example.com"` for any subdomain). Edit it, then `mise run secrets:sync`. See [Secrets](docs/content/secrets.md#who-can-sign-in). Google accounts must also be test users on the consent screen (below), unless the app is published.
- **Username and password:** user `admin`, password `[localdev-admin] password` from `secrets.store.toml`. This works offline, since nothing goes to Google. Local sessions have no email, so the allow-list doesn't apply to them.
  - Apps that sign you in from the email header (Grafana) show their own login form for `admin` instead. Grafana's built-in admin is set to the same `admin` and password.
  - To change the password: edit `secrets.store.toml`, run `mise run secrets:sync` (Reloader rolls oauth2-proxy and Grafana), and `mise run encrypt secrets.store.toml` to update the shared copy.
- To limit a single app to certain Google accounts, add `?allowed_emails=a@x.com` to that app's middleware address. That also shuts `admin` out of that app, because local sessions have no email.

## Grafana dashboards (`grafana/`)

Dashboards are files: `grafana/dashboards/<folder>/*.json`. The folder is mounted live into the cluster, and Grafana shows **one folder per directory**, re-reading files every 15 seconds:

| folder | what's in it |
|---|---|
| `platform/` | Traefik (requests, latency and errors per Ingress), oauth2-proxy (SSO checks, 401s and 403s), CoreDNS, API server, kubelet, cluster compute |
| `infra/` | Kubernetes compute and networking by namespace, workload and pod; node exporter; Prometheus; Alertmanager; Grafana; Loki & Alloy health; OpenTelemetry services (rate, errors, duration, service graph, traces); Docker registry (9621) |
| `tools/` | *from the enabled components (`components/*/dashboards/`):* Keycloak (troubleshooting, capacity planning), Postgres (9628), Postgres top queries (pg_stat_statements), NATS server (2279), NATS JetStream (14725), Knative (Serving autoscaling, requests, event deliveries) |
| `apps/` | yours |
| *(root)* `logs.json` | **Logs**, top level in Grafana: pod stdout **and** OpenTelemetry logs. Filter by namespace, app/service, **Sources** (tick pod stdout and/or OpenTelemetry), **Level** (Loki's `detected_level`, case-insensitive; synonyms grouped, e.g. warn/warning; `unknown` = no level in the line) and regex search. Error lines: text match for stdout, severity for OTel. |

**Developer workflow:**
1. Build or edit a dashboard in the Grafana UI. Provisioned dashboards can be edited and saved there.
2. Run `mise run grafana:export "My dashboard"` (a title search or a uid). It writes clean JSON.
   - A dashboard already provisioned from a file is updated **in place**.
   - A new one goes to `grafana/dashboards/<folder>/<slug>.json`: its current Grafana folder if it's one of the four, otherwise `apps/`.
   - Pass a folder to override; `root` means the top level.
3. Commit the file. It's what Grafana loads from then on, on this cluster and any other built from the repo.

The other tasks:
- `grafana:import <file|dir> [folder]` copies dashboard JSON (an exported file, or an API response) into the tree, normalised so it works here.
- `grafana:fetch <id> <folder>` pulls a community dashboard from grafana.com and resolves its `${DS_…}` data-source inputs to the local `prometheus` and `loki` data sources.
- All three are `grafana/dashtool.py`.

**Notes:**
- A dashboard saved in the UI but not exported lives only in Grafana's database, which is lost on `recreate`.
- To remove a provisioned dashboard, delete its file.
- **Every dashboard needs a unique `uid`.** If two files share one, Grafana stops applying **any** dashboard file changes (it only logs a warning). Run `mise run grafana:check` after copying files around.
- The kube-prometheus-stack built-ins were copied into `platform/` and `infra/`, and the chart's own copies are turned off.

### App metrics convention

Any pod in any namespace with the label **`metrics: enabled`** gets its port **named `metrics`** scraped at `/metrics` every 30 seconds. `job` is set to the pod's `app` label. That's all a developer needs: no Prometheus CRDs.

```yaml
template:
  metadata:
    labels: {app: orders, metrics: enabled}
  spec:
    containers:
      - name: orders
        ports:
          - {name: metrics, containerPort: 9090}
```

Postgres uses this convention for its exporter sidecar. The scrape definitions for platform and infra components, and for the registry outside the cluster, are in `manifests/20-infra/20-4-scrape.yaml`.

## OpenTelemetry (traces, metrics and logs over OTLP)

Alloy is the cluster's OpenTelemetry collector. Point any OTel SDK or agent at it:

```yaml
env:
  - {name: OTEL_EXPORTER_OTLP_ENDPOINT, value: "http://alloy.monitoring.svc:4318"}   # HTTP; gRPC: :4317
  - {name: OTEL_SERVICE_NAME,           value: "orders"}
  - {name: OTEL_EXPORTER_OTLP_PROTOCOL, value: "http/protobuf"}
```

Alloy tags everything with the sending pod's `k8s.namespace.name`, `k8s.pod.name`, `k8s.deployment.name` and so on, then routes each signal:

| signal | goes to | in Grafana |
|---|---|---|
| traces | **Tempo** (`20-5-tracing.yaml`, 72-hour retention, `runtime.data/tempo`) | Explore → Tempo, TraceQL `{resource.service.name="orders"}` |
| metrics | Prometheus's native OTLP receiver. `job` = service name; namespace, pod and deployment become labels | any dashboard; resource attributes are in `target_info` |
| logs | Loki's native OTLP endpoint. `service_name` and `k8s_*` are labels; `trace_id` and `span_id` are structured metadata | the **Logs** dashboard (Sources: OpenTelemetry), or Explore → Loki, `{service_name="orders"}` |

**How the signals link up:**
- **Trace → logs:** each span links to the Loki lines with the same trace ID.
- **Log → trace:** the `trace_id` on an OTLP log line links to Tempo.
- **Metric → trace:** Tempo's span metrics (`traces_spanmetrics_*`) and service graph are written into Prometheus, with **exemplars**. The dots on latency graphs open the trace behind them.
- The **OpenTelemetry services** dashboard (`infra/`) shows rate, errors and duration per service, the service graph, top operations and recent traces, for any service that sends traces.

**To try it without an app**, OpenTelemetry's load generator works:
```bash
kubectl -n demo run tg --rm -it --restart=Never \
  --image=ghcr.io/open-telemetry/opentelemetry-collector-contrib/telemetrygen:v0.161.0 -- \
  traces --otlp-endpoint=alloy.monitoring.svc:4317 --otlp-insecure --duration=60s --rate=5 --child-spans=3 --service=otel-demo
```

The OpenTelemetry Operator (zero-code auto-instrumentation through a pod annotation) isn't installed yet. It's the planned next step once real apps arrive.

## Persistent data (`runtime.data/`)

`runtime.data/` (gitignored) sits next to this readme and survives `destroy` and `recreate`:
- `runtime.data/postgres`: Postgres data. It's mounted into the server node at `/runtime.data`, and the pod uses it as a hostPath.
- `runtime.data/nats`: JetStream data (streams, KV).
- `runtime.data/tempo`: traces, kept for 72 hours.
- `runtime.data/prometheus`: metrics, kept for 3 days (at most 5 GB).
- `runtime.data/loki`: logs, kept for 72 hours.
- `runtime.data/grafana`: Grafana's database: dashboards and settings made in the UI. The admin password is reset from the secret store on every start.
- `runtime.data/registry`: images in the k3d registry, so `localhost:5005` keeps them across rebuilds.

`mise run cluster:clean` deletes it all. It refuses to run while the cluster exists, shows sizes, and asks you to type `clean`. Pass `--yes` to skip the prompt. The files are owned by container users (root, postgres uid 999, and the monitoring stack's users), so it deletes them through a throwaway container rather than `sudo`.

## Postgres

Postgres 18 is a single-instance StatefulSet in namespace `postgres` (`components/postgres/manifests/30-2-postgres.yaml`), with no operator.
- **Credentials** (fixed, dev only): user `app`, password `localdev`, database `app`.
- **In-cluster connection:** host `postgres-rw.postgres.svc:5432`. The Secret `postgres-app` has the keys `username`, `password`, `dbname`, `host`, `port`, `uri` and `jdbc-uri`.
- **From your desktop:** `postgresql://app:localdev@localhost:5432/app`, always on while postgres is installed (Service `postgres/postgres-local`, bound to 127.0.0.1).
- **Web UI:** `/pgweb/`. Each app has its **own database** on this server (e.g. `app`, `keycloak`), not a schema in `app`.
  - pgweb runs with `--sessions`, so **every browser tab or window has its own connection**. pgweb's default is a single connection shared by everyone.
  - A new tab opens on the connect screen: choose a **Bookmark**, then click Connect. To switch databases within a tab, click the current database's name at the top of the left sidebar and pick another in the search box that opens.
  - **Bookmarks are automatic:** the pgweb pod's `bookmarks` sidecar writes one per database on the server (superuser login from Secret `postgres-app`) every 30 seconds, and removes them for dropped databases. A component that creates its own database needs no registration.
- **Later move to CloudNativePG:** the Service and Secret names match what CloudNativePG creates for a `Cluster` named `postgres`, so apps won't need changes. Data moves with `pg_dump` and `pg_restore`.

## NATS

NATS 2.15 runs as a single server with JetStream in namespace `nats` (`components/nats/manifests/30-3-nats.yaml`, official `nats` chart).
- **Client URL:** `nats://nats.nats.svc:4222` in the cluster, `nats://localhost:4222` from your desktop (Service `nats/nats-local`, bound to 127.0.0.1), with no auth.
- **Monitoring endpoint:** `http://nats.nats.svc:8222` (`/varz`, `/connz`, `/subsz`, `/jsz`).
- **Web UI:** NUI at `/nats/`. It's preloaded with a `localdev` connection from the `nui-contexts` ConfigMap. Open the connection, then use Messages to subscribe to a subject or wildcard (`demo.>`, `>`) and watch traffic live.
- **CLI:** `kubectl -n nats exec -it deploy/nats-box -- nats sub '>'`, or `nats stream ls`, `nats pub demo.x hi`.
- **Metrics:** an exporter sidecar plus a PodMonitor, so Prometheus scrapes NATS automatically. Metric names start with `nats_`, e.g. `nats_varz_in_msgs`.

## Knative (Serving + Eventing, internal only)

Knative 1.23 is installed by the **Knative Operator** (`30-4-knative-operator.yaml`) from the `KnativeServing` and `KnativeEventing` resources in `30-5-knative.yaml`. To upgrade, bump the operator chart version and the `version:` in those two resources.

**Internal only.**
- Every Knative Service gets a cluster-local URL, `http://<svc>.<ns>.svc.cluster.local`.
- Nothing is exposed through Traefik.
- Kourier, the networking layer, is ClusterIP only.

From your desktop, run `mise run kn:forward`, then `curl -H 'Host: hello.demo.svc.cluster.local' localhost:8081`. (A Knative Service's Kubernetes Service is an ExternalName, so you can't port-forward to it directly.)

To expose one later, add a wildcard `*.knative.localhost` route to Kourier and remove the service's cluster-local visibility. That's one rule, with no per-service config.

**Serving:**
- `kn service create`, or a `serving.knative.dev/v1` `Service`. You get revisions, traffic splitting and autoscaling.
- Services **scale to zero** about 90 seconds after the last request: a 60-second window plus a 30-second grace period. A cold start takes about 0.6 seconds for a small Go service.

**Eventing:**
- The Broker class is `MTChannelBasedBroker`, backed by the **in-memory channel**. Events are lost if the dispatcher restarts, which is fine for dev.
- Create a Broker in a namespace, then Triggers that filter on CloudEvent attributes (`type`, `source`, and so on) and deliver to a Knative Service or any addressable.
- Producers POST CloudEvents to `http://broker-ingress.knative-eventing.svc.cluster.local/<ns>/<broker>`.

**Examples** (`components/knative/examples/`, not auto-applied):
- `hello.yaml` is a Knative Service.
- `eventing.yaml` is a Broker, an `event-display` consumer and a Trigger. Its header has a curl command for sending a test event.

**Observability:**
- Knative exports its own metrics and traces as OTLP to Alloy.
- Metrics (`kn_revision_*`, `kn_eventing_dispatch_*`, and request metrics per service) go to Prometheus, and appear on the **Knative** dashboard (`tools/`).
- Traces (`activator → <service>`, `broker.ingress → <consumer>`) go to Tempo.

**CLI:** `kn` is installed by mise. Try `kn service list -A`, `kn broker list -n demo`, `kn trigger list -n demo`.

**UI:** Knative has no web UI of its own. Headlamp shows Knative's custom resources generically, under Custom Resources. Headlamp's Knative plugin is beta and covers Serving only, and isn't installed.

## Keycloak (identity provider for apps)

Keycloak 26 runs in namespace `keycloak` (`components/keycloak/manifests/30-6-keycloak.yaml`) as an **OIDC/OAuth server for the apps you build**: realms, clients, roles and test users. localdev's own sign-in (oauth2-proxy) is separate and unaffected.

| what | URL |
|---|---|
| admin console (behind SSO, then `admin` / the `localdev-admin` password) | http://localhost/keycloak/admin/ |
| issuer (put this in your app's config) | `http://localhost/keycloak/realms/localdev` |
| discovery | `http://localhost/keycloak/realms/localdev/.well-known/openid-configuration` |
| from pods (token, JWKS, discovery) | `http://keycloak.keycloak.svc:8080/keycloak/realms/localdev/...` |

- **Browser vs. pod URLs.** Browsers use `localhost`, while pods can't reach `localhost`.
  - Keycloak issues every token with the `localhost` issuer.
  - When asked from inside the cluster, its discovery document lists **in-cluster** token and JWKS addresses.
  - So point the app's *discovery URL* at the in-cluster address, and set its *expected issuer* to the `localhost` one.

  Example environment:
  ```yaml
  OIDC_ISSUER: http://localhost/keycloak/realms/localdev
  OIDC_DISCOVERY_URL: http://keycloak.keycloak.svc:8080/keycloak/realms/localdev/.well-known/openid-configuration
  ```
- **The `/keycloak/realms/*` paths are public** (no localdev SSO): app login pages, token, discovery and JWKS work for any client, including curl.
- **Realms are code:** `components/keycloak/realms/<realm>.json`.
  - `mise run keycloak:apply` applies them with keycloak-config-cli. It's idempotent and runs during `cluster:create`.
  - `mise run keycloak:export [realm]` writes realm changes made in the admin UI back to the file. The export is **trimmed** to your clients, roles, groups and common realm settings, so files stay readable. Users and client secrets are kept from the file, because Keycloak never exports them.
- **The starter realm `localdev`** has **dev-only credentials**:
  - **`demo-app`:** public client, browser login with PKCE, redirects to `http://localhost/*`. Password grant is enabled for curl testing.
  - **`demo-service`:** confidential client, client credentials, secret `demo-service-secret`.
  - **Roles** `user` and `admin`; group `admins`.
  - **Users** `alice` (user) and `bob` (admins), both with password `password`.
  ```bash
  curl -s http://localhost/keycloak/realms/localdev/protocol/openid-connect/token \
    -d grant_type=password -d client_id=demo-app -d username=alice -d password=password | jq -r .access_token
  ```
- **Data** lives in database `keycloak` on `postgres/postgres`, which an init container creates. It survives `cluster:recreate`.
- **Metrics:** Keycloak is scraped via the `metrics: enabled` label. User-event metrics and HTTP latency histograms are on. Dashboards are in `tools/`.
- **Resources:** about 0.5–0.7 GiB of memory, and roughly 30–60 seconds to start.

## Images (`components/<name>/docker/`, `build.sh`)

A component's optional `docker/` folder holds application code and its Dockerfile. `mise run build <name>` builds it as `localhost:5005/<name>:latest`, pushes it to the k3d registry and restarts any deployment labelled `app=<name>`. Inside the cluster, the image is pulled as `localdev-registry:5005/<name>:latest`. If the component has an executable `build.sh`, the task runs that instead, from the component's folder, with `IMAGE`, `REGISTRY`, `COMPONENT_DIR` and `LOCALDEV_ROOT` set; it must build `$IMAGE` and/or push more images itself. Deployments labelled `app=<name>` or `localdev/component=<name>` are restarted. `mise run cluster:create` builds every enabled component that has an image, and `cluster:profile` builds those it adds.

- **docs** (`components/docs`): `build.sh` copies its `docker/` files and the top-level `docs/` into a temporary build context. The multi-stage Dockerfile renders the markdown with MkDocs Material (`mkdocs build --strict`: broken links fail the build) and serves the site from nginx under `/docs/`. Edit `docs/content/*.md` (and `docs/mkdocs.yml` for the nav), then `mise run build docs`.
- **cnav** (code in `components/cnav/docker/`, deployed by `components/cnav/manifests/20-7-cnav.yaml`): a small Python server (stdlib plus PyYAML) serving the navigator page.
  - **Cards (left):** from `cards.yaml`. Fields: `component`, `icon` (an emoji), `link`, an optional `description`, and optional `links`: a list of `{label, url, tool}` shown as pill buttons on the card's bottom row, each opening in a new tab. `url` is absolute or a path on this host (the Headlamp card's `docs` pill opens `/docs/`); `tool` hides the pill when that component isn't installed.
  - **Status column (right):** from `status.yaml`. It's organised as groups, then items, then checks. Check kinds are `Deployment`, `StatefulSet` and `DaemonSet` (with `namespace` and `name`), and `HelmChart` (with `name`). An item's light is the worst of its checks, and a group's light is the worst of its items. Clicking a row opens the first workload in Headlamp. The column refreshes every 10 seconds. **Components add themselves:** any Deployment, StatefulSet or DaemonSet labelled `localdev/component: <name>` gets a row named after the component (group `apps`, or the `localdev/status-group` label), unless `status.yaml` already covers it.
    - **Green:** all replicas ready.
    - **Amber:** partially ready, a rollout in progress, starting up (under 2 minutes), restarted in the last 10 minutes, Helm still installing, or scaled to 0.
    - **Red:** no ready replicas, CrashLoopBackOff, an image pull error, a config error, stuck Pending (over 2 minutes), a failed Helm install, or the workload not found. The reason is shown under the row.
  - **Editing:** both files live in the `cnav-config` ConfigMap. Edit them there and the page picks up the change within about a minute, with no rebuild. The copies in `components/cnav/docker/` are only defaults for running the image on its own, where the status column shows "unavailable".
  - **Header:** shows who you're signed in as (from oauth2-proxy's `/oauth2/userinfo`) and a **Log out** button. Logging out ends the SSO session and lands on `/signed-out`, the only cnav page outside SSO. Your Google session stays signed in.
  - **Cards:** the ↗ icon on each card opens its link in a new tab.
  - **Memory:** working-set memory from metrics-server, summed per row, per group (in the group header) and for the cluster. Hover a row to see its own figure.
    - The header shows **`pods`**, the sum for workloads listed in `status.yaml`, and **`node`**, the whole node(s), which is what Headlamp shows.
    - The node figure is roughly twice the pods figure because it also includes the `k3s server` process (API server, controllers, kubelet and the SQLite datastore, all outside any pod, about 1.5 GiB), containerd, kernel memory and active file cache.
  - **Below the cards**, one full-width panel (about a third of the screen high) with four tabs, **Events** (the default), **Ports**, **Ingress** and **Credentials**; the page remembers the last one:
    - **Ports** lists the host ports k3d exposes and what answers each one in the cluster, e.g. `localhost:80 → kube-system / traefik :80 → web`, with ready endpoints and a link to the Service in Headlamp. It reads `k3d.yaml`, mounted read-only from the node, and matches each port to a LoadBalancer or NodePort Service, plus the image registry on `localhost:5005`. Postgres (5432) and NATS (4222) are exposed this way. To expose another tool, add a `127.0.0.1:<port>:<port>` mapping to `k3d.yaml` (apply it with `k3d cluster edit localdev --port-add 127.0.0.1:<port>:<port>@loadbalancer`, or `cluster:recreate`) and a LoadBalancer Service on that port in the component. A mapped port with nothing behind it (component not installed) shows grey.
    - **Ingress** maps every route Traefik serves, grouped by host (`localhost`, i.e. any host, and `api.localhost`): the path, its auth (**sso**, **api-key**, **open**, or **re-route** for `localhost/api/*`, which hands requests to the API host), the backend Service and whether it has ready endpoints, and the middlewares (strip, redirect). API routes also list their `localhost/api/...` alias. It reads Ingresses, Traefik IngressRoutes and Middlewares live (auth is worked out from each Middleware: forwardAuth to oauth2-proxy is sso, to api-auth is api-key), so it follows profile switches with no config.
    - **Credentials** lists logins for things not behind SSO: the local admin (SSO sign-in, Grafana's form, the Keycloak admin console), Postgres (with ready-made `postgresql://` and JDBC URLs), the Keycloak database and test users, and so on. Usernames and database names show as is; secret values (keys like `password`, `token`, `api-key`, `secret`, and URLs built from them) are masked, the eye reveals one, **unmask all** (top right, on this tab only) reveals them all, and the copy button copies a value without revealing it. Values come from the secret store (`secret-store` namespace; cnav has a read-only Role there and nowhere else) and are only sent to the page when you reveal or copy one; each reveal is in cnav's access log by row id, never by value. The curated rows are `credentials.yaml` in the `cnav-config` ConfigMap (`entry` = a store entry, or `static` values; `extras` = templates over the entry's keys; `tool` hides a row when its component isn't installed). Any other store entry with a `password` key, or an `api-<name>` entry with API keys (see `components/api-auth/`), shows up on its own (`secret-store/<entry>`), but only while the component that declares it (`localdev/component` label, set by `secrets:sync`) is in the profile. Anyone signed in to SSO can see these; that's the point on a local cluster.
    - **Events** lists cluster events, newest first: age, type, `namespace / component` and the message. Kubernetes drops events after an hour, so Alloy ships them to Loki (`loki.source.kubernetes_events` in `20-3-logging.yaml`), where they're kept for 72 hours in `runtime.data/loki` and survive `cluster:recreate`. cnav reads them from Loki and falls back to the Kubernetes API (the last hour) while Loki isn't answering; the header says which. The page polls with the rest (every 10 seconds), but each poll is cheap: cnav backfills the 72 hours once, keeps the collapsed rows in memory, and then asks Loki only for lines since the newest it has seen (with a 60-second overlap for late lines, deduplicated). It resyncs fully every 15 minutes.
      - **Type** comes from the event's reason: `crash` (back-off restarting, OOM, evicted) and `image` (pull failures) are red; `unhealthy` (probe failures, scheduling, mounts), `restart` (killed by a failed probe) and `warning` (any other Warning) are amber; `deploy` (replica sets scaled, k3s manifests applied, StatefulSet/Job pods created) is blue. A repeated event shows once, with `×N`.
      - **Component** is the workload name, with the generated pod and replica set suffixes stripped. It links to the object in Headlamp.
      - Routine pod lifecycle events (scheduled, pulling, pulled, created, started, a normal rollout's stopped containers) are hidden; tick **all** to show them. **severe** shows only the red and amber rows (problems); the two exclude each other, and the page remembers your choice. The **↻** button refreshes the events now.
      - **Your own events:** post a plain Kubernetes Event, e.g. reason `Deployed` on your Deployment, and it shows up as `deploy`, in Headlamp too.
  - **Logs:** stdlib `logging`, one logfmt line per record (`ts=… level=info logger=cnav.http msg="…"`), so Grafana's Logs dashboard can filter by level. Status changes are logged once (turning red is a warning), API failures are errors with the traceback, and the page's own 10-second polling and probes only show at debug. Set the level with `CNAV_LOG_LEVEL` in `20-7-cnav.yaml`.
  - **Access:** the `cnav-status` ClusterRole gives read-only access to workloads, pods, jobs, HelmCharts, pod metrics, Services, EndpointSlices and events (the fallback).

## Google OAuth client (one-time setup)

oauth2-proxy needs a Google OAuth client ID and secret, plus a cookie secret.

1. **Pick a project.** Go to <https://console.cloud.google.com/>. Select an existing project, such as the root `GOOGLE_CLOUD_PROJECT` (`gcloud-aws-compare`), or create one like `localdev-sso`.

2. **Set up the consent screen.** Open **APIs & Services → OAuth consent screen**. This now lives under **Google Auth Platform**.
   - Under **Branding**, set the app name (`localdev`), a user support email and a developer contact email.
   - Under **Audience**, set the user type to **External** and the publishing status to **Testing**.
   - Under **Test users**, add every Google account that should be able to sign in. In Testing mode, only these accounts can log in, with a maximum of 100. The cluster's allow-list (`[sso-access]` in `secrets.store.toml`) narrows this further.
   - No scopes need adding. oauth2-proxy only asks for `openid email profile`, which are non-sensitive, so Google doesn't need to verify the app.

3. **Create the client.** Go to **Google Auth Platform → Clients → Create client**.
   - Application type: **Web application**
   - Name: `localdev oauth2-proxy`
   - Authorized JavaScript origins: `http://localhost`
   - Authorized redirect URIs: `http://localhost/oauth2/callback`

   Google allows plain `http` for `localhost`.

4. **Copy the client ID and client secret right away.** Newer consoles only show the secret once, at creation, so download the JSON too.

5. **Generate a cookie secret.** It must be 32 bytes, URL-safe base64:
   ```bash
   openssl rand -base64 32 | tr -- '+/' '-_'
   ```

6. **Write `secrets.store.toml`** in this directory. It's gitignored, so it never gets committed:
   ```toml
   [sso]
   google-client-id = "xxxxxxxx.apps.googleusercontent.com"
   google-client-secret = "GOCSPX-xxxxxxxx"
   cookie-secret = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx="

   [localdev-admin]
   password = "choose-one"     # local "admin" login (sign-in page form, Grafana, Keycloak)
   ```

7. **Encrypt it and commit only the `.asc`:**
   ```bash
   mise run encrypt secrets.store.toml    # -> secrets.store.toml.asc
   git add secrets.store.toml.asc
   ```
   `mise run cluster:create` decrypts `secrets.store.toml.asc` automatically when `secrets.store.toml` is missing or older than it, and checks every required value (`""` in `secrets.defaults.toml`) is set. Save the file before encrypting: the encrypt task reads the file on disk. The passphrase is `$PASSPHRASE_FILE`, which defaults to `~/.ssh/id_ed25519`.
