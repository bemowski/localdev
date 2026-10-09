"""cnav — cluster navigator.

Serves the static page, the config files (cards.yaml, status.yaml) and
/api/status, which evaluates status.yaml against the Kubernetes API using the
pod's service account. Stdlib only, plus PyYAML.
"""
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

STATIC_DIR = Path(os.environ.get("CNAV_STATIC", "/app/static"))
CONFIG_DIR = Path(os.environ.get("CNAV_CONFIG", "/etc/cnav"))
PORT = int(os.environ.get("CNAV_LISTEN_PORT", "8080"))  # not CNAV_PORT: k8s injects that for Service "cnav"
HEADLAMP = os.environ.get("CNAV_HEADLAMP", "/headlamp/c/main")

STARTING_GRACE_S = 120      # Pending/creating younger than this is amber, older is red
RECENT_RESTART_S = 600      # a restart within this window is amber

RED_WAITING = {
    "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "InvalidImageName",
    "CreateContainerConfigError", "CreateContainerError", "RunContainerError",
}
RANK = {"green": 0, "amber": 1, "red": 2}

WORKLOADS = {  # kind -> (api path, headlamp route)
    "Deployment": ("/apis/apps/v1/namespaces/{ns}/deployments/{name}", "deployments"),
    "StatefulSet": ("/apis/apps/v1/namespaces/{ns}/statefulsets/{name}", "statefulsets"),
    "DaemonSet": ("/apis/apps/v1/namespaces/{ns}/daemonsets/{name}", "daemonsets"),
}


# --- kubernetes API ---------------------------------------------------------

class Kube:
    SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")

    def __init__(self):
        host = os.environ.get("KUBERNETES_SERVICE_HOST")
        if not host:
            raise RuntimeError("not running in a cluster")
        self.base = f"https://{host}:{os.environ.get('KUBERNETES_SERVICE_PORT', '443')}"
        self.ctx = ssl.create_default_context(cafile=str(self.SA / "ca.crt"))

    def get(self, path, **query):
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        token = (self.SA / "token").read_text()  # re-read: projected tokens rotate
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=5) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise


def age_s(ts):
    if not ts:
        return 0
    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - t).total_seconds()


def ago(seconds):
    return f"{int(seconds // 60)}m ago" if seconds >= 60 else f"{int(seconds)}s ago"


def result(state, reason="", ready=None, desired=None, mem=None):
    return {"state": state, "reason": reason, "ready": ready, "desired": desired, "mem": mem}


UNITS = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12}


def quantity_bytes(q):
    """Parse a k8s memory quantity ('1592Ki', '34Mi', '123456') to bytes."""
    for suffix in sorted(UNITS, key=len, reverse=True):
        if q.endswith(suffix):
            return int(float(q[: -len(suffix)]) * UNITS[suffix])
    return int(float(q))


def pods_memory(kube, ns, selector):
    """Working-set memory of matching pods from metrics-server; None if unavailable."""
    try:
        m = kube.get(f"/apis/metrics.k8s.io/v1beta1/namespaces/{ns}/pods", labelSelector=selector)
    except Exception:
        return None
    if not m:
        return None
    return sum(quantity_bytes(c["usage"]["memory"]) for p in m.get("items", []) for c in p.get("containers", []))


# --- checks -----------------------------------------------------------------

def pod_problem(pod):
    """Return (state, reason) for the worst thing wrong with one pod, or None."""
    st = pod.get("status", {})
    statuses = st.get("initContainerStatuses", []) + st.get("containerStatuses", [])
    for cs in statuses:
        waiting = cs.get("state", {}).get("waiting") or {}
        if waiting.get("reason") in RED_WAITING:
            last = (cs.get("lastState", {}).get("terminated") or {}).get("reason")
            why = waiting["reason"] + (f" (last exit: {last})" if last else "")
            return "red", f"{cs['name']}: {why}"
        # newer kubelets leave a crash-looping container in state.terminated between
        # restarts rather than waiting:CrashLoopBackOff; exit 0 = finished init container
        term = cs.get("state", {}).get("terminated") or {}
        if term and term.get("exitCode", 0) != 0:
            loop = "CrashLoopBackOff" if cs.get("restartCount") else "exited"
            return "red", (f"{cs['name']}: {loop}, exit {term.get('exitCode')} ({term.get('reason', '?')}), "
                           f"{cs.get('restartCount', 0)} restarts")
    if st.get("phase") == "Pending":
        age = age_s(pod["metadata"].get("creationTimestamp"))
        cond = next((c for c in st.get("conditions", []) if c.get("status") == "False"), {})
        why = cond.get("reason") or "Pending"
        if age < STARTING_GRACE_S:
            return "amber", f"starting ({why})"
        return "red", f"Pending {ago(age)}: {cond.get('message') or why}"[:160]
    if st.get("phase") == "Failed":
        return "red", f"pod failed: {st.get('reason', 'unknown')}"
    for cs in statuses:
        term = cs.get("lastState", {}).get("terminated") or {}
        since = age_s(term.get("finishedAt"))
        if cs.get("restartCount") and term and since < RECENT_RESTART_S:
            return "amber", f"{cs['name']} restarted {ago(since)} ({term.get('reason', '?')})"
    return None


