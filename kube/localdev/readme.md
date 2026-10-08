# kube/localdev

Ephemeral local Kubernetes (k3d) with path-based routing on `http://localhost/<app>`, behind Google SSO.

| path | app |
|---|---|
| `/whoami` | SSO test app. It echoes the request headers, including `X-Auth-Request-Email`. |
| `/headlamp` | Kubernetes UI. No token login: Headlamp acts as its own cluster-admin account, so the SSO allow-list is the only gate. |
| `/oauth2/*` | oauth2-proxy sign-in and callback |

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
k9s                  # uses ./.kubeconfig via mise env; ~/.kube/config is never touched
```

`manifests/` is mounted live into k3s's auto-deploy directory, so saving a file there re-applies it within seconds.

**Allow-lists.** Each app has its own `sso` forwardAuth Middleware with `?allowed_emails=a@x.com,b@y.com`. Google accounts must also be listed as test users on the consent screen (below).

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
