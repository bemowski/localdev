# Getting started

## Prerequisites

localdev runs on **Linux**, **macOS** (Intel or Apple Silicon) and **Windows through WSL2**. Every
platform needs the same things:

- **Docker**, with about 6 GB of memory available to it.
- **[mise](https://mise.jdx.dev)**, activated in your shell.
- **A terminal:** a Linux shell, the macOS Terminal, or a WSL shell. Not PowerShell or `cmd`.
- **git**, **curl**, and **gpg** (gpg only to decrypt `secrets.store.toml.asc`).
- **Free host ports:** 80 (ingress), 5005 (image registry), and 5432 / 4222 (Postgres / NATS, on 127.0.0.1).

Set up your platform:

=== "Linux"

    1. **Docker Engine** ([install](https://docs.docker.com/engine/install/)). Add yourself to the
        `docker` group, then log out and back in:
        ```bash
        sudo usermod -aG docker "$USER"
        ```
    2. **Raise the inotify limits** once. Following every pod's logs (Alloy → Loki) needs more
        inotify instances than the default 128. Without this, pods log
        `failed to create fsnotify watcher: too many open files` instead of their real output.
        ```bash
        printf 'fs.inotify.max_user_instances=1024\nfs.inotify.max_user_watches=1048576\n' | sudo tee /etc/sysctl.d/99-localdev.conf
        sudo sysctl --system
        ```
    3. **mise:** `curl https://mise.run | sh`, then add `eval "$(~/.local/bin/mise activate bash)"`
        to `~/.bashrc` (or the `zsh` equivalent).

=== "macOS (Rancher Desktop)"

    1. **[Rancher Desktop](https://rancherdesktop.io/)**. In its Preferences:
        - **Container Engine:** `dockerd (moby)`, not containerd. localdev needs the `docker` CLI and API.
        - **Kubernetes:** turn it **off**. localdev runs its own cluster (k3d), and Rancher Desktop's
          Traefik would take port 80.
        - **Virtual Machine:** at least **6 GB** of memory and 4 CPUs. Under *Emulation*, pick
          **VZ**, and under *Volumes*, **virtiofs** (faster, and handles file ownership for
          `runtime.data`).
    2. **Keep your clone under your home folder** (e.g. `~/src/mono`). Rancher Desktop only shares
        `~` with its VM, and the cluster mounts the repo and its parent folder from there.
    3. **Tools:** `brew install mise gnupg` (curl and git ship with macOS; macOS 13 or newer is needed
        for `realpath`).
    4. **Shell:** macOS uses zsh. Add `eval "$(mise activate zsh)"` to `~/.zshrc`, **after** the
        lines Rancher Desktop adds for `~/.rd/bin`. Rancher Desktop ships its own `kubectl` and
        `helm`, and mise's pinned versions must come first in `PATH`. Check with `which kubectl`.
    5. **Check:** `docker info` shows `Operating System: Rancher Desktop`, and
        `docker run --rm hello-world` works.

    Apple Silicon needs nothing extra: every image localdev uses is published for arm64, and
    `mise run build` builds your images for the same architecture the cluster runs.

=== "Windows (WSL2)"

    localdev runs **inside** a WSL2 Linux distro (Ubuntu), with **Docker Engine installed in that
    distro**. Don't use Docker Desktop: if it's installed, turn off its WSL integration for this
    distro, so `docker` talks to the engine inside WSL.

    1. **WSL2 with Ubuntu:** in PowerShell, `wsl --install -d Ubuntu`. Then open the Ubuntu
        terminal; everything else happens there.
    2. **Enable systemd**, so Docker and the sysctl settings start with the distro. Add this to
        `/etc/wsl.conf`, then run `wsl --shutdown` from PowerShell and reopen Ubuntu:
        ```ini
        [boot]
        systemd=true
        ```
    3. **Docker Engine:** install it from Docker's apt repository
        ([Ubuntu instructions](https://docs.docker.com/engine/install/ubuntu/)), then
        `sudo usermod -aG docker "$USER"` and reopen the terminal.
    4. **Raise the inotify limits**, as on Linux:
        ```bash
        printf 'fs.inotify.max_user_instances=1024\nfs.inotify.max_user_watches=1048576\n' | sudo tee /etc/sysctl.d/99-localdev.conf
        sudo sysctl --system
        ```
    5. **Memory:** WSL gets half the PC's memory by default. If that's less than about 8 GB, raise
        it in `%UserProfile%\.wslconfig` (`[wsl2]` then `memory=8GB`), then `wsl --shutdown`.
    6. **Clone inside WSL**, e.g. `~/src/mono`, with the Linux `git`. **Never under `/mnt/c`**:
        the Windows filesystem breaks symlinks (`runtime.profile/`), executable bits (`build.sh`)
        and file ownership (Postgres's data folder), and it's slow.
    7. **mise:** `curl https://mise.run | sh`, then add `eval "$(~/.local/bin/mise activate bash)"`
        to `~/.bashrc`.

    Your Windows browser reaches the cluster at `http://localhost/` through WSL's localhost
    forwarding, and the Google sign-in redirect works the same way.

Then, on every platform:

- **`mise install`** in `kube/localdev` (run `mise trust` first if asked). This gets k3d, kubectl,
  kn, yq and Python.
- **`secrets.store.toml`** (or the encrypted `secrets.store.toml.asc`): the Google OAuth client, the
  cookie secret and the local admin password. See [Secrets](secrets.md), and the readme for the
  one-time Google setup.

If something doesn't work on your platform, see [Platform notes](platform-notes.md).

## Create a cluster

```bash
cd kube/localdev
mise run cluster:create          # default profile (dev), or: mise run cluster:create lite|dev|knative|full
```

This creates the k3d cluster, loads the secrets, deploys everything and builds the local images
(cnav, docs). The first run takes a few minutes while images download.

Then open [http://localhost/](http://localhost/) and sign in, either with Google (accounts on the
allow-list) or as `admin` with the `localdev-admin` password from `secrets.store.toml`.

Need a password for something that isn't behind SSO (Grafana's admin form, the Keycloak console,
Postgres from a SQL client)? The navigator's **Credentials** tab, next to **Ports**, lists them masked,
with a button to reveal or copy each one.

## Profiles

A profile chooses which optional tools run. Platform, infra and apps always run.

| profile | tools | |
|---|---|---|
| `lite` | none | SSO, observability, registry, cnav |
| `dev` (default) | postgres, nats, docs | app development |
| `knative` | postgres, nats, knative, docs | serverless and eventing |
| `auth` | postgres, keycloak, docs | identity (Keycloak) work |
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

The **Ports** card in the navigator shows these live. Next to it, **Events** lists what happened in the
cluster over the last 72 hours (deploys, crashes, failing probes, image pull errors). Tick **all** to also
see the routine pod lifecycle.

Coming from docker compose? See [Moving from Docker Compose](from-docker-compose.md).

## Everyday commands

```bash
mise run cluster:status            # profile, nodes, helm jobs, ingresses
mise run build <component>         # rebuild a component's image (cnav, docs) and restart it
mise run secrets:sync              # reload secrets.store.toml into the secret store
mise run cluster:recreate          # destroy + create (runtime.data is kept: data, metrics, logs, traces)
mise run cluster:destroy           # delete the cluster
mise run cluster:clean             # delete runtime.data (cluster must be destroyed)
```
