"""Frozen API versions: api/openapi-<info.version>.yaml, checked-in copies of openapi.yaml.

openapi.yaml is the spec you edit. Once a version is frozen it must not change: a changed spec needs a
new info.version, then a new freeze. Clients pin a frozen file (usage-client's contract test).

  python scripts/api_versions.py freeze    copy openapi.yaml to api/openapi-<version>.yaml
  python scripts/api_versions.py check     problems (exit 1): a frozen file that differs from
                                           openapi.yaml at the same version, or is misnamed
  python scripts/api_versions.py released  like check, and the current version must be frozen

Run: mise run api:freeze (check runs in mise run api:check)
"""
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "openapi.yaml"
API = ROOT / "api"


def version(path):
    return str(yaml.safe_load(path.read_text())["info"]["version"])


def frozen(v):
    return API / f"openapi-{v}.yaml"


def problems(require_current=False):
    out = []
    for f in sorted(API.glob("*.yaml")):
        try:
            v = version(f)
        except Exception as e:
            out.append(f"{f.relative_to(ROOT)}: unreadable ({e})")
            continue
        if f.name != frozen(v).name:
            out.append(f"{f.relative_to(ROOT)}: info.version is {v}, so it should be named {frozen(v).name}")
    v = version(SPEC)
    f = frozen(v)
    if f.exists() and f.read_bytes() != SPEC.read_bytes():
        out.append(f"openapi.yaml differs from frozen {f.relative_to(ROOT)}: bump info.version, then mise run api:freeze")
    elif require_current and not f.exists():
        out.append(f"version {v} is not frozen: mise run api:freeze, then commit {f.relative_to(ROOT)}")
    return out


def main(cmd):
    if cmd == "freeze":
        v = version(SPEC)
        f = frozen(v)
        if f.exists():
            if f.read_bytes() != SPEC.read_bytes():
                sys.exit(f"{f.relative_to(ROOT)} exists and differs from openapi.yaml: bump info.version")
            print(f"{f.relative_to(ROOT)} is up to date")
            return
        API.mkdir(exist_ok=True)
        f.write_bytes(SPEC.read_bytes())
        print(f"froze {f.relative_to(ROOT)} (commit it)")
    elif cmd in ("check", "released"):
        errs = problems(require_current=cmd == "released")
        for e in errs:
            print(e, file=sys.stderr)
        sys.exit(1 if errs else 0)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
