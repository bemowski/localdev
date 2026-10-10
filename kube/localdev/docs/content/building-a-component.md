# Building a component

This page hooks **your app** into localdev: a small component folder with a manifest and a build
hook, either inside this repo or, more usually, inside your own project.

## Where the folder lives

=== "In your project (external)"

    Keep the component next to your code, e.g. `~/src/myapp/localdev/`, and register it in your
    **`local.toml`** (gitignored, next to `profiles.toml` in `kube/localdev`):

    ```toml
    default = "myapp"                  # optional: your default profile

    [components.myapp]
    path = "~/src/myapp/localdev"      # or relative to kube/localdev
    layer = "apps"                     # platform|infra|tools|apps
    requires = ["postgres", "nats"]    # added automatically

    [profiles]
    myapp = { description = "myapp + tools", components = ["myapp", "docs"] }
    ```

    Then `mise run cluster:profile myapp` (or `cluster:create myapp`). `mise run cluster:profile`
    lists external components and how they are mounted.

=== "In this repo"

    Create `kube/localdev/components/<name>/` and add the name to the profiles you want in
    `profiles.toml`. Good for shared tools everyone uses.

### Workspace: linked or copied

The cluster can only read host folders that are mounted into it. `cluster:create` mounts the
**workspace**, read-only: by default the folder that holds this repo, so its sibling projects
(override with `LOCALDEV_WORKSPACE`, e.g. in a gitignored `kube/localdev/.mise.local.toml`).

- **Inside the workspace:** the component's `manifests/` is linked. Save a manifest and it is applied.
- **Outside it:** the manifests are **copied** when the profile is applied. After editing them, run
  `mise run cluster:profile` again. Moving the project, or changing the workspace and running
  `mise run cluster:recreate`, makes it live.

Grafana dashboards of external components are always copied.

## The folder

```
myapp/localdev/
  manifests/50-1-myapp.yaml    what to deploy (required)
  build.sh                     how to build the image (or a docker/ folder with a Dockerfile)
  dashboards/*.json            Grafana dashboards (optional)
```

## The manifest

