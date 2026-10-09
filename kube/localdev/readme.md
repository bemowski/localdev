# kube/localdev

Ephemeral local Kubernetes (k3d) with path-based routing on `http://localhost/<app>`, behind Google SSO.

| path | app |
|---|---|
| `/` | Cluster navigator (cnav): cards linking to the apps below. It's also the catch-all, so unknown paths land here. |
| `/whoami` | SSO test app. It echoes the request headers, including `X-Auth-Request-Email`. |
| `/headlamp` | Kubernetes UI. No token login: Headlamp acts as its own cluster-admin account, so the SSO allow-list is the only gate. |
| `/grafana` | Grafana. You're signed in from the SSO email header, as an org Admin. |
| `/prometheus` | Prometheus UI. Use `/prometheus/targets` for scrape health. |
| `/alertmanager` | Alertmanager UI |
| `/pgweb` | pgweb, a Postgres web UI locked to the `app` database |
| `/nats` | NUI, a NATS web UI: live subscribe and publish on subjects, plus JetStream streams, consumers, KV and object stores |
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
mise run create      # create cluster, write .kubeconfig, load secrets, wait for traefik
mise run status      # nodes, helm install jobs, ingresses
mise run destroy     # delete cluster + .kubeconfig
mise run recreate
mise run build cnav  # rebuild apps/cnav, push to the local registry, restart it
mise run pg:forward  # postgres on localhost:5432 (pass a port to change it)
mise run pg:psql     # psql shell in the postgres pod
mise run clean       # delete runtime.data (asks first; cluster must be destroyed)
mise run grafana:list                    # dashboards in Grafana
mise run grafana:export "<title|uid>"    # Grafana UI -> grafana/dashboards/<folder>/
mise run grafana:import <file|dir> [folder]
mise run grafana:fetch <grafana.com id> <folder>
k9s                  # uses ./.kubeconfig via mise env; ~/.kube/config is never touched
```

`manifests/` is mounted live into k3s's auto-deploy directory, so saving a file there re-applies it within seconds.

```
manifests/
  00-namespaces.yaml
  10-platform/  10-1-traefik-config  10-2-oauth2-proxy
  20-infra/     20-1-headlamp  20-2-monitoring  20-3-logging  20-4-scrape
  30-tools/     30-1-registry-ui  30-2-postgres  30-3-nats
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
- Changes to `k3d.yaml` or `secrets.env` need `mise run recreate`. `runtime.data/` is kept across it.

**Allow-lists.** Each app has its own `sso` forwardAuth Middleware with `?allowed_emails=a@x.com,b@y.com`. Google accounts must also be listed as test users on the consent screen (below).

## Grafana dashboards (`grafana/`)

Dashboards are files: `grafana/dashboards/<folder>/*.json`. The folder is mounted live into the cluster, and Grafana shows **one folder per directory**, re-reading files every 10 seconds:

| folder | what's in it |
|---|---|
| `platform/` | Traefik (requests, latency and errors per Ingress), oauth2-proxy (SSO checks, 401s and 403s), CoreDNS, API server, kubelet, cluster compute |
| `infra/` | Kubernetes compute and networking by namespace, workload and pod; node exporter; Prometheus; Alertmanager; Grafana; Loki & Alloy health; Logs overview (filter by namespace and app, regex search) |
| `tools/` | Postgres (9628), Postgres top queries (pg_stat_statements), NATS server (2279), NATS JetStream (14725), Docker registry (9621) |
| `apps/` | yours |

**Developer workflow:**
1. Build or edit a dashboard in the Grafana UI. Provisioned dashboards can be edited and saved there.
2. Run `mise run grafana:export "My dashboard"` (a title search or a uid). It writes clean JSON to `grafana/dashboards/<folder>/<slug>.json`: the dashboard's current folder if it's one of the four, otherwise `apps/`. Pass a folder name to override.
3. Commit the file. It's what Grafana loads from then on, on this cluster and any other built from the repo.

The other tasks:
- `grafana:import <file|dir> [folder]` copies dashboard JSON (an exported file, or an API response) into the tree, normalised so it works here.
- `grafana:fetch <id> <folder>` pulls a community dashboard from grafana.com and resolves its `${DS_…}` data-source inputs to the local `prometheus` and `loki` data sources.
- All three are `grafana/dashtool.py`.

**Notes:**
- A dashboard saved in the UI but not exported lives only in Grafana's database, which is lost on `recreate`.
- To remove a provisioned dashboard, delete its file.
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

## Persistent data (`runtime.data/`)

`runtime.data/` (gitignored) sits next to this readme and survives `destroy` and `recreate`:
- `runtime.data/postgres`: Postgres data. It's mounted into the server node at `/runtime.data`, and the pod uses it as a hostPath.
- `runtime.data/nats`: JetStream data (streams, KV).
- `runtime.data/registry`: images in the k3d registry, so `localhost:5005` keeps them across rebuilds.

`mise run clean` deletes it all. It refuses to run while the cluster exists, shows sizes, and asks you to type `clean`. Pass `--yes` to skip the prompt. The files are owned by container users (root, and postgres uid 999), so it deletes them through a throwaway container rather than `sudo`.

## Postgres

