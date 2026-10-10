# Cluster layout

The cluster is built in **layers**, deployed in order. Each layer is a folder of manifests that k3s
applies on its own (its "auto-deploy" manifests folder): saving a file re-applies it within seconds.

| layer | always on | what |
|---|---|---|
| **10-platform** | yes | Traefik config (ingress), oauth2-proxy (single sign-on) |
| **20-infra** | yes | Headlamp, Prometheus / Alertmanager / Grafana, Loki + Alloy (logs), Tempo (traces), registry UI, cnav, External Secrets |
| **30-tools** | by profile | the optional components: Postgres, NATS, Knative, Keycloak, docs |
| **50-apps** | yes | your apps (whoami, the SSO test app, to start with) |

## Where the files are

```
manifests/<layer>/     always-on manifests (10-platform, 20-infra, 50-apps)
components/<name>/     one component (see Components)
grafana/dashboards/    always-on Grafana dashboards
docs/                  these docs (markdown)
profiles.toml          profiles: which components run
runtime.profile/       generated view of the active profile
runtime.data/          persistent data (postgres, nats, metrics, logs, traces, grafana, registry)
```

## The active profile: `runtime.profile/`

`profile.py` builds `runtime.profile/`, the view the cluster reads. It holds **links only**, so edits to
the real files stay live:

- `runtime.profile/manifests/` is mounted as k3s's manifests folder. It has a real folder per layer
  holding directory links: `core` for the layer's own files, and one per enabled component in that layer,
  e.g. `20-infra/cnav/20-7-cnav.yaml`, `30-tools/postgres/30-2-postgres.yaml`.
- `runtime.profile/dashboards/` is Grafana's dashboard folder, with file links.

## Routing and sign-in

Everything with a UI is an Ingress on a path of `http://localhost/`. Each namespace has an `sso`
Middleware that asks oauth2-proxy whether the request is signed in, so every app is behind the same
sign-in: Google (allow-listed accounts) or the local `admin` user.

## Manifest rules

- **Each file creates the namespace(s) it uses**, and HelmCharts set `createNamespace: true`, so a
  component brings, and takes away, its own namespaces. k3s retries a file until it applies.
- **Filenames must be unique across the whole tree.** k3s tracks each file as an AddOn named after its
  base name, cut at the first dot. Name files `<layer number>-<n>-<name>.yaml` (dashes, no dots).
  After renaming a file, `kubectl -n kube-system delete addon <old-name>` removes the old tracking entry.
- Manifests are `.yaml`, never `.json`: k3s applies every JSON file it sees.
- Removing an object from a file deletes it; **deleting a whole file does not** delete its objects.
- Changing a HelmChart's `valuesContent` runs a Helm upgrade.
- Changes to `k3d.yaml` need `mise run cluster:recreate`.
