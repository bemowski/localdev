# Secrets

localdev runs the **[External Secrets Operator](https://external-secrets.io)** (ESO), as in most
production clusters: apps never hold secret values in their manifests. They declare an
**ExternalSecret** ("I need key X from store Y, as Secret Z"), and ESO fetches the value from a
**secret store** (GCP Secret Manager, AWS Secrets Manager, Vault, ...) and keeps a Kubernetes Secret
in sync with it.

Locally there is no cloud connection by default. Instead, a **local store** answers the same
ExternalSecrets, so apps use the same manifests here as in production.

## The local store

| piece | what |
|---|---|
| `ClusterSecretStore` **`local`** | the store apps reference; ESO's Kubernetes provider |
| namespace **`secret-store`** | where the values live: one Secret per entry |
| **`secrets.store.toml`** | your values, in `kube/localdev`, gitignored |
| `mise run secrets:sync` | loads the file into `secret-store` and refreshes ExternalSecrets |

```toml
# kube/localdev/secrets.store.toml: one table per secret
[myapp-db]                    # remoteRef.key
username = "app"              # remoteRef.property = "username"
password = "localdev"

[myapp-api]
stripe-key = "sk_test_..."
```

```bash
mise run secrets:sync     # file -> store; entries removed from the file are removed from the store
mise run secrets:list     # store names and keys (no values), stores, ExternalSecrets and their status
```

`cluster:create` and `cluster:secrets` run the sync too. To share values with a team without
committing them in plain text, encrypt the file (`mise run encrypt secrets.store.toml`) and commit
`secrets.store.toml.asc`; `secrets:sync` decrypts it when it is newer than your copy.

## Using a secret in a component

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: myapp-db
  namespace: myapp
spec:
  refreshInterval: 1h
  secretStoreRef: {kind: ClusterSecretStore, name: local}
  target: {name: myapp-db}           # the Secret ESO creates in this namespace
  data:
    - secretKey: DB_USER
      remoteRef: {key: myapp-db, property: username}
    - secretKey: DB_PASSWORD
      remoteRef: {key: myapp-db, property: password}
```

and in the Deployment's container:

```yaml
          envFrom:
            - secretRef: {name: myapp-db}
```

`secrets:sync` asks ESO to re-read at once, so a changed value lands in the Secret within seconds.
Pods read environment variables at start: restart them (`mise run build <name>`, or
`kubectl rollout restart`) to pick up a new value, or mount the Secret as a file instead.

## Same manifests as production

ExternalSecrets name their store. If production uses, say, a `ClusterSecretStore` called
`gcp-secrets`, give localdev a store with **that name** and the local spec, and your component's
ExternalSecrets don't change at all between environments. Add it to your component's manifests
(or your own `local.toml` component):

```yaml
apiVersion: external-secrets.io/v1
kind: ClusterSecretStore
metadata:
  name: gcp-secrets          # production's store name, answered locally
spec:
  provider:
    kubernetes:
      remoteNamespace: secret-store
      server:
        caProvider: {type: ConfigMap, name: kube-root-ca.crt, key: ca.crt, namespace: secret-store}
      auth:
        serviceAccount: {name: store-reader, namespace: secret-store}
```

Keys must then match production's secret names (`remoteRef.key`) in `secrets.store.toml`.

## Using a real cloud store

When you do want the real thing locally (shared dev secrets, a test project), add a store for the
provider next to `local`. Credentials go into a Secret from your gitignored config, never into git.
Sketches (see the ESO provider docs for all options):

=== "GCP Secret Manager"

    ```bash
    kubectl -n external-secrets create secret generic gcp-sm --from-file=key.json=<service-account-key.json>
    ```
    ```yaml
    apiVersion: external-secrets.io/v1
    kind: ClusterSecretStore
    metadata: {name: gcp-secrets}
    spec:
      provider:
        gcpsm:
          projectID: my-dev-project
          auth:
            secretRef:
              secretAccessKeySecretRef: {name: gcp-sm, key: key.json, namespace: external-secrets}
    ```

=== "AWS Secrets Manager"

    ```bash
    kubectl -n external-secrets create secret generic aws-sm \
        --from-literal=access-key=... --from-literal=secret-key=... # [--from-literal=session-token=...]
    ```
    ```yaml
    apiVersion: external-secrets.io/v1
    kind: ClusterSecretStore
    metadata: {name: aws-secrets}
    spec:
      provider:
        aws:
          service: SecretsManager
          region: us-east-1
          auth:
            secretRef:
              accessKeyIDSecretRef: {name: aws-sm, key: access-key, namespace: external-secrets}
              secretAccessKeySecretRef: {name: aws-sm, key: secret-key, namespace: external-secrets}
    ```

=== "Vault"

    ```bash
    kubectl -n external-secrets create secret generic vault-token --from-literal=token=...
    ```
    ```yaml
    apiVersion: external-secrets.io/v1
    kind: ClusterSecretStore
    metadata: {name: vault}
    spec:
      provider:
        vault:
          server: https://vault.example.com
          path: secret
          version: v2
          auth:
            tokenSecretRef: {name: vault-token, key: token, namespace: external-secrets}
    ```

Wiring one of these in as a configurable option is on the roadmap.

## What stays outside ESO

localdev's own bootstrap secrets (the Google sign-in client, the local admin password) come from
`secrets.env` and are loaded by `mise run cluster:secrets`: they are needed before anything else
runs, so they don't depend on ESO.
