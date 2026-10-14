#!/usr/bin/env python3
"""Cluster profiles for kube/localdev (driven by the cluster:* mise tasks).

profiles.toml names which optional components (components/<name>/) a cluster runs.
application-profiles.toml (checked in, same format) adds the profiles and metadata of application
components, kept apart from the core ones (e.g. usage-server). local.toml (gitignored, same format) adds
a developer's own profiles and EXTERNAL components - folders in other projects:
[components.<name>] path = "../../../myapp/localdev". This tool builds runtime.profile/, the
"active view" the cluster reads - links, so edits to the real files stay live:

  runtime.profile/name          current profile
  runtime.profile/workspace     host dir mounted into the node at the same path (cluster:create);
                                external components under it are linked, others are COPIED
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
  missing [addons]      "<component>\t<file>" per manifest of an enabled component not yet deployed
                        (to touch: k3s skips a path+mtime it has seen)
  buildable             enabled components with an image to build (build.sh or docker/)
  dir <component>       the component's folder (components/<c>, or its local.toml path)
  workspace             the workspace mounted into the cluster (empty if none)
  check                 validate profiles.toml, application-profiles.toml, local.toml and components/
"""
import functools
import os
import shutil
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "profiles.toml"
APPS = HERE / "application-profiles.toml"   # checked in: application components' profiles and metadata
LOCAL = HERE / "local.toml"            # per developer, gitignored: own profiles, external components
COMPONENTS = HERE / "components"
VIEW = HERE / "runtime.profile"
# layer -> folder in runtime.profile/manifests/ (sorted = deploy order). manifests/<folder>/ holds the
# always-on files of a layer, linked as <folder>/core; components land in their layer's folder.
LAYERS = {"platform": "10-platform", "infra": "20-infra", "tools": "30-tools", "apps": "50-apps"}
DASHBOARDS = HERE / "grafana" / "dashboards"      # always-on dashboards (platform, infra, apps, root)


def die(msg):
    sys.exit(f"profile: {msg}")


@functools.cache
def config():
    """profiles.toml, then application-profiles.toml and local.toml merged over it (per profile / per
    component; `default` from local.toml only)."""
    cfg = tomllib.loads(CONFIG.read_text())
    for f in (APPS, LOCAL):
        if not f.exists():
            continue
        over = tomllib.loads(f.read_text())
        if f == LOCAL and "default" in over:
            cfg["default"] = over["default"]
        for table in ("profiles", "components"):
            for key, value in over.get(table, {}).items():
                cfg.setdefault(table, {})[key] = {**cfg.get(table, {}).get(key, {}), **value}
    return cfg


def external_path(c):
    """The folder of an external component (a `path` in local.toml / *profiles.toml), or None."""
    p = config().get("components", {}).get(c, {}).get("path")
    if not p:
        return None
    p = Path(os.path.expanduser(p))
    return (p if p.is_absolute() else HERE / p).resolve()


def component_dir(c):
    return external_path(c) or COMPONENTS / c


def components_on_disk():
    names = {p.name for p in COMPONENTS.iterdir() if (p / "manifests").is_dir()}
    names |= {c for c in config().get("components", {}) if external_path(c) and (external_path(c) / "manifests").is_dir()}
    return sorted(names)


def workspace():
    """The host dir cluster:create mounted into the node at the same path (None: no mount)."""
    f = VIEW / "workspace"
    return Path(f.read_text().strip()) if f.exists() and f.read_text().strip() else None


def linkable(c):
    """True if the cluster can follow a link to the component's folder: inside this repo (always
    mounted) or inside the mounted workspace. Otherwise its manifests are copied."""
    d, ws = component_dir(c), workspace()
    return d.is_relative_to(HERE) or bool(ws and d.is_relative_to(ws))


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
    return sorted((component_dir(component) / "manifests").glob("*.y*ml"))


