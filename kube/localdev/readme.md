# kube/localdev

Ephemeral local Kubernetes (k3d) with path-based routing on `http://localhost/<app>`, behind Google SSO.

| path | app |
|---|---|
| `/` | Cluster navigator (cnav): cards linking to the apps below. It's also the catch-all, so unknown paths land here. |
| `/whoami` | SSO test app. It echoes the request headers, including `X-Auth-Request-Email`. |
| `/headlamp` | Kubernetes UI. No token login: Headlamp acts as its own cluster-admin account, so SSO is the only gate. |
| `/grafana` | Grafana. You're signed in from the SSO email header, as an org Admin. |
| `/prometheus` | Prometheus UI. Use `/prometheus/targets` for scrape health. |
| `/alertmanager` | Alertmanager UI |
| `/pgweb` | pgweb, a Postgres web UI. Each browser tab has its own connection: pick a database bookmark (`app`, `keycloak`) on the connect screen |
| `/nats` | NUI, a NATS web UI: live subscribe and publish on subjects, plus JetStream streams, consumers, KV and object stores |
| `/keycloak/admin` | Keycloak admin console (an identity provider for apps), behind SSO. `/keycloak/realms/*` (login, token, discovery) is public |
| `/registry` | Registry UI (joxit/docker-registry-ui), read-only, for browsing images in the k3d registry (`localhost:5005`) |
| *(no route)* | Loki holds every pod's logs, shipped by Alloy. Query them in Grafana → Explore → Loki, e.g. `{namespace="demo", app="whoami"}`. |
| `/oauth2/*` | oauth2-proxy sign-in and callback (`/oauth2/sign_out` to log out) |

## Prerequisites

- **Docker.** On Linux, your user must be in the `docker` group. On WSL2, use Docker Desktop with WSL integration or native Docker inside WSL. On macOS, use Docker Desktop, OrbStack or Colima.
- **Free host ports:** 80 (ingress) and 5005 (local registry `localdev-registry`). Check with `ss -ltn`. On Linux, binding port 80 works because Docker does it as root.
- **About 4 GB of RAM free for Docker.**
- **Linux: raise the inotify limits.** Every k3d node shares the host kernel, and following container logs (Alloy → Loki) uses one inotify instance per stream. The default of 128 runs out, and pods then log `failed to create fsnotify watcher: too many open files` instead of their real output. Raise it once:
  ```bash
  printf 'fs.inotify.max_user_instances=1024\nfs.inotify.max_user_watches=1048576\n' | sudo tee /etc/sysctl.d/99-localdev.conf
  sudo sysctl --system
  ```
  Docker Desktop and OrbStack on macOS and WSL2 run their own VM kernels, and usually already allow more.
- **mise.** Run `mise install` in this directory to get k3d, kubectl, helm and k9s.
- **`secrets.env` or `secrets.env.asc`**, from the Google OAuth setup below.

## Usage

```bash
# cluster lifecycle
mise run cluster:create                     # create cluster, write .kubeconfig, load secrets, build apps
mise run cluster:status                     # nodes, helm install jobs, ingresses
mise run cluster:secrets                    # (re)load secrets.env into the cluster: Google client, admin login, Grafana admin
mise run cluster:destroy                    # delete cluster + .kubeconfig (runtime.data is kept)
mise run cluster:recreate                   # destroy + create
mise run cluster:clean                      # delete runtime.data (asks first; cluster must be destroyed)

# apps and tools
mise run build cnav                         # rebuild apps/cnav, push to the local registry, restart it
mise run pg:forward                         # postgres on localhost:5432 (pass a port to change it)
mise run pg:psql                            # psql shell in the postgres pod
mise run kn:forward                         # Knative internal gateway on localhost:8081 (call with a Host header)
mise run keycloak:apply                     # apply keycloak/realms/*.json to Keycloak (idempotent)
mise run keycloak:export [realm]            # Keycloak UI changes -> keycloak/realms/<realm>.json

# grafana dashboards
mise run grafana:list                       # dashboards in Grafana
mise run grafana:export "<title|uid>"       # Grafana UI -> grafana/dashboards/<folder>/
mise run grafana:import <file|dir> [folder]
mise run grafana:fetch <grafana.com id> <folder>
mise run grafana:check                      # JSON valid + uids unique (a duplicate freezes ALL provisioning)
```

