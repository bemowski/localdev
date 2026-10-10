#!/usr/bin/env python3
"""Cluster profiles for kube/localdev (driven by the cluster:* mise tasks).

profiles.toml names which optional components (components/<name>/) a cluster runs. This tool
builds runtime.profile/, the "active view" the cluster reads - links only, no copies, so edits to
the real files stay live:

  runtime.profile/name          current profile
  runtime.profile/components    enabled components, one per line (-> cnav ConfigMap)
  runtime.profile/manifests/    mounted as k3s's manifests dir: a real folder per layer
                                (10-platform, 20-infra, 30-tools, 50-apps) holding DIRECTORY links -
                                core -> manifests/<layer>, <component> -> components/<c>/manifests
                                (k3s follows dir links and sees edits; absolute targets - the repo
                                is mounted inside the node at the same path)
  runtime.profile/dashboards/   Grafana's provider path; real folders with FILE links (Grafana
                                follows file links, not dir links); relative targets, valid on the
                                host and in the Grafana pod (repo mounted at /localdev)

  link [profile]        build runtime.profile/ (default: current, else profiles.toml default)
  list                  profiles and their components; marks the current one
  current               print the current profile name
  enabled <component>   exit 0 if enabled in the current profile
  removed <to> [addons] manifest files to delete to reach <to>: components still deployed
                        (their AddOns exist) but not in <to>; dependents first, files reversed
  missing [addons]      manifest files of enabled components not yet deployed (to touch)
  buildable             enabled components with an image to build (build.sh or docker/)
  check                 validate profiles.toml and components/
"""
import os
import shutil
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "profiles.toml"
COMPONENTS = HERE / "components"
VIEW = HERE / "runtime.profile"
# layer -> folder in runtime.profile/manifests/ (sorted = deploy order). manifests/<folder>/ holds the
# always-on files of a layer, linked as <folder>/core; components land in their layer's folder.
LAYERS = {"platform": "10-platform", "infra": "20-infra", "tools": "30-tools", "apps": "50-apps"}
DASHBOARDS = HERE / "grafana" / "dashboards"      # always-on dashboards (platform, infra, apps, root)


def die(msg):
    sys.exit(f"profile: {msg}")


def config():
    return tomllib.loads(CONFIG.read_text())


def components_on_disk():
    return sorted(p.name for p in COMPONENTS.iterdir() if (p / "manifests").is_dir())


def meta(cfg, c):
    return cfg.get("components", {}).get(c, {})


def always(cfg):
    return [c for c in components_on_disk() if meta(cfg, c).get("always")]


def resolve(name, cfg=None):
    """Components of a profile: its list + `always` components + `requires` (returns (list, auto-added))."""
    cfg = cfg or config()
    profiles = cfg.get("profiles", {})
    if name not in profiles:
        die(f"unknown profile '{name}' (have: {', '.join(profiles)})")
    known = set(components_on_disk())
    wanted, added = list(profiles[name].get("components", [])), []
    wanted += [c for c in always(cfg) if c not in wanted]
    i = 0
    while i < len(wanted):
        c = wanted[i]
        if c not in known:
            die(f"profile '{name}': no component '{c}' (have: {', '.join(sorted(known))})")
        for r in cfg.get("components", {}).get(c, {}).get("requires", []):
            if r not in wanted:
                wanted.append(r); added.append(f"{r} (required by {c})")
        i += 1
    order = components_on_disk()
    return sorted(wanted, key=order.index), added


def current():
    f = VIEW / "name"
    return f.read_text().strip() if f.exists() else None


def manifest_files(component):
    return sorted((COMPONENTS / component / "manifests").glob("*.y*ml"))


def link(name):
    cfg = config()
    name = name or current() or cfg.get("default", "dev")
    comps, added = resolve(name, cfg)
    for a in added:
        print(f"  + {a}")
    # rebuild the CONTENTS from scratch (cheap; stale links can't survive). Never remove the
    # directories themselves: runtime.profile/manifests is bind-mounted into the k3d node, and a
    # bind mount stays attached to the original directory - a deleted-and-recreated one would
    # leave k3s watching an orphaned, empty directory.
    for sub in ("manifests", "dashboards"):
        (VIEW / sub).mkdir(parents=True, exist_ok=True)
        for entry in (VIEW / sub).iterdir():
            if entry.is_symlink() or entry.is_file():
                entry.unlink()
            else:
                shutil.rmtree(entry)
    m = VIEW / "manifests"
    # each layer is a real folder with DIRECTORY links: <layer>/core -> manifests/<layer> (the
    # always-on files) and <layer>/<component> -> components/<component>/manifests. k3s follows links
    # at any depth of the real tree and sees edits through dir links (not through file links).
    for layer, folder in LAYERS.items():
        (m / folder).mkdir()
        if (HERE / "manifests" / folder).is_dir():
            (m / folder / "core").symlink_to(HERE / "manifests" / folder, target_is_directory=True)
    for c in comps:
        folder = LAYERS[meta(cfg, c).get("layer", "tools")]
        (m / folder / c).symlink_to(COMPONENTS / c / "manifests", target_is_directory=True)
    d = VIEW / "dashboards"

    def link_file(src, dst):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.symlink_to(os.path.relpath(src, dst.parent))

    for src in DASHBOARDS.rglob("*.json"):
        link_file(src, d / src.relative_to(DASHBOARDS))
    for c in comps:
        for src in sorted((COMPONENTS / c / "dashboards").glob("*.json")):
            link_file(src, d / "tools" / src.name)
    (VIEW / "name").write_text(name + "\n")
    (VIEW / "components").write_text("".join(f"{c}\n" for c in comps))
    n_dash = sum(1 for _ in d.rglob("*.json"))
    print(f"  profile {name}: components [{', '.join(comps) or '-'}]; "
          f"{n_dash} dashboards -> {VIEW.relative_to(HERE)}/")


