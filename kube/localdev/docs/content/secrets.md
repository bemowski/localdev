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
| `localdev-admin` | `username`, `password` | `auth/oauth2-proxy-htpasswd` (bcrypt line, built by ESO), `monitoring/grafana-admin`, `keycloak/keycloak-admin` |
| `postgres-app` | `username`, `password`, `dbname` | `pg/postgres-app` (+ `host`, `port`, `uri`, `jdbc-uri`), `keycloak/keycloak-db` |
| `keycloak-db` | `username`, `password` | `keycloak/keycloak-db` |
| *your entries* | | your components' ExternalSecrets |

## Where the values come from

The store's values are merged from these files, later ones winning:

| file | committed? | holds |
|---|---|---|
| `secrets.defaults.toml` | yes | the platform entries: their keys, fixed values (`username = "admin"`), and `""` for **required** values |
| `components/<c>/secrets.defaults.toml` | yes | a component's fixed dev values (Postgres `app` / `localdev`) |
| **`secrets.store.toml`** | **no** (gitignored) | your real secrets and any overrides |

```toml
# kube/localdev/secrets.store.toml
[sso]
google-client-id = "1234-abc.apps.googleusercontent.com"
google-client-secret = "GOCSPX-..."
cookie-secret = "..."            # openssl rand -base64 32 | tr -- '+/' '-_'

[localdev-admin]
password = "..."

[myapp-api]                      # your own entry
stripe-key = "sk_test_..."
```

```bash
mise run secrets:sync      # merge the files into the store; ESO and Reloader take it from there
mise run secrets:list      # entries and keys (no values), where each comes from, ExternalSecret status
```

- `cluster:create` checks that every required value is set before it starts, and syncs after.
- Entries removed from the files are removed from the store.
- To share values with a team without committing them in plain text, encrypt the file
  (`mise run encrypt secrets.store.toml`) and commit `secrets.store.toml.asc`; it is decrypted
  automatically when it is newer than your copy.
- **Coming from `secrets.env`?** `mise run secrets:migrate` writes `secrets.store.toml` from it once
  (it never overwrites or deletes anything). Then encrypt the new file, and delete `secrets.env` and
  `secrets.env.asc` when you're happy.

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

!!! note "Postgres passwords"
    Postgres uses `postgres-app.password` only when it first initializes its data
    (`runtime.data/postgres`). Changing it later updates the Secret, not the database.