`manifests/` is mounted live into k3s's auto-deploy directory, so saving a file there re-applies it within seconds.

```
manifests/
  00-namespaces.yaml
  10-platform/  10-1-traefik-config  10-2-oauth2-proxy
  20-infra/     20-1-headlamp  20-2-monitoring  20-3-logging  20-4-scrape  20-5-tracing
  30-tools/     30-1-registry-ui (ns registry)  30-2-postgres (ns pg)  30-3-nats (ns nats)
                30-4-knative-operator  30-5-knative (ns knative-*)  30-6-keycloak (ns keycloak)
  50-apps/      50-1-whoami  50-2-cnav
```
- Subfolders work at any depth.
- **Filenames must be unique across the whole tree.** k3s tracks each file as an AddOn named after the file's base name, ignoring the folder.
  - The name is cut at the **first dot**. `10.1-a.yaml` and `10.2-b.yaml` would both become AddOn `10`, and the two files would keep deleting each other's objects.
  - So name files `<folder number>-<n>-<name>.yaml`, with dashes and no dots before `.yaml`.
  - After renaming a file, delete its old AddOn entry with `kubectl -n kube-system delete addon <old-name>`. That removes only the tracking entry, not the objects.
- Removing an object from a file deletes it from the cluster.
- Changing a `HelmChart`'s `valuesContent` runs a Helm upgrade.
- **Deleting a whole file does not delete its objects.** Remove them from the file first, or `kubectl delete` them.
- Changes to `k3d.yaml` need `mise run cluster:recreate`. `runtime.data/` is kept across it. After editing `secrets.env`, run `mise run cluster:secrets`, which updates the running cluster.

**Signing in.** Every app goes through oauth2-proxy (each namespace has an `sso` forwardAuth Middleware). Its sign-in page offers two ways in:
- **Sign in with Google.** Only accounts on the **allow-list** in `manifests/10-platform/10-2-oauth2-proxy.yaml` (`authenticatedEmailsFile.restricted_access`, one per line) get in, and the list applies to every app. Google accounts must also be test users on the consent screen (below).
- **Username and password:** user `admin`, password `LOCAL_ADMIN_PASSWORD` from `secrets.env`. This works offline, since nothing goes to Google. Local sessions have no email, so the allow-list doesn't apply to them.
  - Apps that sign you in from the email header (Grafana) show their own login form for `admin` instead. Grafana's built-in admin is set to the same `admin` and password.
  - To change the password: edit `secrets.env`, run `mise run encrypt secrets.env`, then `mise run cluster:secrets`.
- To limit a single app to certain Google accounts, add `?allowed_emails=a@x.com` to that app's middleware address. That also shuts `admin` out of that app, because local sessions have no email.

## Grafana dashboards (`grafana/`)

Dashboards are files: `grafana/dashboards/<folder>/*.json`. The folder is mounted live into the cluster, and Grafana shows **one folder per directory**, re-reading files every 10 seconds:

| folder | what's in it |
|---|---|
| `platform/` | Traefik (requests, latency and errors per Ingress), oauth2-proxy (SSO checks, 401s and 403s), CoreDNS, API server, kubelet, cluster compute |
| `infra/` | Kubernetes compute and networking by namespace, workload and pod; node exporter; Prometheus; Alertmanager; Grafana; Loki & Alloy health; OpenTelemetry services (rate, errors, duration, service graph, traces) |
| `tools/` | Keycloak (troubleshooting, capacity planning), Postgres (9628), Postgres top queries (pg_stat_statements), NATS server (2279), NATS JetStream (14725), Docker registry (9621), Knative (Serving autoscaling, requests, event deliveries) |
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
- `runtime.data/registry`: images in the k3d registry, so `localhost:5005` keeps them across rebuilds.