def cmd_list(_):
    cfg, cur = config(), current()
    if always(cfg):
        print(f"   (every profile also includes: {', '.join(always(cfg))})")
    for name, p in cfg.get("profiles", {}).items():
        comps, _ = resolve(name, cfg)
        mark = "*" if name == cur else " "
        dflt = " (default)" if name == cfg.get("default") else ""
        print(f" {mark} {name:9} {p.get('description', '')}{dflt}\n             [{', '.join(comps) or 'no tools'}]")


def cmd_removed(args):
    """removed <to-profile> [deployed addon names...]: what to uninstall to reach <to-profile>.
    Based on what is actually deployed (k3s AddOn names = manifest basenames), not on the previous
    profile name, so re-running a switch finishes any partial uninstall."""
    if not args:
        die("usage: removed <to-profile> [deployed addon names...]")
    cfg = config()
    after, _ = resolve(args[0], cfg)
    deployed = set(args[1:])
    gone = [c for c in components_on_disk()
            if c not in after and any(f.stem in deployed for f in manifest_files(c))]
    # uninstall dependents before what they require (keycloak before postgres), and each
    # component's files in reverse order (e.g. Knative CRs before the operator that finalizes them)
    requires = {c: set(cfg.get("components", {}).get(c, {}).get("requires", [])) for c in gone}
    ordered = []
    while gone:
        leaf = next(c for c in gone if not any(c in requires[o] for o in gone if o != c))
        ordered.append(leaf); gone.remove(leaf)
    for c in ordered:
        for f in reversed(manifest_files(c)):
            print(f"{c}\t{f}")


def cmd_missing(args):
    """missing [deployed addon names...]: manifest files of enabled components with no AddOn yet.
    k3s skips a file whose path+mtime it has already seen (even if its objects were deleted since),
    so a component removed and re-added in the same k3s process needs a fresh mtime (touch)."""
    deployed = set(args)
    comps = (VIEW / "components").read_text().split() if (VIEW / "components").exists() else []
    for c in comps:
        for f in manifest_files(c):
            if f.stem not in deployed:
                print(f)


def cmd_check(_):
    cfg, problems = config(), []
    for c, m in cfg.get("components", {}).items():
        if m.get("layer", "tools") not in LAYERS:
            problems.append(f"[components.{c}] layer '{m['layer']}' is not one of {', '.join(LAYERS)}")
    if cfg.get("default") not in cfg.get("profiles", {}):
        problems.append(f"default profile '{cfg.get('default')}' is not defined")
    for c in cfg.get("components", {}):
        if c not in components_on_disk():
            problems.append(f"[components.{c}] has no components/{c}/manifests/")
    for name in cfg.get("profiles", {}):
        try:
            resolve(name, cfg)
        except SystemExit as e:
            problems.append(str(e))
    for c in components_on_disk():
        if not manifest_files(c):
            problems.append(f"components/{c}/manifests/ has no .yaml files")
        if any((COMPONENTS / c / "manifests").glob("*.json")):
            problems.append(f"components/{c}/manifests/ contains .json (k3s would apply it; dashboards go in dashboards/)")
    for msg in problems:
        print(f"  {msg}")
    if problems:
        die(f"{len(problems)} problem(s)")
    print(f"  ok: {len(cfg['profiles'])} profiles, components: {', '.join(components_on_disk())}")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "link":
        link(args[0] if args else None)
    elif cmd == "list":
        cmd_list(args)
    elif cmd == "current":
        print(current() or config().get("default", "dev"))
    elif cmd == "enabled":
        comps = (VIEW / "components").read_text().split() if (VIEW / "components").exists() else []
        sys.exit(0 if args and args[0] in comps else 1)
    elif cmd == "removed":
        cmd_removed(args)
    elif cmd == "buildable":
        comps = (VIEW / "components").read_text().split() if (VIEW / "components").exists() else []
        print("\n".join(c for c in comps
                        if (COMPONENTS / c / "build.sh").is_file() or (COMPONENTS / c / "docker").is_dir()))
    elif cmd == "missing":
        cmd_missing(args)
    elif cmd == "check":
        cmd_check(args)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
