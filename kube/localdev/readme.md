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
k9s                  # uses ./.kubeconfig via mise env; ~/.kube/config is never touched
```

`manifests/` is mounted live into k3s's auto-deploy directory, so saving a file there re-applies it within seconds.

```
manifests/
  00-namespaces.yaml
  10-platform/  10-1-traefik-config  10-2-oauth2-proxy
  20-infra/     20-1-headlamp  20-2-monitoring  20-3-logging
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
- Changes to `k3d.yaml` or `secrets.env` need `mise run recreate`.

**Allow-lists.** Each app has its own `sso` forwardAuth Middleware with `?allowed_emails=a@x.com,b@y.com`. Google accounts must also be listed as test users on the consent screen (below).

## Apps (`apps/`)

Each `apps/<name>/` directory holds a Dockerfile. `mise run build <name>` builds it as `localhost:5005/<name>:latest`, pushes it to the k3d registry and restarts any deployment labelled `app=<name>`. Inside the cluster, the image is pulled as `localdev-registry:5005/<name>:latest`. `mise run create` builds every app.

- **cnav** (`apps/cnav`, deployed by `manifests/50-apps/50-2-cnav.yaml`): nginx serving a page that renders cards from `cards.yaml`.
  - To change the cards, edit the `cnav-cards` ConfigMap in `manifests/50-apps/50-2-cnav.yaml`. The page updates within about a minute, with no rebuild.
  - Card fields: `component`, `icon` (an emoji), `link`, and an optional `description`.
  - The `cards.yaml` baked into the image is only the default, used when you run the image on its own.

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
