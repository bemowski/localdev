# Getting started

## Prerequisites

- **Docker**, and about 4 GB of RAM free for it.
- **[mise](https://mise.jdx.dev)**: run `mise install` in `kube/localdev` to get k3d, kubectl, kn and Python.
- **Free host ports:** 80 (ingress), 5005 (image registry), and 5432 / 4222 (Postgres / NATS, on 127.0.0.1).
- **`secrets.env`** (or the encrypted `secrets.env.asc`): the Google OAuth client and the local admin
  password. See the readme for the one-time Google setup.
- **Linux:** raise the inotify limits once (see the readme), or pods' logs fail with "too many open files".

## Create a cluster

```bash
cd kube/localdev
mise run cluster:create          # default profile (dev), or: mise run cluster:create lite|dev|knative|full
```

This creates the k3d cluster, loads the secrets, deploys everything and builds the local images
(cnav, docs). The first run takes a few minutes while images download.

Then open [http://localhost/](http://localhost/) and sign in, either with Google (accounts on the
allow-list) or as `admin` with the password from `secrets.env`.

## Profiles

A profile chooses which optional tools run. Platform, infra and apps always run.

| profile | tools | |
|---|---|---|
| `lite` | none | SSO, observability, registry, cnav |
| `dev` (default) | postgres, nats, docs | app development |
| `knative` | postgres, nats, knative, docs | serverless and eventing |
| `full` | postgres, nats, knative, keycloak, docs | everything |

```bash
mise run cluster:profile          # list profiles, * marks the current one
mise run cluster:profile full     # switch the running cluster: adds/removes tools live
```

Dropped tools are uninstalled; their data in `runtime.data/` is kept, so switching back restores it.

## From your desktop

| address | what |
|---|---|
| `http://localhost/` | everything with a UI, by path (`/grafana`, `/headlamp`, `/pgweb`, `/nats`, `/docs`, ...) |
| `postgresql://app:localdev@localhost:5432/app` | Postgres (when installed) |
| `nats://localhost:4222` | NATS (when installed) |
| `localhost:5005` | image registry: `docker push localhost:5005/<name>` |

The **Ports** card in the navigator shows these live.

## Everyday commands

```bash
mise run cluster:status            # profile, nodes, helm jobs, ingresses
mise run build <component>         # rebuild a component's image (cnav, docs) and restart it
mise run cluster:secrets           # reload secrets.env into the cluster
mise run cluster:recreate          # destroy + create (runtime.data is kept)
mise run cluster:destroy           # delete the cluster
mise run cluster:clean             # delete runtime.data (cluster must be destroyed)
```
