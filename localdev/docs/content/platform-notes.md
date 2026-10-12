# Platform notes

localdev was built on Linux. It also runs on **macOS with Docker Desktop or Rancher Desktop** and on
**Windows in WSL2 with Docker Engine inside the distro**; [Getting started](getting-started.md) has the setup
for each. This page collects what differs between them, and what to check when something doesn't work.

Items marked **(to confirm)** are expected to work but haven't been tried on that platform yet.

## Everywhere

- **Docker Hub rate limits.** The cluster node pulls most images itself, anonymously: your
  `docker login` on the host doesn't apply to it. On a shared or corporate network, the anonymous
  limit can run out, and pods sit in `ImagePullBackOff` with `toomanyrequests`. Wait, or switch networks.
- **Port already in use.** `cluster:create` fails if 80, 5005, 5432 or 4222 is taken, often by a
  local Postgres or web server. Find it with `ss -ltnp` (Linux, WSL) or
  `lsof -nP -iTCP -sTCP:LISTEN` (macOS).
- **`secrets.store.toml.asc`** is encrypted with a passphrase from your own `~/.ssh/id_ed25519`, so
  only you can decrypt it. A new developer creates their own `secrets.store.toml` (see [Secrets](secrets.md)).

## macOS

Docker Desktop and Rancher Desktop both run Docker in a Linux VM. Most of what follows applies to
either; the differences are in their own sections below.

- **CPU architecture.** Apple Silicon is arm64. Every image localdev deploys is published for both
  amd64 and arm64, and `mise run build` builds for the machine you're on, which is also what the
  cluster runs. When you write your own component, use multi-arch base images and **don't pin
  `--platform=linux/amd64`** in Dockerfiles: an amd64 image fails on an arm64 cluster with
  `exec format error`.
- **Clone under `~`.** Both share your home folder with their VM (Rancher Desktop shares nothing
  else). The cluster mounts the repo, its parent folder (the workspace) and `runtime.data/` from
  the host by path, so a clone outside the shared folders gives empty mounts and pods that can't
  find their files.
- **File ownership in `runtime.data/`** (to confirm). Postgres, NATS, Prometheus, Loki, Tempo and
  Grafana `chown` their data folders. This needs the **virtiofs** file sharing. If Postgres fails
  with `data directory ... has wrong ownership`, check that setting, then `mise run cluster:destroy`,
  `mise run cluster:clean` and `mise run cluster:create`.
- **The engine's own Kubernetes must be off.** It uses memory for a second cluster, and Rancher
  Desktop's also brings a Traefik that takes port 80.
- **`kubectl` version.** Both install their own `kubectl`. Activate mise last in `~/.zshrc`, so
  `which kubectl` points into mise's install folder.
- **Only one engine at a time.** With both installed, the `docker` CLI talks to whichever its
  current context names. Quit the one you don't use, and check with `docker context ls`
  (`desktop-linux` is Docker Desktop, `rancher-desktop` is Rancher Desktop); switch with
  `docker context use <name>`. A cluster created on one engine isn't visible from the other.
- **inotify limits** live in the VM's kernel. If pods log `too many open files`, check them with
  `docker run --rm alpine sysctl fs.inotify` (to confirm). Values of 1024 instances or more are fine.
- **bash 3.2.** The mise tasks run with macOS's `/bin/bash`, which is old. They avoid bash 4
  features and GNU-only flags. Keep it that way when adding tasks: no `declare -A`, `mapfile`,
  `${x,,}`, `xargs -d`, `sed -i` without a suffix, `readlink -f`, `date -d`, or `grep -P`.

### Docker Desktop

- **File sharing:** Settings → General → *Virtual Machine Options*: **VirtioFS**, with the Apple
  Virtualization framework or Docker VMM. Older installs may still be on gRPC FUSE or osxfs, which
  are slower and can break `chown`.
- **Shared folders:** `/Users`, `/Volumes`, `/private`, `/tmp` and `/var/folders` by default
  (Settings → Resources → File sharing). If you've trimmed that list, keep your clone's folder in it.
- **Memory:** Settings → Resources → Advanced. Docker Desktop's default is often below 6 GB; pods
  getting `OOMKilled` or evicted mean it needs more.
- **`kubectl`:** Docker Desktop links its own into `/usr/local/bin` (or `~/.docker/bin`, depending
  on Settings → Advanced). mise must still win in `PATH`.
- **Licence:** Docker Desktop needs a paid subscription for commercial use in companies with more
  than 250 employees or $10M in revenue. Rancher Desktop is free.
- `docker info` shows `Operating System: Docker Desktop` and `Name: docker-desktop`.

### Rancher Desktop

- **Container engine:** `dockerd (moby)`, not containerd. localdev needs the `docker` CLI and API.
- **File sharing:** Preferences → Virtual Machine → *Emulation* **VZ**, and *Volumes* **virtiofs**.
- **Shared folders:** only your home folder.
- **Kubernetes off:** its Traefik takes port 80.
- **`kubectl`:** Rancher Desktop puts its own `kubectl` and `helm` in `~/.rd/bin`, and adds that to
  `~/.zshrc`. Activate mise after those lines.
- `docker info` shows `Operating System: Rancher Desktop`.

## Windows (WSL2)

- **Docker Engine inside WSL, not Docker Desktop.** If Docker Desktop's WSL integration is on, it
  replaces the `docker` command in your distro, and its bind mounts and ports behave differently.
  `docker info` should show your distro's name under *Name* and `Ubuntu` under *Operating System*.
- **Clone inside WSL** (`~/...`), with the Linux `git`. A clone under `/mnt/c` breaks symlinks,
  executable bits and file ownership. A clone made with Windows git gets CRLF line endings; the
  repo's `.gitattributes` forces LF, but symlinks and permissions still break, so don't.
- **Docker doesn't start.** Without systemd (`/etc/wsl.conf`, `[boot] systemd=true`), nothing
  starts Docker when the distro boots. Check with `systemctl status docker`.
- **Reaching the cluster from Windows.** WSL forwards ports from the distro to Windows `localhost`,
  so the browser and the Google redirect (`http://localhost/oauth2/callback`) work. The tool ports
  bound to `127.0.0.1` (5432, 4222) should be reachable from Windows tools too (to confirm). If not,
  turn on mirrored networking in `%UserProfile%\.wslconfig` (`[wsl2]` then
  `networkingMode=mirrored`) and `wsl --shutdown`.
- **Port 80 is taken on the Windows side.** IIS, or another service using `http.sys`, can hold port
  80 and hide localdev. In PowerShell, `netstat -ano | findstr :80` shows the process.
- **Clock drift after sleep.** WSL's clock can fall behind after the PC sleeps. Google sign-in then
  fails with token time errors. Run `sudo hwclock -s` in WSL, or `wsl --shutdown`.
- **Memory.** WSL gets half the PC's memory by default. If pods get evicted or the PC swaps, set
  `memory=8GB` (or more) under `[wsl2]` in `.wslconfig`.

## Checking a new platform

When you set localdev up on a platform for the first time, run through these, and update the
**(to confirm)** items above with what you found:

1. `mise run cluster:create dev`, then open `http://localhost/` and sign in with Google and as `admin`.
2. `psql postgresql://app:localdev@localhost:5432/app -c 'select 1'` from the host (on WSL, also from a Windows tool).
3. `mise run cluster:profile full`, then `mise run cluster:profile dev`: tools come and go.
4. `mise run build cnav`: an image builds, pushes to `localhost:5005` and the page reloads.
5. `mise run cluster:recreate`: Postgres data survives.
6. Grafana → Explore → Loki shows logs, and no pod logs `too many open files`.