`mise run cluster:clean` deletes it all. It refuses to run while the cluster exists, shows sizes, and asks you to type `clean`. Pass `--yes` to skip the prompt. The files are owned by container users (root, and postgres uid 999), so it deletes them through a throwaway container rather than `sudo`.

## Postgres

Postgres 18 is a single-instance StatefulSet in namespace `pg` (`manifests/30-tools/30-2-postgres.yaml`), with no operator.
- **Credentials** (fixed, dev only): user `app`, password `localdev`, database `app`.
- **In-cluster connection:** host `postgres-rw.pg.svc:5432`. The Secret `postgres-app` has the keys `username`, `password`, `dbname`, `host`, `port`, `uri` and `jdbc-uri`.
- **From your desktop:** run `mise run pg:forward`, then connect to `postgresql://app:localdev@localhost:5432/app`.
- **Web UI:** `/pgweb/`. Each app has its **own database** on this server (e.g. `app`, `keycloak`), not a schema in `app`.
  - pgweb runs with `--sessions`, so **every browser tab or window has its own connection**. pgweb's default is a single connection shared by everyone.
  - A new tab opens on the connect screen: choose a **Bookmark** (one per database), then click Connect. Within a tab, the database menu switches databases.
  - When an app gets a new database, add a bookmark file to the `pgweb-bookmarks` ConfigMap in `30-2-postgres.yaml`.
- **Later move to CloudNativePG:** the Service and Secret names match what CloudNativePG creates for a `Cluster` named `postgres`, so apps won't need changes. Data moves with `pg_dump` and `pg_restore`.

## NATS

NATS 2.15 runs as a single server with JetStream in namespace `nats` (`manifests/30-tools/30-3-nats.yaml`, official `nats` chart).
- **Client URL:** `nats://nats.nats.svc:4222`, with no auth (local only).
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

**Examples** (`examples/knative/`, not auto-applied):
- `hello.yaml` is a Knative Service.
- `eventing.yaml` is a Broker, an `event-display` consumer and a Trigger. Its header has a curl command for sending a test event.

**Observability:**
- Knative exports its own metrics and traces as OTLP to Alloy.
- Metrics (`kn_revision_*`, `kn_eventing_dispatch_*`, and request metrics per service) go to Prometheus, and appear on the **Knative** dashboard (`tools/`).
- Traces (`activator → <service>`, `broker.ingress → <consumer>`) go to Tempo.

**CLI:** `kn` is installed by mise. Try `kn service list -A`, `kn broker list -n demo`, `kn trigger list -n demo`.

**UI:** Knative has no web UI of its own. Headlamp shows Knative's custom resources generically, under Custom Resources. Headlamp's Knative plugin is beta and covers Serving only, and isn't installed.

## Keycloak (identity provider for apps)

Keycloak 26 runs in namespace `keycloak` (`manifests/30-tools/30-6-keycloak.yaml`) as an **OIDC/OAuth server for the apps you build**: realms, clients, roles and test users. localdev's own sign-in (oauth2-proxy) is separate and unaffected.

| what | URL |
|---|---|
| admin console (behind SSO, then `admin` / `LOCAL_ADMIN_PASSWORD`) | http://localhost/keycloak/admin/ |
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
- **Realms are code:** `keycloak/realms/<realm>.json`.
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
- **Data** lives in database `keycloak` on `pg/postgres`, which an init container creates. It survives `cluster:recreate`.
- **Metrics:** Keycloak is scraped via the `metrics: enabled` label. User-event metrics and HTTP latency histograms are on. Dashboards are in `tools/`.
- **Resources:** about 0.5–0.7 GiB of memory, and roughly 30–60 seconds to start.

## Apps (`apps/`)

Each `apps/<name>/` directory holds a Dockerfile. `mise run build <name>` builds it as `localhost:5005/<name>:latest`, pushes it to the k3d registry and restarts any deployment labelled `app=<name>`. Inside the cluster, the image is pulled as `localdev-registry:5005/<name>:latest`. `mise run cluster:create` builds every app.

