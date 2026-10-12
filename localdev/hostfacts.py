"""Facts about this localdev cluster that pods can't see: who, which OS, which k3d, which role.

Prints key=value lines for `kubectl create configmap --from-env-file`; cluster:profile:sync
publishes them as ConfigMap kube-system/localdev-host (read by e.g. the usage cluster-agent).

  username          login name ($USER)
  cluster_type      localdev
  cluster_role      [cluster] role in local.toml: dev (default) | test | stage | prod | perftest
  platform          k3d
  os                linux | macos | windows (WSL counts as windows)
  os_variant        wsl2 | <distro id from /etc/os-release> | docker-desktop | rancher-desktop (macOS)
  os_version        distro VERSION_ID; on WSL "<distro NAME> <VERSION_ID>"; macOS product version
  platform_version  k3d version (v5.9.0)
"""
import getpass
import os
import platform
import re
import subprocess
import sys
import tomllib
from pathlib import Path

LOCAL_TOML = Path(__file__).resolve().parent / "local.toml"
ROLES = ("dev", "test", "stage", "prod", "perftest")   # the usage API's ClusterRole


def run(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def os_release():
    try:
        lines = Path("/etc/os-release").read_text().splitlines()
    except OSError:
        return {}
    return {k: v.strip('"') for k, _, v in (line.partition("=") for line in lines) if k}


def cluster_role():
    """[cluster] role from local.toml; anything unknown falls back to dev (with a warning), so a typo
    can't get every event rejected."""
    try:
        role = tomllib.loads(LOCAL_TOML.read_text()).get("cluster", {}).get("role", "dev")
    except FileNotFoundError:
        return "dev"
    except tomllib.TOMLDecodeError as e:
        print(f"hostfacts: {LOCAL_TOML.name}: {e}; cluster_role=dev", file=sys.stderr)
        return "dev"
    if role not in ROLES:
        print(f"hostfacts: [cluster] role {role!r} is not one of {', '.join(ROLES)}; using dev", file=sys.stderr)
        return "dev"
    return role


def facts():
    system = platform.system()
    if system == "Darwin":
        engine = run("docker", "info", "--format", "{{.Name}}")
        variant = "rancher-desktop" if "rancher" in engine else "docker-desktop" if "docker-desktop" in engine else ""
        os_name, version = "macos", platform.mac_ver()[0]
    elif "microsoft" in Path("/proc/version").read_text(errors="ignore").lower():
        rel = os_release()
        os_name, variant = "windows", "wsl2"
        version = " ".join(x for x in (rel.get("NAME", ""), rel.get("VERSION_ID", "")) if x)
    else:
        rel = os_release()
        os_name, variant, version = "linux", rel.get("ID", ""), rel.get("VERSION_ID", "")
    k3d = re.search(r"k3d version (v\S+)", run("k3d", "version"))
    return {
        "username": os.environ.get("USER") or getpass.getuser(),
        "cluster_type": "localdev",
        "cluster_role": cluster_role(),
        "platform": "k3d",
        "os": os_name,
        "os_variant": variant,
        "os_version": version or platform.release(),
        "platform_version": k3d.group(1) if k3d else "",   # empty = left out of events
    }


if __name__ == "__main__":
    for key, value in facts().items():
        print(f"{key}={value}")
