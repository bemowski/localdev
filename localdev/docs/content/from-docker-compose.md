# Moving from Docker Compose

Most projects arrive here with a `docker-compose.yml` that runs a few tools (Postgres, NATS, ...) and
maybe the app itself. Move in two stages: first the tools, then (when you want to) the app.
Stage 2 is where the real gain is: your app starts running on the same Kubernetes constructs it
will meet in production (see [Kubernetes, earlier in the dev cycle](concepts.md#kubernetes-earlier-in-the-dev-cycle)).

## Stage 1: keep your app in the IDE, use localdev's tools

This is the cheapest step, and often all you need. Run the app as you do today (IDE, `go run`,
`mvn spring-boot:run`), and point it at localdev instead of compose:

| tool | compose (typical) | localdev |
|---|---|---|
| Postgres | `localhost:5432` | `postgresql://app:localdev@localhost:5432/app` |
| NATS | `localhost:4222` | `nats://localhost:4222` |

The host ports are the same, so connection strings rarely change. Then:

1. `mise run cluster:create` (the `dev` profile has Postgres and NATS).
2. Remove those services from your compose file (or stop them: the ports must be free).
3. You also get a database UI (`/pgweb`), a NATS UI (`/nats`), metrics and logs in Grafana.

Postgres data lives in `kube/localdev/runtime.data/postgres` and survives cluster rebuilds.

## Stage 2: run your app in the cluster

Turn the app into a [component](building-a-component.md): a small folder with its manifests and a
`build.sh` that calls your existing build. It can live in your own repo.

### Mapping compose to localdev

| compose | localdev |
|---|---|
| `services.<name>` | a Deployment (plus a Service) in your component's manifest |
| `image:` | `localdev-registry:5005/<name>:latest`, pushed by `mise run build <name>` |
| `build:` | the component's `docker/` folder, or its `build.sh` (Maven, Gradle, a Dockerfile elsewhere) |
| `ports:` (a UI or HTTP API) | an Ingress on `http://localhost/<name>`, behind sign-in |
| `ports:` (a raw TCP port) | a LoadBalancer Service + a `127.0.0.1:<port>` mapping in `k3d.yaml` |
| `environment:` | `env:` on the container; shared values in a ConfigMap |
| `env_file:` / secrets | a Secret (`kubectl create secret generic ... --from-env-file`) |
| `volumes:` (data) | a `hostPath` under `/runtime.data/<name>` (kept across rebuilds), or `emptyDir` |
| `depends_on:` | not needed: Kubernetes restarts until dependencies answer. For order, an init container that waits |
| `healthcheck:` | `readinessProbe` / `livenessProbe` |
| networks, service names | DNS: `<service>.<namespace>.svc`, e.g. `postgres-rw.postgres.svc:5432`, `nats.nats.svc:4222` |
| `profiles:` | `profiles.toml` (or your `local.toml`) |
| `docker compose up` / `down` | `mise run cluster:create` / `cluster:destroy` (or `cluster:profile` to add/remove) |
| `docker compose build` | `mise run build <name>` |
| `docker compose logs -f` | Grafana Logs dashboard, `kubectl logs -f`, or k9s |
| `docker compose ps` | cnav's status column, `kubectl get pods -A`, k9s |
| `docker compose exec` | `kubectl exec -it deploy/<name> -n <ns> -- sh` |

### Example

A compose service:

```yaml
services:
  api:
    build: .
    ports: ["8080:8080"]
    environment:
      DB_URL: postgres://app:localdev@postgres:5432/app
    depends_on: [postgres]
```

becomes, in your component's `manifests/50-1-api.yaml` (namespace, Service and Ingress as in
[Building a component](building-a-component.md#the-manifest)):

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  namespace: api
  labels: {app: api}
spec:
  selector:
    matchLabels: {app: api}
  template:
    metadata:
      labels: {app: api}
    spec:
      containers:
        - name: api
          image: localdev-registry:5005/api:latest
          imagePullPolicy: Always
          env:
            - {name: DB_URL, value: "postgres://app:localdev@postgres-rw.postgres.svc:5432/app"}
          readinessProbe: {httpGet: {path: /health, port: 8080}}
```

## What isn't there yet

- **Live reload** of code into a running container: today it is build, push, restart
  (`mise run build <name>`). Tilt or Skaffold on the local registry is on the [roadmap](concepts.md#roadmap).
- **Per-component setup scripts** (`deploy.sh`) for schemas, realms and streams: planned. Meanwhile,
  an init container can do it (see [Building a component](building-a-component.md#databases-and-other-setup)).
