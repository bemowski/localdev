#!/usr/bin/env python3
"""Local secret store for localdev (driven by the secrets:* mise tasks).

Every secret localdev uses is mastered here and synced to its consumers by ExternalSecrets that
read ClusterSecretStore `localdev` (remoteRef.key = entry name, remoteRef.property = key). The
store's values are merged, later wins:

  secrets.defaults.toml                    committed: platform/infra entries (sso, localdev-admin)
  components/<c>/secrets.defaults.toml     committed: a component's fixed dev values (postgres-app)
  secrets.store.toml                       yours, gitignored: real secrets + overrides

    [myapp-db]                # one table per entry -> Secret secret-store/myapp-db
    username = "app"
    password = "localdev"

A value "" in a defaults file means REQUIRED: it must be set in secrets.store.toml. A list
(emails = ["a@x.com", "b@x.com"]) is stored as one item per line.

  render    print the Secrets (JSON List) for namespace secret-store, labelled
            localdev/secret-store=managed, for `kubectl apply --prune`
  list      entries and their keys (never values), and where each comes from
  check     exit 1 listing required keys that are still empty
  migrate   print secrets.store.toml content built from secrets.env (one-time move)
"""
import base64
import importlib.util
import json
import os
import sys
import tomllib
from pathlib import Path

LOCALDEV = Path(__file__).resolve().parents[2]
# kube/localdev/profile.py (component folders, local.toml) - loaded by path: "profile" is also a stdlib module
_spec = importlib.util.spec_from_file_location("localdev_profile", LOCALDEV / "profile.py")
profiles = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(profiles)

STORE = Path(os.environ.get("LOCALDEV_SECRETS_STORE", LOCALDEV / "secrets.store.toml"))
NAMESPACE = "secret-store"
LABEL = {"localdev/secret-store": "managed"}
OWNER = {}   # entry -> component whose secrets.defaults.toml declares it (filled by load())


def sources():
    """(label, path, component) of every file feeding the store, lowest precedence first."""
    out = [("defaults", LOCALDEV / "secrets.defaults.toml", None)]
    out += [(f"{c} defaults", profiles.component_dir(c) / "secrets.defaults.toml", c)
            for c in profiles.components_on_disk()]
    out.append(("secrets.store.toml", STORE, None))
    return [(label, p, c) for label, p, c in out if p.exists()]


def load():
    """{entry: {key: value}}, plus {(entry, key): source label} and the required (entry, key)s."""
    data, origin, required = {}, {}, set()
    for label, path, component in sources():
        for entry, keys in tomllib.loads(path.read_text()).items():
            if component:
                OWNER.setdefault(entry, component)
            if not isinstance(keys, dict):
                sys.exit(f"store: [{entry}] in {path} must be a table of key = value")
            for k, v in keys.items():
                if v == "" and path != STORE:
                    required.add((entry, k))
                # a list becomes one item per line (e.g. an allow-list file); consumers can split it
                data.setdefault(entry, {})[k] = "\n".join(map(str, v)) if isinstance(v, list) else str(v)
                origin[(entry, k)] = label
    return data, origin, required


def missing(data, required):
    return sorted((e, k) for e, k in required if not data.get(e, {}).get(k))


def render(data):
    items = [{
        "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
        # localdev/component: the declaring component, so cnav can hide entries of components
        # that aren't in the cluster's profile
        "metadata": {"name": entry, "namespace": NAMESPACE,
                     "labels": {**LABEL, **({"localdev/component": OWNER[entry]} if entry in OWNER else {})}},
        # data (not stringData): an unchanged entry then applies as "unchanged"
        "data": {k: base64.b64encode(v.encode()).decode() for k, v in keys.items()},
    } for entry, keys in data.items()]
    return {"apiVersion": "v1", "kind": "List", "items": items}


def migrate(env_file):
    """secrets.env (KEY=value lines) -> secrets.store.toml text for the platform entries."""
    env = {}
    for line in Path(env_file).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    q = lambda v: json.dumps(v)   # a TOML basic string is JSON-compatible for these values
    return ("# localdev secrets (gitignored). Encrypt to share: mise run encrypt secrets.store.toml\n"
            "# Entries and keys: see secrets.defaults.toml and components/*/secrets.defaults.toml\n\n"
            "[sso]\n"
            f"google-client-id = {q(env.get('GOOGLE_CLIENT_ID', ''))}\n"
            f"google-client-secret = {q(env.get('GOOGLE_CLIENT_SECRET', ''))}\n"
            f"cookie-secret = {q(env.get('COOKIE_SECRET', ''))}\n\n"
            "[localdev-admin]\n"
            f"password = {q(env.get('LOCAL_ADMIN_PASSWORD', ''))}\n")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "render":
        json.dump(render(load()[0]), sys.stdout)
    elif cmd == "list":
        data, origin, required = load()
        for entry, keys in data.items():
            print(f"  {NAMESPACE}/{entry}: " + ", ".join(
                f"{k} ({origin[(entry, k)]}{', MISSING' if (entry, k) in required and not v else ''})"
                for k, v in keys.items()))
    elif cmd == "check":
        data, _, required = load()
        gaps = missing(data, required)
        if gaps:
            for e, k in gaps:
                print(f"  missing: [{e}] {k}  (set it in {STORE.name})", file=sys.stderr)
            sys.exit(f"store: {len(gaps)} required secret(s) not set - see the docs (Secrets)")
    elif cmd == "migrate":
        sys.stdout.write(migrate(sys.argv[2] if len(sys.argv) > 2 else LOCALDEV / "secrets.env"))
    else:
        sys.exit(__doc__)
