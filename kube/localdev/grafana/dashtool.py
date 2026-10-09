#!/usr/bin/env python3
"""Grafana dashboard tooling for kube/localdev (driven by the grafana:* mise tasks).

grafana/dashboards/<folder>/*.json is provisioned into Grafana with one Grafana folder
per directory (platform, infra, tools, apps). This tool moves dashboards between that
tree, the running Grafana and grafana.com:

  list                         dashboards in Grafana (folder, uid, title, file-provisioned?)
  export <uid|search> [folder] Grafana -> grafana/dashboards/<folder>/<slug>.json
                               (default: the file it's provisioned from; "root" = top level)
  import <file|dir> [folder]   JSON file(s) -> grafana/dashboards/<folder>/ (normalized)
  fetch <gnet-id> <folder>     grafana.com dashboard -> grafana/dashboards/<folder>/

Normalizing: drops the instance-specific "id", resolves grafana.com "__inputs"
(${DS_PROMETHEUS}, ${DS_LOKI}, ...) to this cluster's datasource uids, keeps a stable uid.
Grafana is reached at $GRAFANA_URL (the mise task port-forwards it) using the same
auth-proxy header oauth2-proxy sets, so no password is needed.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "dashboards"
FOLDERS = ("platform", "infra", "tools", "apps")
GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://localhost:13000/grafana").rstrip("/")
AUTH_HEADER = {"X-Auth-Request-Email": os.environ.get("GRAFANA_USER", "dashtool@localdev")}
DATASOURCE_UIDS = {"prometheus": "prometheus", "loki": "loki", "alertmanager": "alertmanager"}


def die(msg):
    sys.exit(f"dashtool: {msg}")


def grafana(path):
    req = urllib.request.Request(GRAFANA_URL + path, headers=AUTH_HEADER)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        die(f"GET {path}: HTTP {e.code} {e.read()[:200]!r}")
    except urllib.error.URLError as e:
        die(f"cannot reach Grafana at {GRAFANA_URL}: {e.reason}")


def slug(title):
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "dashboard"


def normalize(dash, fallback_uid=None):
    """Make a dashboard JSON safe to provision from a file in this cluster."""
    text = json.dumps(dash)
    for inp in dash.get("__inputs", []):
        if inp.get("type") == "datasource":
            uid = DATASOURCE_UIDS.get(inp.get("pluginId"))
            if not uid:
                print(f"  warning: no local datasource for input {inp['name']} ({inp.get('pluginId')})")
                continue
            text = text.replace("${%s}" % inp["name"], uid)
    dash = json.loads(text)
    for key in ("__inputs", "__elements", "id", "version"):
        dash.pop(key, None)
    dash["id"] = None
    if not dash.get("uid"):
        dash["uid"] = fallback_uid or slug(dash.get("title", "dashboard"))[:40]
    return dash


def provisioned_file(uid):
    """The file under ROOT a dashboard uid is provisioned from, if any."""
    for f in ROOT.rglob("*.json"):
        if json.loads(f.read_text()).get("uid") == uid:
            return f
    return None


def write(dash, folder, out=None):
    if folder == "root":       # top level of the tree = Grafana's root "Dashboards" list
        folder = "."
    if folder not in FOLDERS + (".",):
        print(f"  note: '{folder}' is not one of {FOLDERS}; it becomes its own Grafana folder")
    out = out or ROOT / folder / f"{slug(dash.get('title', dash['uid']))}.json"
    # Grafana can't provision two files with one uid: a dashboard lives in exactly one place
    for other in ROOT.rglob("*.json"):
        if other.resolve() != out.resolve() and json.loads(other.read_text()).get("uid") == dash["uid"]:
            die(f"uid {dash['uid']} is already provisioned from {other.relative_to(ROOT)}; "
                f"move that file instead, or change the uid in the source to keep a copy")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dash, indent=2, ensure_ascii=False) + "\n")
    print(f"  wrote {out.relative_to(ROOT.parent.parent)}  (uid {dash['uid']})")
    return out


def cmd_list(_args):
    for d in sorted(grafana("/api/search?type=dash-db&limit=5000"), key=lambda d: (d.get("folderTitle", ""), d["title"])):
        print(f"{d.get('folderTitle', 'General'):10} {d['uid']:40} {d['title']}")


def cmd_export(args):
    if not args:
        die("usage: export <uid|search> [folder]")
    query, folder = args[0], (args[1] if len(args) > 1 else None)
    hits = grafana("/api/search?type=dash-db&limit=5000&query=" + urllib.parse.quote(query))
    exact = [h for h in grafana("/api/search?type=dash-db&limit=5000") if h["uid"] == query]
    hits = exact or hits
    if not hits:
        die(f"no dashboard matches {query!r} (try: mise run grafana:list)")
    if len(hits) > 1:
        print(f"{len(hits)} dashboards match {query!r}; pass a uid:")
        for h in hits:
            print(f"  {h['uid']:40} {h.get('folderTitle', 'General'):10} {h['title']}")
        sys.exit(1)
    data = grafana(f"/api/dashboards/uid/{hits[0]['uid']}")
    existing = provisioned_file(hits[0]["uid"])
    if existing and not folder:   # already a file: update it in place
        write(normalize(data["dashboard"]), "", out=existing)
        return
    current = (data["meta"].get("folderTitle") or "").lower()
    folder = folder or (current if current in FOLDERS else "apps")
    write(normalize(data["dashboard"]), folder)


def cmd_import(args):
    if not args:
        die("usage: import <file|dir> [folder]")
    src, folder = Path(args[0]), (args[1] if len(args) > 1 else "apps")
    files = sorted(src.glob("*.json")) if src.is_dir() else [src]
    if not files:
        die(f"no .json files in {src}")
    for f in files:
        dash = json.loads(f.read_text())
        dash = dash.get("dashboard", dash)  # accept raw API responses too
        write(normalize(dash, fallback_uid=slug(f.stem)[:40]), folder)


def cmd_fetch(args):
    if len(args) < 2:
        die("usage: fetch <grafana.com id> <folder>")
    gnet, folder = args[0], args[1]
    url = f"https://grafana.com/api/dashboards/{gnet}/revisions/latest/download"
    with urllib.request.urlopen(url, timeout=30) as r:
        dash = json.load(r)
    dash = normalize(dash, fallback_uid=f"gnet-{gnet}")
    dash.setdefault("tags", [])
    if "grafana.com" not in dash["tags"]:
        dash["tags"].append("grafana.com")
    dash["description"] = (dash.get("description") or "") + f" [grafana.com/dashboards/{gnet}]"
    write(dash, folder)


COMMANDS = {"list": cmd_list, "export": cmd_export, "import": cmd_import, "fetch": cmd_fetch}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(__doc__)
    COMMANDS[sys.argv[1]](sys.argv[2:])
