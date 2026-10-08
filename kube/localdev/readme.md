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
| `/registry` | Registry UI (joxit/docker-registry-ui), read-only, for browsing images in the k3d registry (`localhost:5005`) |
| *(no route)* | Loki holds every pod's logs, shipped by Alloy. Query them in Grafana → Explore → Loki, e.g. `{namespace="demo", app="whoami"}`. |
| `/oauth2/*` | oauth2-proxy sign-in and callback (`/oauth2/sign_out` to log out) |

## Prerequisites

- **Docker.** On Linux, your user must be in the `docker` group. On WSL2, use Docker Desktop with WSL integration or native Docker inside WSL. On macOS, use Docker Desktop, OrbStack or Colima.
- **Free host ports:** 80 (ingress) and 5005 (local registry `localdev-registry`). Check with `ss -ltn`. On Linux, binding port 80 works because Docker does it as root.
- **About 4 GB of RAM free for Docker.**
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
k9s                  # uses ./.kubeconfig via mise env; ~/.kube/config is never touched
```

`manifests/` is mounted live into k3s's auto-deploy directory, so saving a file there re-applies it within seconds.

```
manifests/
  00-namespaces.yaml
  10-platform/  10-1-traefik-config  10-2-oauth2-proxy
  20-infra/     20-1-headlamp  20-2-monitoring  20-3-logging
  30-tools/     30-1-registry-ui  30-2-postgres
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

## Persistent data (`runtime.data/`)

`runtime.data/` (gitignored) sits next to this readme and survives `destroy` and `recreate`:
- `runtime.data/postgres`: Postgres data. It's mounted into the server node at `/runtime.data`, and the pod uses it as a hostPath.
- `runtime.data/registry`: images in the k3d registry, so `localhost:5005` keeps them across rebuilds.

`mise run clean` deletes it all. It refuses to run while the cluster exists, shows sizes, and asks you to type `clean`. Pass `--yes` to skip the prompt. The files are owned by container users (root, and postgres uid 999), so it deletes them through a throwaway container rather than `sudo`.

## Postgres

Postgres 18 is a single-instance StatefulSet in `tools` (`manifests/30-tools/30-2-postgres.yaml`), with no operator.
- **Credentials** (fixed, dev only): user `app`, password `localdev`, database `app`.
- **In-cluster connection:** host `postgres-rw.tools.svc:5432`. The Secret `postgres-app` has the keys `username`, `password`, `dbname`, `host`, `port`, `uri` and `jdbc-uri`.
- **From your desktop:** run `mise run pg:forward`, then connect to `postgresql://app:localdev@localhost:5432/app`.
- **Web UI:** `/pgweb/`.
- **Later move to CloudNativePG:** the Service and Secret names match what CloudNativePG creates for a `Cluster` named `postgres`, so apps won't need changes. Data moves with `pg_dump` and `pg_restore`.

## Apps (`apps/`)

Each `apps/<name>/` directory holds a Dockerfile. `mise run build <name>` builds it as `localhost:5005/<name>:latest`, pushes it to the k3d registry and restarts any deployment labelled `app=<name>`. Inside the cluster, the image is pulled as `localdev-registry:5005/<name>:latest`. `mise run create` builds every app.

- **cnav** (`apps/cnav`, deployed by `manifests/50-apps/50-2-cnav.yaml`): a small Python server (stdlib plus PyYAML) serving the navigator page.
  - **Cards (left):** from `cards.yaml`. Fields: `component`, `icon` (an emoji), `link`, and an optional `description`.
  - **Status column (right):** from `status.yaml`. It's organised as groups, then items, then checks. Check kinds are `Deployment`, `StatefulSet` and `DaemonSet` (with `namespace` and `name`), and `HelmChart` (with `name`). An item's light is the worst of its checks, and a group's light is the worst of its items. Clicking a row opens the first workload in Headlamp. The column refreshes every 10 seconds.
    - **Green:** all replicas ready.
    - **Amber:** partially ready, a rollout in progress, starting up (under 2 minutes), restarted in the last 10 minutes, Helm still installing, or scaled to 0.
    - **Red:** no ready replicas, CrashLoopBackOff, an image pull error, a config error, stuck Pending (over 2 minutes), a failed Helm install, or the workload not found. The reason is shown under the row.
  - **Editing:** both files live in the `cnav-config` ConfigMap. Edit them there and the page picks up the change within about a minute, with no rebuild. The copies in `apps/cnav/` are only defaults for running the image on its own, where the status column shows "unavailable".
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