def deployed_file(cfg, c, f):
    """Where k3s sees manifest f of component c: the source via a dir link, or its copy."""
    if linkable(c):
        return f
    return VIEW / "manifests" / LAYERS[meta(cfg, c).get("layer", "tools")] / c / f.name


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
    copied = []
    for c in comps:
        folder = LAYERS[meta(cfg, c).get("layer", "tools")]
        if linkable(c):
            (m / folder / c).symlink_to(component_dir(c) / "manifests", target_is_directory=True)
        else:   # outside every mount: copy (copy2 keeps mtimes, so unchanged files aren't re-applied)
            (m / folder / c).mkdir()
            for f in manifest_files(c):
                shutil.copy2(f, m / folder / c / f.name)
            copied.append(c)
    d = VIEW / "dashboards"

    def link_file(src, dst):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.symlink_to(os.path.relpath(src, dst.parent))

    for src in DASHBOARDS.rglob("*.json"):
        link_file(src, d / src.relative_to(DASHBOARDS))
    for c in comps:
        for src in sorted((component_dir(c) / "dashboards").glob("*.json")):
            if external_path(c):   # Grafana only sees this repo (/localdev): copy
                (d / "tools").mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, d / "tools" / src.name)
            else:
                link_file(src, d / "tools" / src.name)
    (VIEW / "name").write_text(name + "\n")
    (VIEW / "components").write_text("".join(f"{c}\n" for c in comps))
    n_dash = sum(1 for _ in d.rglob("*.json"))
    print(f"  profile {name}: components [{', '.join(comps) or '-'}]; "
          f"{n_dash} dashboards -> {VIEW.relative_to(HERE)}/")
    for c in copied:
        print(f"  note: {c} ({component_dir(c)}) is outside the mounted workspace ({workspace() or 'none'}):"
              f" its manifests are COPIED - after editing them, re-run `mise run cluster:profile`")


def cmd_list(_):
    cfg, cur = config(), current()
    if always(cfg):
        print(f"   (every profile also includes: {', '.join(always(cfg))})")
    for name, p in cfg.get("profiles", {}).items():
        comps, _ = resolve(name, cfg)
        mark = "*" if name == cur else " "
        dflt = " (default)" if name == cfg.get("default") else ""
        print(f" {mark} {name:9} {p.get('description', '')}{dflt}\n             [{', '.join(comps) or 'no tools'}]")
    external = [c for c in components_on_disk() if external_path(c)]
    if external:
        print("   external components (a path in local.toml / *profiles.toml):")
        for c in external:
            print(f"     {c:12} {component_dir(c)}  ({'linked' if linkable(c) else 'copied: outside the workspace'})")


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
    cfg = config()
    for c in comps:
        for f in manifest_files(c):
            if f.stem not in deployed:
                print(f"{c}\t{deployed_file(cfg, c, f)}")


def cmd_check(_):
    cfg, problems = config(), []
    for c, m in cfg.get("components", {}).items():
        if m.get("layer", "tools") not in LAYERS:
            problems.append(f"[components.{c}] layer '{m['layer']}' is not one of {', '.join(LAYERS)}")
    if cfg.get("default") not in cfg.get("profiles", {}):
        problems.append(f"default profile '{cfg.get('default')}' is not defined")
    for c, m in cfg.get("components", {}).items():
        if m.get("path"):
            if (COMPONENTS / c).exists():
                problems.append(f"external component '{c}' clashes with components/{c}/ (pick another name)")
            elif not (external_path(c) / "manifests").is_dir():
                problems.append(f"[components.{c}] path {external_path(c)} has no manifests/ folder")
            elif not linkable(c):
                print(f"  note: {c} is outside the mounted workspace ({workspace() or 'none'}): manifests are copied")
        elif c not in components_on_disk():
            problems.append(f"[components.{c}] has no components/{c}/manifests/")
    for name in cfg.get("profiles", {}):
        try:
            resolve(name, cfg)
        except SystemExit as e:
            problems.append(str(e))
    for c in components_on_disk():
        if not manifest_files(c):
            problems.append(f"components/{c}/manifests/ has no .yaml files")
        if any((component_dir(c) / "manifests").glob("*.json")):
            problems.append(f"{component_dir(c)}/manifests/ contains .json (k3s would apply it; dashboards go in dashboards/)")
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
                        if (component_dir(c) / "build.sh").is_file() or (component_dir(c) / "docker").is_dir()))
    elif cmd == "dir":
        if not args or args[0] not in components_on_disk():
            die(f"no component '{args[0] if args else ''}' (have: {', '.join(components_on_disk())})")
        print(component_dir(args[0]))
    elif cmd == "workspace":
        print(workspace() or "")
    elif cmd == "missing":
        cmd_missing(args)
    elif cmd == "check":
        cmd_check(args)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