- **cnav** (`apps/cnav`, deployed by `manifests/50-apps/50-2-cnav.yaml`): a small Python server (stdlib plus PyYAML) serving the navigator page.
  - **Cards (left):** from `cards.yaml`. Fields: `component`, `icon` (an emoji), `link`, and an optional `description`.
  - **Status column (right):** from `status.yaml`. It's organised as groups, then items, then checks. Check kinds are `Deployment`, `StatefulSet` and `DaemonSet` (with `namespace` and `name`), and `HelmChart` (with `name`). An item's light is the worst of its checks, and a group's light is the worst of its items. Clicking a row opens the first workload in Headlamp. The column refreshes every 10 seconds.
    - **Green:** all replicas ready.
    - **Amber:** partially ready, a rollout in progress, starting up (under 2 minutes), restarted in the last 10 minutes, Helm still installing, or scaled to 0.
    - **Red:** no ready replicas, CrashLoopBackOff, an image pull error, a config error, stuck Pending (over 2 minutes), a failed Helm install, or the workload not found. The reason is shown under the row.
  - **Editing:** both files live in the `cnav-config` ConfigMap. Edit them there and the page picks up the change within about a minute, with no rebuild. The copies in `apps/cnav/` are only defaults for running the image on its own, where the status column shows "unavailable".
  - **Header:** shows who you're signed in as (from oauth2-proxy's `/oauth2/userinfo`) and a **Log out** button. Logging out ends the SSO session and lands on `/signed-out`, the only cnav page outside SSO. Your Google session stays signed in.
  - **Cards:** the ↗ icon on each card opens its link in a new tab.
  - **Memory:** working-set memory from metrics-server, summed per row, per group (in the group header) and for the cluster. Hover a row to see its own figure.
    - The header shows **`pods`**, the sum for workloads listed in `status.yaml`, and **`node`**, the whole node(s), which is what Headlamp shows.
    - The node figure is roughly twice the pods figure because it also includes the `k3s server` process (API server, controllers, kubelet and the SQLite datastore, all outside any pod, about 1.5 GiB), containerd, kernel memory and active file cache.
  - **Access:** the `cnav-status` ClusterRole gives read-only access to workloads, pods, jobs, HelmCharts and pod metrics.

## Google OAuth client (one-time setup)

oauth2-proxy needs a Google OAuth client ID and secret, plus a cookie secret.

1. **Pick a project.** Go to <https://console.cloud.google.com/>. Select an existing project, such as the root `GOOGLE_CLOUD_PROJECT` (`gcloud-aws-compare`), or create one like `localdev-sso`.

2. **Set up the consent screen.** Open **APIs & Services → OAuth consent screen**. This now lives under **Google Auth Platform**.
   - Under **Branding**, set the app name (`localdev`), a user support email and a developer contact email.
   - Under **Audience**, set the user type to **External** and the publishing status to **Testing**.
   - Under **Test users**, add every Google account that should be able to sign in. In Testing mode, only these accounts can log in, with a maximum of 100. The cluster's allow-list (`10-2-oauth2-proxy.yaml`) narrows this further.
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

6. **Write `secrets.env`** in this directory. It's gitignored, so it never gets committed:
   ```bash
   GOOGLE_CLIENT_ID=xxxxxxxx.apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=GOCSPX-xxxxxxxx
   COOKIE_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx=
   LOCAL_ADMIN_PASSWORD=choose-one     # local "admin" login (sign-in page form, Grafana)
   ```

7. **Encrypt it and commit only the `.asc`:**
   ```bash
   mise run encrypt secrets.env    # -> secrets.env.asc
   git add secrets.env.asc
   ```
   `mise run cluster:create` (via `mise run cluster:secrets`) decrypts `secrets.env.asc` automatically when `secrets.env` is missing or older than it, and checks all four keys are set. Save `secrets.env` before encrypting: the encrypt task reads the file on disk. The passphrase is `$PASSPHRASE_FILE`, which defaults to `~/.ssh/id_ed25519`.
