# localdev

localdev is an ephemeral local Kubernetes cluster ([k3d](https://k3d.io)) for development. Everything is
served on `http://localhost/<app>`, behind single sign-on, and the whole cluster is defined by the files in
this repo: edit a manifest and it is applied within seconds.

It is meant to replace the docker compose files developers use to run local tools, with a light
Kubernetes that runs their apps the way production does. All it needs is mise and docker.

- **[Concepts](concepts.md)**: who it's for, the goals, and the vocabulary (layers, components, profiles).
- **[Getting started](getting-started.md)**: create a cluster, sign in, switch profiles.
- **[Moving from Docker Compose](from-docker-compose.md)**: use localdev's Postgres and NATS from your IDE, then run your app in the cluster.
- **[Cluster layout](cluster-layout.md)**: the layers (platform, infra, tools, apps) and how files become a running cluster.
- **[Components](components.md)**: the components that ship with localdev, and what a component folder holds.
- **[Building a component](building-a-component.md)**: hook your own app in, from its own repo and its own build.
- **[Secrets](secrets.md)**: External Secrets with a local store, the same manifests as production.

!!! tip "Where things are"
    Start at [http://localhost/](http://localhost/), the cluster navigator: a card for every app, a live
    status column, the exposed ports and recent cluster events. These docs are built from `docs/content/*.md` in the localdev repo.
