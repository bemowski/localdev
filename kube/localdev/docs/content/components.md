# Components

A component is an optional, self-contained piece of the cluster: everything it needs lives in
`components/<name>/`. Profiles (`profiles.toml`) choose which components run.

| component | what | in profiles |
|---|---|---|
| cnav | cluster navigator at `/` | all (always on, infra layer) |
| postgres | Postgres + pgweb (`/pgweb`) | dev, knative, full |
| nats | NATS + JetStream, NUI (`/nats`) | dev, knative, full |
| docs | these docs (`/docs`) | dev, knative, full |
| knative | Knative Serving + Eventing | knative, full |
| keycloak | Keycloak identity provider (`/keycloak`) | full |

## Anatomy

```
components/<name>/
  manifests/     required: Kubernetes manifests, deployed by k3s (namespace, workloads, Service, Ingress, sso Middleware)
  dashboards/    optional: Grafana dashboards, shown in Grafana's "tools" folder
  docker/        optional: application code + Dockerfile, built by `mise run build <name>`
  build.sh       optional: a custom image build, used instead of building docker/ directly
  ...            anything else the component owns: examples, config-as-code, tooling
                 (e.g. knative/examples/, keycloak/realms/ + realmtool.py)
```

- **Images** are pushed to the local registry as `localhost:5005/<name>:latest`, and pulled in the cluster
  as `localdev-registry:5005/<name>:latest`. After a build, deployments labelled `app=<name>` restart.
- **`build.sh`** is called with `IMAGE` and `LOCALDEV_ROOT` set and must build `$IMAGE`. The docs component
  uses it to copy the top-level `docs/` folder into a staged build context; its multi-stage Dockerfile then
  renders the markdown with MkDocs and serves the result with nginx.
- `cluster:create` builds every enabled component that has an image; `cluster:profile` builds the ones it adds.

## Metadata (`profiles.toml`)

Optional, under `[components.<name>]`:

- `requires = [...]`: other components it needs, added automatically (keycloak requires postgres).
- `layer = "..."`: where its manifests deploy: `platform`, `infra`, `tools` (default) or `apps`.
- `always = true`: part of every profile (cnav).

## Adding a component

1. Create `components/<name>/manifests/30-<n>-<name>.yaml` with its own namespace, workloads, and, for a
   UI, a Service, an `sso` Middleware and an Ingress on `/<name>`.
2. Optionally add `docker/` (and `build.sh`) for an image, and `dashboards/`.
3. Add it to the profiles you want in `profiles.toml`, then `mise run cluster:profile <profile>`.
4. Optionally add a card and a status row (tagged `tool: <name>`) to cnav's ConfigMap, in
   `components/cnav/manifests/20-7-cnav.yaml`; they hide themselves when the component isn't installed.
