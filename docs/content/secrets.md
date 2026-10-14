# Secrets

Every secret localdev uses is **mastered in one secret store** and **synced to where it is needed by
the [External Secrets Operator](https://external-secrets.io)** (ESO), as in most production
clusters. Nothing writes Secrets into namespaces by hand, and no manifest holds a secret value.

- The store is a `ClusterSecretStore` named **`localdev`**. Every consumer references that name.
- Each consumer owns an **ExternalSecret** ("I need key X of entry Y, as Secret Z") next to its own
  manifests; ESO creates and updates the Secret.
- **[Reloader](https://github.com/stakater/Reloader)** rolls the pods that use a Secret when it changes
  (workloads opt in with the annotation `reloader.stakater.com/auto: "true"`).

Today the store is **local**: no cloud account needed. Moving it to a cloud secret manager later
(most likely Google Secret Manager) changes the store's definition, and nothing else.

## What's in the store

| entry | keys | consumers (ExternalSecret → Secret) |
|---|---|---|
| `sso` | `google-client-id`, `google-client-secret`, `cookie-secret` | `auth/oauth2-proxy` |
| `sso-access` | `emails`, `domains` (lists) | `auth/oauth2-proxy-access`: who may sign in with Google |
| `localdev-admin` | `username`, `password` | `auth/oauth2-proxy-htpasswd` (bcrypt line, built by ESO), `monitoring/grafana-admin`, `keycloak/keycloak-admin` |
| `postgres-app` | `username`, `password`, `dbname` | `postgres/postgres-app` (+ `host`, `port`, `uri`, `jdbc-uri`), `keycloak/keycloak-db` |
| `keycloak-db` | `username`, `password` | `keycloak/keycloak-db` |
| `api-<name>` | `api-key` (and any other `api-key*`) | read directly by api-auth: the keys for `http://api.localhost/<name>/` (e.g. `api-whoami`) |
| *your entries* | | your components' ExternalSecrets |

## Who can sign in

oauth2-proxy lets a Google account in when it matches the `[sso-access]` entry (the local `admin`
user is not affected). Set it in `secrets.store.toml`, then `mise run secrets:sync`:

```toml
[sso-access]
emails = ["you@gmail.com", "teammate@gmail.com"]   # exact addresses
domains = ["example.com"]                          # anyone @example.com
```

| `domains` value | lets in | doesn't let in |
|---|---|---|
| `"example.com"` | `ann@example.com` | `ann@eng.example.com` |
| `".example.com"` (or `"*.example.com"`) | `ann@eng.example.com`, `bo@a.b.example.com` | `ann@example.com` (list both for both) |
| `"*"` | any Google account | — |

- An account gets in if it matches **either** list. With both empty, nobody can sign in with Google.
- The lists are stored one item per line. ESO turns them into Secret `auth/oauth2-proxy-access`:
  `emails` becomes oauth2-proxy's authenticated-emails file, and `domains`, comma-joined, its
  `--email-domain` flag. Reloader rolls oauth2-proxy when that Secret changes.
- While the Google consent screen is in **Testing** mode, Google itself only lets its listed test
  users sign in, so a domain here doesn't open the door to anyone else until the app is published.
  Be careful with `"*"` on a published app: every Google account in the world would get in.

## Where the values come from

The store's values are merged from these files, later ones winning:

| file | committed? | holds |
|---|---|---|
| `secrets.defaults.toml` | yes | the platform entries: their keys and dev defaults (`admin` / `admin`, placeholder Google client): enough to run with Google sign-in off |
| `components/<c>/secrets.defaults.toml` | yes | a component's fixed dev values (Postgres `app` / `localdev`) |
| **`secrets.store.toml`** | **no** (gitignored) | your real secrets and any overrides |

```toml
# kube/localdev/secrets.store.toml
[sso]
google-client-id = "1234-abc.apps.googleusercontent.com"
google-client-secret = "GOCSPX-..."
cookie-secret = "..."            # openssl rand -base64 32 | tr -- '+/' '-_'

[sso-access]                     # who may sign in with Google (lists)
emails = ["you@gmail.com"]
domains = []                     # e.g. ["example.com"]

[localdev-admin]
password = "..."

[myapp-api]                      # your own entry
stripe-key = "sk_test_..."
```

```bash
mise run secrets:sync      # merge the files into the store; ESO and Reloader take it from there
mise run secrets:list      # entries and keys (no values), where each comes from, ExternalSecret status
```

- `secrets.store.toml` is optional: without it you sign in as `admin` / `admin` and the Google button
  fails until you set a real `[sso]` client. `secrets:check` says so.
- `cluster:create` checks that every required value (`""` in a defaults file) is set before it
  starts, and syncs after.
- Entries removed from the files are removed from the store.
- To share values with a team without committing them in plain text, encrypt the file
  (`mise run encrypt secrets.store.toml`) and commit `secrets.store.toml.asc`. Nothing decrypts it
  automatically: decrypt it yourself. `secrets:check` only notes when it's there but not decrypted,
  or newer than your copy.
- **Coming from `secrets.env`?** `mise run secrets:migrate` writes `secrets.store.toml` from it once
  (it never overwrites or deletes anything). Then encrypt the new file, and delete `secrets.env` and
  `secrets.env.asc` when you're happy.

!!! tip "Looking up a password"
    The cluster navigator's **Credentials** tab (next to **Ports**) shows the store's logins, masked,
    with reveal and copy buttons. A store entry with a `password` key appears there automatically
    while its component is installed; for a nicer row (a link, connection URLs), add it to
    `credentials.yaml` in cnav's ConfigMap.

## Using a secret in a component

Add an entry to `secrets.store.toml` (or, for fixed dev values, a `secrets.defaults.toml` in your
component), then an ExternalSecret next to your manifests:

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: myapp-api
  namespace: myapp
spec:
  refreshInterval: 1h
  secretStoreRef: {kind: ClusterSecretStore, name: localdev}
  target: {name: myapp-api}            # the Secret ESO creates in this namespace
  data:
    - secretKey: STRIPE_KEY
      remoteRef: {key: myapp-api, property: stripe-key}
```

and in the Deployment:

```yaml
metadata:
  annotations: {reloader.stakater.com/auto: "true"}   # roll when the Secret changes
spec:
  template:
    spec:
      containers:
        - name: myapp
          envFrom:
            - secretRef: {name: myapp-api}
```

ESO can also build values with a template, e.g. a connection string from several keys
(see `components/postgres/manifests/30-1-postgres-secrets.yaml`).

!!! tip "Keep ExternalSecrets in their own file"
    k3s applies a manifest file as a whole and retries it until every object in it is accepted. On a
    fresh cluster, ExternalSecrets wait for ESO to install, so put them in a separate file from your
    workloads (`30-1-postgres-secrets.yaml` next to `30-2-postgres.yaml`).

## Moving to a cloud store

Because every consumer asks for `ClusterSecretStore localdev`, moving to Google Secret Manager is a
change to that one object (`components/external-secrets/manifests/20-8-external-secrets.yaml`):

```yaml
apiVersion: external-secrets.io/v1
kind: ClusterSecretStore
metadata:
  name: localdev
spec:
  provider:
    gcpsm:
      projectID: my-dev-project
      auth:
        secretRef:
          secretAccessKeySecretRef: {name: gcp-sm, key: key.json, namespace: external-secrets}
```

with each entry stored as a Google secret of the same name, holding a JSON object with the same keys
(`sso` = `{"google-client-id": "...", ...}`). The ExternalSecrets' `key` / `property` references then
work unchanged. The credentials Secret (`gcp-sm`) is the one thing still created locally:

```bash
kubectl -n external-secrets create secret generic gcp-sm --from-file=key.json=<service-account-key.json>
```

AWS Secrets Manager and Vault work the same way (provider `aws` with access keys, or `vault` with a
token); see the ESO provider docs. Making the store's backing a configurable choice (local or Google)
is the next step on the roadmap.

## Reading GCP Secret Manager as yourself

Next to `localdev`, a second store, `dematicnonprod.localdev`, reads Google Secret Manager (project
`LOCALDEV_GCP_PROJECT`, set in `.mise.toml`: DematicNonProd/localdev) with your own gcloud login, not a
service account key. Store names can't contain `/` or capitals, so the display name is an annotation.

**`cluster:create` turns it on by itself** when it can: if you've run
`gcloud auth application-default login` and can read the project's secrets, the cluster comes up with
the GCP store and **Google sign-in works**: oauth2-proxy uses the OAuth client stored in GCP
(`localdev-sso-oauth`). If not (no gcloud, no login, an expired one, no access), it says why and carries
on with local sign-in only (`admin`). To turn it on later, or after `gcloud auth application-default login`:

```bash
mise run secrets:gcp-login     # prompts for the gcloud login if you have none; fails loudly
```

To try the cluster as someone without GCP access, set `LOCALDEV_GCP=off`
(`LOCALDEV_GCP=off mise run cluster:create`, or in `.mise.local.toml` `[env]`).

How it works: the task copies your Application Default Credentials
(`~/.config/gcloud/application_default_credentials.json`) into Secret `external-secrets/gcp-adc`, which
the ESO controller mounts as `GOOGLE_APPLICATION_CREDENTIALS`. A `gcpsm` store with no `auth` block uses
those. (ESO's `secretRef` auth only takes service account keys.) It then applies
`components/external-secrets/gcp-store.yaml.tmpl`: the store, plus ExternalSecret
`auth/oauth2-proxy-google`. oauth2-proxy loads `envFrom` Secret `oauth2-proxy` (localdev store) and then
the optional `oauth2-proxy-google`, so the GCP client wins when it exists. The cookie secret, the admin
login and the `[sso-access]` allow-list stay in the localdev store.

Your own components can read from it too:

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata: {name: myapp-api, namespace: myapp}
spec:
  secretStoreRef: {kind: ClusterSecretStore, name: dematicnonprod.localdev}
  data:
    - {secretKey: api-key, remoteRef: {key: myapp-api-key}}   # Google secret name (latest version)
```

That only works for developers with GCP access. Keep a `localdev`-store fallback for the others.

- You read what your IAM grants: `secretmanager.versions.access` on the secrets.
- The credential is controller-wide: any `gcpsm` store without `auth` in this cluster reads as you.
  It's a long-lived refresh token, so keep it to your own local cluster.
- Your ADC needs a quota project (`gcloud auth application-default set-quota-project <project>`).
- If your org's session policy expires the login, the store turns not-ready: run
  `gcloud auth application-default login` and `mise run secrets:gcp-login` again.

!!! note "Postgres passwords"
    Postgres uses `postgres-app.password` only when it first initializes its data
    (`runtime.data/postgres`). Changing it later updates the Secret, not the database.
