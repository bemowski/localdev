# localdev

localdev is an ephemeral local Kubernetes cluster ([k3d](https://k3d.io)) for development. Everything is
served on `http://localhost/<app>`, behind single sign-on, and the whole cluster is defined by the files in
`kube/localdev`: edit a manifest and it is applied within seconds.

- **[Getting started](getting-started.md)**: create a cluster, sign in, switch profiles.
- **[Cluster layout](cluster-layout.md)**: the layers (platform, infra, tools, apps) and how files become a running cluster.
- **[Components](components.md)**: the optional tools, what a component folder holds, and how to add one.

!!! tip "Where things are"
    Start at [http://localhost/](http://localhost/), the cluster navigator: a card for every app and a live
    status column. These docs are built from `kube/localdev/docs/content/*.md`.