Postgres 18 is a single-instance StatefulSet in `tools` (`manifests/30-tools/30-2-postgres.yaml`), with no operator.
- **Credentials** (fixed, dev only): user `app`, password `localdev`, database `app`.
- **In-cluster connection:** host `postgres-rw.tools.svc:5432`. The Secret `postgres-app` has the keys `username`, `password`, `dbname`, `host`, `port`, `uri` and `jdbc-uri`.
- **From your desktop:** run `mise run pg:forward`, then connect to `postgresql://app:localdev@localhost:5432/app`.
- **Web UI:** `/pgweb/`.
- **Later move to CloudNativePG:** the Service and Secret names match what CloudNativePG creates for a `Cluster` named `postgres`, so apps won't need changes. Data moves with `pg_dump` and `pg_restore`.

## NATS

NATS 2.15 runs as a single server with JetStream in `tools` (`manifests/30-tools/30-3-nats.yaml`, official `nats` chart).
- **Client URL:** `nats://nats.tools.svc:4222`, with no auth (local only).
- **Monitoring endpoint:** `http://nats.tools.svc:8222` (`/varz`, `/connz`, `/subsz`, `/jsz`).
- **Web UI:** NUI at `/nats/`. It's preloaded with a `localdev` connection from the `nui-contexts` ConfigMap. Open the connection, then use Messages to subscribe to a subject or wildcard (`demo.>`, `>`) and watch traffic live.
- **CLI:** `kubectl -n tools exec -it deploy/nats-box -- nats sub '>'`, or `nats stream ls`, `nats pub demo.x hi`.
- **Metrics:** an exporter sidecar plus a PodMonitor, so Prometheus scrapes NATS automatically. Metric names start with `nats_`, e.g. `nats_varz_in_msgs`.

## Apps (`apps/`)

Each `apps/<name>/` directory holds a Dockerfile. `mise run build <name>` builds it as `localhost:5005/<name>:latest`, pushes it to the k3d registry and restarts any deployment labelled `app=<name>`. Inside the cluster, the image is pulled as `localdev-registry:5005/<name>:latest`. `mise run create` builds every app.

- **cnav** (`apps/cnav`, deployed by `manifests/50-apps/50-2-cnav.yaml`): a small Python server (stdlib plus PyYAML) serving the navigator page.
  - **Cards (left):** from `cards.yaml`. Fields: `component`, `icon` (an emoji), `link`, and an optional `description`.
  - **Status column (right):** from `status.yaml`. It's organised as groups, then items, then checks. Check kinds are `Deployment`, `StatefulSet` and `DaemonSet` (with `namespace` and `name`), and `HelmChart` (with `name`). An item's light is the worst of its checks, and a group's light is the worst of its items. Clicking a row opens the first workload in Headlamp. The column refreshes every 10 seconds.
    - **Green:** all replicas ready.
    - **Amber:** partially ready, a rollout in progress, starting up (under 2 minutes), restarted in the last 10 minutes, Helm still installing, or scaled to 0.
    - **Red:** no ready replicas, CrashLoopBackOff, an image pull error, a config error, stuck Pending (over 2 minutes), a failed Helm install, or the workload not found. The reason is shown under the row.
  - **Editing:** both files live in the `cnav-config` ConfigMap. Edit them there and the page picks up the change within about a minute, with no rebuild. The copies in `apps/cnav/` are only defaults for running the image on its own, where the status column shows "unavailable".
  - **Header:** shows who you're signed in as (from oauth2-proxy's `/oauth2/userinfo`) and a **Log out** button. Logging out ends the SSO session and lands on `/signed-out`, the only cnav page outside SSO. Your Google session stays signed in.
  - **Cards:** the ↗ icon on each card opens its link in a new tab.
  - **Memory:** working-set memory from metrics-server, summed per row, per group (in the group header) and for the cluster (beside the "Status" title). Hover a row to see its own figure. Only workloads listed in `status.yaml` are counted.
  - **Access:** the `cnav-status` ClusterRole gives read-only access to workloads, pods, jobs, HelmCharts and pod metrics.

## Google OAuth client (one-time setup)

oauth2-proxy needs a Google OAuth client ID and secret, plus a cookie secret.

1. **Pick a project.** Go to <https://console.cloud.google.com/>. Select an existing project, such as the root `GOOGLE_CLOUD_PROJECT` (`gcloud-aws-compare`), or create one like `localdev-sso`.

2. **Set up the consent screen.** Open **APIs & Services → OAuth consent screen**. This now lives under **Google Auth Platform**.
   - Under **Branding**, set the app name (`localdev`), a user support email and a developer contact email.
   - Under **Audience**, set the user type to **External** and the publishing status to **Testing**.
   - Under **Test users**, add every Google account that should be able to sign in. In Testing mode, only these accounts can log in, with a maximum of 100. The per-app allow-lists in the cluster narrow this further.
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
   ```

7. **Encrypt it and commit only the `.asc`:**
   ```bash
   mise run encrypt secrets.env    # -> secrets.env.asc
   git add secrets.env.asc
   ```
   `mise run create` decrypts `secrets.env.asc` automatically when `secrets.env` is missing. The passphrase is `$PASSPHRASE_FILE`, which defaults to `~/.ssh/id_ed25519`.