def check_workload(kube, kind, ns, name):
    path, _ = WORKLOADS[kind]
    obj = kube.get(path.format(ns=ns, name=name))
    if obj is None:
        return result("red", f"{kind} {ns}/{name} not found")

    spec, st = obj.get("spec", {}), obj.get("status", {})
    if kind == "DaemonSet":
        desired, ready = st.get("desiredNumberScheduled", 0), st.get("numberReady", 0)
    else:
        desired, ready = spec.get("replicas", 1), st.get("readyReplicas", 0)
    if desired == 0:
        return result("amber", "scaled to 0", ready, desired)

    labels = spec.get("selector", {}).get("matchLabels", {})
    selector = ",".join(f"{k}={v}" for k, v in labels.items())
    pods = (kube.get(f"/api/v1/namespaces/{ns}/pods", labelSelector=selector) or {}).get("items", [])
    problems = [p for p in (pod_problem(p) for p in pods) if p]
    worst = max(problems, key=lambda p: RANK[p[0]], default=None)
    mem = pods_memory(kube, ns, selector)

    if ready == 0:
        if worst and worst[0] == "amber" and worst[1].startswith("starting"):
            return result("amber", worst[1], ready, desired, mem)
        return result("red", worst[1] if worst else "no ready pods", ready, desired, mem)
    if ready < desired:
        why = f"{ready}/{desired} ready" + (f": {worst[1]}" if worst else "")
        return result("amber", why, ready, desired, mem)
    if worst:  # all ready, but something flapped recently
        return result("amber", worst[1], ready, desired, mem)
    return result("green", "", ready, desired, mem)


def check_helmchart(kube, name, ns="kube-system"):
    hc = kube.get(f"/apis/helm.cattle.io/v1/namespaces/{ns}/helmcharts/{name}")
    if hc is None:
        return result("red", f"HelmChart {name} not found")
    job_name = hc.get("status", {}).get("jobName")
    job = job_name and kube.get(f"/apis/batch/v1/namespaces/{ns}/jobs/{job_name}")
    if not job:
        return result("amber", "helm install pending")
    jst = job.get("status", {})
    if jst.get("succeeded"):
        return result("green")
    if jst.get("active"):
        return result("amber", "helm installing")
    if any(c.get("type") == "Failed" and c.get("status") == "True" for c in jst.get("conditions", [])):
        return result("red", "helm install failed")
    return result("amber", "helm install retrying")


def evaluate_item(kube, item):
    results, link = [], None
    for c in item.get("checks", []):
        kind = c.get("kind")
        try:
            if kind in WORKLOADS:
                r = check_workload(kube, kind, c["namespace"], c["name"])
                if link is None:
                    link = f"{HEADLAMP}/{WORKLOADS[kind][1]}/{c['namespace']}/{c['name']}"
            elif kind == "HelmChart":
                r = check_helmchart(kube, c["name"], c.get("namespace", "kube-system"))
            else:
                r = result("red", f"unknown check kind {kind!r}")
        except Exception as e:  # one broken check must not sink the whole page
            r = result("red", f"check error: {e}"[:160])
        results.append(r)

    worst = max(results, key=lambda r: RANK[r["state"]], default=result("amber", "no checks"))
    counted = [r for r in results if r["desired"] is not None]
    mems = [r["mem"] for r in results if r["mem"] is not None]
    return {
        "name": item.get("name", "?"),
        "state": worst["state"],
        "reason": worst["reason"],
        "ready": sum(r["ready"] for r in counted) if counted else None,
        "desired": sum(r["desired"] for r in counted) if counted else None,
        "mem": sum(mems) if mems else None,
        "link": item.get("link", link),
    }


def status():
    config = yaml.safe_load((CONFIG_DIR / "status.yaml").read_text()) or []
    kube = Kube()
    groups = []
    for g in config:
        items = [evaluate_item(kube, i) for i in g.get("items", [])]
        state = max((i["state"] for i in items), key=RANK.get, default="green")
        mem = sum(i["mem"] for i in items if i["mem"] is not None)
        groups.append({"group": g.get("group", "?"), "state": state, "mem": mem, "items": items})
    return {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mem": sum(g["mem"] for g in groups),
        "groups": groups,
    }


# --- http -------------------------------------------------------------------

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(STATIC_DIR), **kw)

    def send_json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/healthz":
            return self.send_json(200, {"ok": True})
        if path == "/api/status":
            try:
                return self.send_json(200, status())
            except Exception as e:
                return self.send_json(503, {"error": str(e)})
        if path in ("/cards.yaml", "/status.yaml"):
            f = CONFIG_DIR / path.lstrip("/")
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/yaml; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/signed-out":
            self.path = "/signed-out.html"  # landing page after /oauth2/sign_out (outside SSO)
        elif not (STATIC_DIR / path.lstrip("/")).is_file():
            self.path = "/index.html"  # catch-all: unknown paths land on the navigator
        return super().do_GET()

    def log_message(self, fmt, *args):
        if "/healthz" not in (args[0] if args else ""):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    print(f"cnav listening on :{PORT}", flush=True)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
