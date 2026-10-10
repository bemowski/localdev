#!/usr/bin/env python3
"""Local secret store for localdev (driven by the secrets:* mise tasks).

secrets.store.toml (gitignored, next to profiles.toml) holds one table per secret:

    [myapp-db]                # -> Secret secret-store/myapp-db
    username = "app"
    password = "localdev"

  render    print the Secrets (JSON List) for namespace secret-store, labelled
            localdev/secret-store=managed, for `kubectl apply --prune`
  list      secret names and their keys (never values)

ExternalSecrets read them through ClusterSecretStore `local` (remoteRef.key = table name,
remoteRef.property = key). No file = an empty store.
"""
import json
import os
import sys
import tomllib
from pathlib import Path

LOCALDEV = Path(__file__).resolve().parents[2]
STORE = Path(os.environ.get("LOCALDEV_SECRETS_STORE", LOCALDEV / "secrets.store.toml"))
NAMESPACE = "secret-store"
LABEL = {"localdev/secret-store": "managed"}


def load():
    if not STORE.exists():
        return {}
    data = tomllib.loads(STORE.read_text())
    for name, keys in data.items():
        if not isinstance(keys, dict):
            sys.exit(f"store: [{name}] must be a table of key = value (in {STORE})")
    return data


def render(data):
    items = [{
        "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
        "metadata": {"name": name, "namespace": NAMESPACE, "labels": LABEL},
        "stringData": {k: str(v) for k, v in keys.items()},
    } for name, keys in data.items()]
    return {"apiVersion": "v1", "kind": "List", "items": items}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "render":
        json.dump(render(load()), sys.stdout)
    elif cmd == "list":
        data = load()
        if not data:
            print(f"  (no secrets: {STORE} is missing or empty)")
        for name, keys in data.items():
            print(f"  {NAMESPACE}/{name}: {', '.join(keys)}")
    else:
        sys.exit(__doc__)