One file can hold everything. Name it `<layer number>-<n>-<name>.yaml`, unique across the cluster
(see [manifest rules](cluster-layout.md#manifest-rules)).

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: myapp
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: myapp
  namespace: myapp
  # app: `mise run build myapp` restarts deployments with this label
  # localdev/component: cnav's status column shows it (see below)
  labels: {app: myapp, localdev/component: myapp}
spec:
  selector:
    matchLabels: {app: myapp}
  template:
    metadata:
      labels: {app: myapp}
    spec:
      containers:
        - name: myapp
          image: localdev-registry:5005/myapp:latest
          imagePullPolicy: Always    # pick up each rebuilt :latest
          ports: [{containerPort: 8080}]
          readinessProbe: {httpGet: {path: /myapp/health, port: 8080}}
---
apiVersion: v1
kind: Service
metadata:
  name: myapp
  namespace: myapp
spec:
  selector: {app: myapp}
  ports: [{port: 80, targetPort: 8080}]
---
# sign-in for this namespace (same for every component)
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: sso
  namespace: myapp
spec:
  forwardAuth:
    address: http://oauth2-proxy.auth.svc.cluster.local/
    authResponseHeaders: [X-Auth-Request-Email, X-Auth-Request-User]
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: myapp
  namespace: myapp
  annotations:
    traefik.ingress.kubernetes.io/router.middlewares: myapp-sso@kubernetescrd
spec:
  rules:
    - http:
        paths:
          - path: /myapp
            pathType: Prefix
            backend: {service: {name: myapp, port: {number: 80}}}
```

The app is served on `http://localhost/myapp/...`, so it should expect that path prefix (or add a
Traefik `stripPrefix` Middleware). The signed-in user arrives in the `X-Auth-Request-Email` header.

## The build hook

`mise run build <name>` builds the image, pushes it to the local registry and restarts the
deployments. With a `docker/` folder it simply runs `docker build` on it. With an executable
**`build.sh`**, it runs that instead:

- run **from the component's folder**;
- with `IMAGE` (`localhost:5005/<name>:latest`), `REGISTRY` (`localhost:5005`), `COMPONENT_DIR` and
  `LOCALDEV_ROOT` set;
- it must build `$IMAGE` (the task pushes it), and/or build and push other images itself.

Recipes, each a complete `build.sh` after `#!/bin/bash` and `set -euo pipefail`:

=== "Dockerfile elsewhere"

    ```bash
    docker build -t "$IMAGE" -f ../Dockerfile ..        # the project's own Dockerfile and context
    ```

=== "Maven + Jib"

    ```bash
    cd .. && ./mvnw -q compile jib:dockerBuild -Dimage="$IMAGE"
    ```

=== "Spring Boot buildpacks"

    ```bash
    cd .. && ./mvnw -q spring-boot:build-image -Dspring-boot.build-image.imageName="$IMAGE"
    ```

=== "Gradle + Jib"

    ```bash
    cd .. && ./gradlew -q jibDockerBuild --image="$IMAGE"
    ```

=== "Go + ko"

    ```bash
    cd .. && KO_DOCKER_REPO="$REGISTRY" ko build --bare --tags latest ./cmd/myapp   # pushes itself
    ```

=== "An image you already have"

    ```bash
    docker tag myapp:dev "$IMAGE"
    ```

`cluster:create` runs the build for every enabled component that has one, and `cluster:profile`
for each component it adds.

!!! note "Apple Silicon (arm64)"
    The cluster runs on the host's CPU architecture: arm64 on an Apple Silicon Mac, amd64 elsewhere.
    `docker build` builds for the host automatically, but some tools build amd64 unless told
    otherwise, and the pod then fails with `exec format error`. With **Jib**, set
    `jib.from.platforms` to `linux/arm64` (or both). With **ko**, pass `--platform=linux/arm64`, or
    `linux/amd64,linux/arm64`. Use multi-arch base images, and don't pin `--platform=linux/amd64`
    in a Dockerfile. See [Platform notes](platform-notes.md).

### A family of services

For a UI and a few APIs, one component can hold them all: one manifest per service (or one file),
and a `build.sh` that builds and pushes each image as `$REGISTRY/<name>-<service>:latest`. Label
every deployment `localdev/component: <name>` so `mise run build <name>` restarts them all.

### In cnav's status column

Label each workload (Deployment, StatefulSet, DaemonSet) `localdev/component: <name>`. cnav shows one
row per component, with all its labelled workloads as checks, in the `apps` group. Add
`localdev/status-group: <group>` to put the row in another group. Nothing to register: the row comes
and goes with the workloads. (`status.yaml` in cnav's ConfigMap still lists the built-in tools; a
component named by a `tool:` there isn't shown twice.)

## Secrets

Don't put secret values in the manifest: declare an **ExternalSecret** against the `localdev` store
(in its own file), keep the values in your gitignored `secrets.store.toml` (fixed dev values can go in
your component's `secrets.defaults.toml`), and annotate the Deployment for Reloader. See [Secrets](secrets.md).

## Databases and other setup

Postgres runs in namespace `pg` with fixed local credentials (`app` / `localdev`); from a component,
connect to `postgres-rw.pg.svc:5432`. NATS is `nats.nats.svc:4222`.

To create your own database today, use an **init container** that waits for Postgres and creates it
idempotently, as the Keycloak component does (`components/keycloak/manifests/30-6-keycloak.yaml`):

```yaml
      initContainers:
        - name: create-db
          image: postgres:18.6
          env:
            - {name: PGHOST, value: postgres-rw.pg.svc}
            - {name: PGUSER, value: app}
            - {name: PGPASSWORD, value: localdev}
            - {name: PGDATABASE, value: app}
          command: [sh, -c, 'until pg_isready -q; do sleep 2; done; psql -c "CREATE SCHEMA IF NOT EXISTS myapp"']
```

pgweb gets a bookmark for every database on the server automatically, within 30 seconds.

!!! note "Planned: `deploy.sh`"
    A component's `deploy.sh` will run after the component is installed (by `cluster:create` and
    `cluster:profile`), for setup that manifests express badly: schemas and migrations, a Keycloak
    realm, NATS streams. It must be idempotent, since it runs again on every install.

## Grafana: datasources and dashboards

### How a datasource gets into Grafana

A component can add its own Grafana datasource without touching Grafana's config: it publishes a
labelled Secret (or ConfigMap) and Grafana picks it up within seconds. Three pieces make that work:

1. **Grafana file provisioning** (core Grafana): Grafana reads datasource definitions from YAML files
   in `/etc/grafana/provisioning/datasources/`, and re-reads them when its reload API is called.
   [Grafana docs: Provision Grafana](https://grafana.com/docs/grafana/latest/administration/provisioning/#data-sources).
2. **A watcher sidecar** ([kiwigrid/k8s-sidecar](https://github.com/kiwigrid/k8s-sidecar)): the
   `grafana-sc-datasources` container in the Grafana pod watches namespace **`monitoring`** for
   ConfigMaps and Secrets labelled **`grafana_datasource: "1"`**, writes each one's data keys as files
   into that folder, and calls Grafana's reload API.
3. **The Grafana Helm chart** (part of kube-prometheus-stack, `manifests/20-infra/20-2-monitoring.yaml`)
   adds and wires up the sidecar (its `sidecar.datasources.*` values; on by default).

The object's lifetime is the datasource's lifetime: remove the component (`mise run cluster:profile`
without it) and the Secret goes, and so does the datasource.

### Example: a Postgres datasource for your database

The credentials stay in the [secret store](secrets.md): an **ExternalSecret** in `monitoring`
renders the provisioning file into a Secret with the label. This is what the usage component does
(`kube/components/usage/server/manifests/50-2-0-usage-secrets.yaml`).

```toml
# your component's secrets.defaults.toml: a read-only login (dev value)
[myapp-ro]
username = "myapp_ro"
password = "myapp_ro"
```

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: myapp-grafana-datasource
  namespace: monitoring                 # the only namespace the datasource sidecar watches
spec:
  refreshInterval: 1h
  secretStoreRef: {kind: ClusterSecretStore, name: localdev}
  target:
    name: myapp-grafana-datasource
    template:
      engineVersion: v2
      metadata:
        labels: {grafana_datasource: "1"}   # what the sidecar looks for
      data:
        myapp-datasource.yaml: |            # file name in Grafana's provisioning folder: keep it unique
          apiVersion: 1
          datasources:
            - name: MyApp
              uid: myapp-pg                 # stable: dashboards refer to the datasource by uid
              type: grafana-postgresql-datasource
              url: postgres-rw.pg.svc:5432
              user: {{ .username }}
              jsonData: {database: myapp, sslmode: disable, postgresVersion: 1500}
              secureJsonData: {password: "{{ .password }}"}
  data:
    - {secretKey: username, remoteRef: {key: myapp-ro, property: username}}
    - {secretKey: password, remoteRef: {key: myapp-ro, property: password}}
```

The login must exist in Postgres: create it in your `create-db` init container (see
[Databases](#databases-and-other-setup)) and give it read access, including to tables created later:

```sql
-- in database myapp, as the Postgres admin
GRANT USAGE ON SCHEMA public TO myapp_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO myapp_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE myapp IN SCHEMA public GRANT SELECT ON TABLES TO myapp_ro;
```

Nothing secret in the datasource (a JSON API with no auth, say)? A plain ConfigMap with the same
label and the same `data:` key works too, no ExternalSecret needed.

**Check it:** Grafana → Connections → Data sources shows **MyApp**; *Save & test* tries the login.
If it's missing, the sidecar logs each file it writes:

```bash
kubectl -n monitoring logs deploy/grafana -c grafana-sc-datasources | grep myapp
```

**Gotchas:**

- The Secret must be in `monitoring`; anywhere else it's ignored.
- Every datasource needs a unique `uid` and `name`, and the file name (the `data:` key) must not
  collide with another component's.
- Grafana expands `$VAR` and `${VAR}` in provisioning files as environment variables: a password
  containing `$` must be written `$$`.

### Dashboards

Put dashboard JSON in your component's `dashboards/` folder: they appear in Grafana's **tools**
folder while the component is in the profile. Point panels at your datasource by its `uid`
(`{"type": "grafana-postgresql-datasource", "uid": "myapp-pg"}`).

Saving UI edits back to the file:

- **In-repo component** (`components/<name>/`): `mise run grafana:export "<title>"` updates the file in place.
- **External component** (`local.toml` path): its dashboards are *copied* into the cluster, and
  `grafana:export` doesn't know your folder. It writes a new file under `kube/localdev/grafana/dashboards/`
  instead, which duplicates the uid (and a duplicate uid stops Grafana applying any dashboard file).
  Move that file over your component's copy right away, then `mise run cluster:profile <profile>`
  to copy it in again.
(The chart's dashboard sidecar also turns ConfigMaps labelled `grafana_dashboard: "1"`, in any
namespace, into dashboards, but the `dashboards/` folder is the localdev way: plain files, exported
from the UI.)

## Checklist

1. `manifests/` with a namespace, Deployment (`app: <name>`, `localdev/component: <name>`), Service, `sso` Middleware, Ingress.
2. `build.sh` (or `docker/`) producing `localdev-registry:5005/<name>:latest`.
3. Registered: `local.toml` (external) or `profiles.toml` (in this repo); `python3 profile.py check`.
4. `mise run cluster:profile <profile>`; the app is on `http://localhost/<name>/`.
5. Optional: a Grafana datasource (ExternalSecret in `monitoring`) and `dashboards/`.
