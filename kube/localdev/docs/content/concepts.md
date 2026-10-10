# Concepts

## Who localdev is for

localdev is for **developers writing applications**: a Java, Go or Python service, or a family of
related microservices with a UI container and a few APIs. Today that work usually looks like this:

- **docker compose** runs the local tools: Postgres, NATS, maybe a cache or an identity provider.
- The apps are **packaged as containers**.
- Later, those published containers are **deployed to Kubernetes**.

localdev replaces the compose step with a **light, local Kubernetes**, so the tools and the apps run
locally the way they will run in production, behind the same kind of ingress, with the same manifests.

## Kubernetes, earlier in the dev cycle

With compose, Kubernetes shows up late: an app is written and tested against compose's model
(containers on one network, `depends_on`, published ports, `.env` files), and only meets Kubernetes
on its way to a shared environment. That is where the surprises surface: the app expects a database
on `localhost`, assumes start-up order, has no health endpoint, reads config from a file nobody
mounts, or doesn't work under a path prefix.

Developing on localdev moves those concerns **to the developer's desk, from the first day**. The
deployment and interaction constructs are the Kubernetes ones, so apps become Kubernetes-aware as a
matter of course:

| concern | compose habit | what localdev makes you use, locally |
|---|---|---|
| Describing a deployment | a compose service | Deployment, Service, Ingress manifests, close to what ships |
| Finding other services | container names on one network | DNS: `<service>.<namespace>.svc`, namespaces as boundaries |
| Start-up order | `depends_on` | readiness probes, retries, init containers: apps that tolerate dependencies being late |
| Health | optional `healthcheck` | readiness and liveness probes that decide traffic and restarts |
| Configuration and secrets | `.env` files, bind mounts | env from ConfigMaps and Secrets, mounted config files |
| Reaching the app | `localhost:<port>` per service | one ingress, path routing, sign-in in front, identity in headers |
| Images | built locally, used locally | pushed to a registry and pulled by the cluster, by tag |
| Change rollout | recreate the container | rolling restarts, pods replaced, crash loops visible as such |
| Observability | `docker logs` | logs, metrics and traces collected per namespace and app label |

None of this is extra work done "for Kubernetes later": it is how the app runs locally. The
manifests and habits built here carry over to the shared clusters, and problems that used to appear
at deployment time show up while the code is still being written.

## Goals

- **Only mise and docker.** Most developers already have both. mise installs everything else
  (k3d, kubectl, helm, k9s) at pinned versions, per directory.
- **Uncomplicated.** One command to create, one to switch profiles, one to rebuild an image. Files are
  the source of truth: save a manifest and it is applied.
- **Configurable.** Profiles choose the tools; a personal `local.toml` adds your own projects.
- **Fast.** A cluster starts in a few minutes the first time, and quickly after that. Data survives
  rebuilds.

## Vocabulary

**Cluster**
:   A single-node [k3d](https://k3d.io) cluster (k3s in docker), created and destroyed by mise tasks.
    Everything with a UI is served on `http://localhost/<path>`, behind sign-in.

**Layer**
:   The cluster is deployed in layers, in order: **platform** (ingress, sign-in), **infra**
    (observability, Headlamp, registry, cnav), **tools** (optional components), **apps**.
    See [Cluster layout](cluster-layout.md).

**Component**
:   A self-contained folder that adds something to the cluster: its manifests, and optionally its
    image build (`docker/` or `build.sh`) and Grafana dashboards. Postgres, NATS and these docs are
    components; so is your app once you hook it in. See [Components](components.md).

**Profile**
:   A named set of components (`profiles.toml`): `lite`, `dev`, `knative`, `full`, or your own.
    Switching profiles installs and uninstalls components on the running cluster.

**Registry**
:   A docker registry next to the cluster: push to `localhost:5005/<name>`, and the cluster pulls
    `localdev-registry:5005/<name>`. Images survive cluster rebuilds.

**Build hook**
:   A component's `build.sh`: how its image gets built when that isn't a plain `docker build` of its
    `docker/` folder, e.g. a Maven build, or a Dockerfile somewhere else in your project.

**External component**
:   A component folder that lives in **your project**, not in this repo, registered in your
    `local.toml`. See [Building a component](building-a-component.md).

**Workspace**
:   The host folder mounted into the cluster so external components can be followed live; by
    default, the folder holding this repo (so its sibling projects).

**Secret store**
:   Where apps' secrets come from. Apps declare ExternalSecrets; the External Secrets Operator
    fetches the values. Locally a store called `local` answers from your gitignored
    `secrets.store.toml`; cloud stores (GCP, AWS, Vault) can be added. See [Secrets](secrets.md).

**Deploy hook** *(planned)*
:   A component's `deploy.sh`, run after it is installed, to set up what manifests can't express
    well: a database schema, a Keycloak realm, NATS streams.

## The inner loop today

1. Edit code.
2. `mise run build <component>`: builds the image, pushes it to the local registry, restarts the
   component's deployments.
3. Check it at `http://localhost/<path>`, its logs in Grafana (Logs dashboard), its status in cnav.

Manifests need no step at all: saving the file applies it.

## Roadmap

- **`deploy.sh`** for per-component setup (schemas, realms, streams), run by `cluster:create` and
  `cluster:profile`.
- **Tilt or Skaffold** on top of the local registry, for a watch-and-redeploy inner loop.
- **cnav cards for external components**, declared by the component instead of in cnav's config.
