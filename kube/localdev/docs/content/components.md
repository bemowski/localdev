# Components

A component is an optional, self-contained piece of the cluster: everything it needs lives in
one folder, `components/<name>/` in this repo, or a folder in your own project registered in your
`local.toml` (see [Building a component](building-a-component.md)). Profiles choose which components run.

| component | what | in profiles |
|---|---|---|
| cnav | cluster navigator at `/` | all (always on, infra layer) |
| external-secrets | External Secrets Operator, Reloader, the `localdev` secret store | all (always on, infra layer) |
| usage-agent | sends this cluster's usage events (created, heartbeat, profile changed, app deployed) to a usage server; spools when there's none. External: `kube/components/usage/cluster-agent` | all (always on, infra layer) |
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
  secrets.defaults.toml  optional: the component's entries in the secret store (fixed dev values)
  ...            anything else the component owns: examples, config-as-code, tooling
                 (e.g. knative/examples/, keycloak/realms/ + realmtool.py)
```

- **Images** are pushed to the local registry as `localhost:5005/<name>:latest`, and pulled in the cluster
  as `localdev-registry:5005/<name>:latest`. After a build, deployments labelled `app=<name>` restart.
- **`build.sh`** runs from the component's folder with `IMAGE`, `REGISTRY`, `COMPONENT_DIR` and
  `LOCALDEV_ROOT` set, and must build `$IMAGE` (see [the build hook](building-a-component.md#the-build-hook)).
  The docs component uses it to copy the top-level `docs/` folder into a staged build context; its
  multi-stage Dockerfile then renders the markdown with MkDocs and serves the result with nginx.
- `cluster:create` builds every enabled component that has an image; `cluster:profile` builds the ones it adds.

## Metadata (`profiles.toml`)

Optional, under `[components.<name>]`:

- `requires = [...]`: other components it needs, added automatically (keycloak requires postgres).
- `layer = "..."`: where its manifests deploy: `platform`, `infra`, `tools` (default) or `apps`.
- `always = true`: part of every profile (cnav).
- `path = "..."`: only in `local.toml`, for an external component's folder.

## Adding a component

See [Building a component](building-a-component.md): the folder, a manifest template, build hook
recipes (Maven, Gradle, Go, a Dockerfile elsewhere), and registering it from your own project.
To show it in cnav, add a card and a status row tagged `tool: <name>` to cnav's ConfigMap
(`components/cnav/manifests/20-7-cnav.yaml`); they hide themselves when the component isn't installed.
