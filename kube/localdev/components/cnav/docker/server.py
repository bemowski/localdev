"""cnav — cluster navigator.

Serves the static page, the config files (cards.yaml, status.yaml),
/api/status, which evaluates status.yaml against the Kubernetes API using the
pod's service account, and /api/ports, the host ports k3d.yaml exposes and what
answers them in the cluster. Stdlib only, plus PyYAML.
"""
import json
import logging
import os
import signal
import ssl
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

class LogfmtFormatter(logging.Formatter):
    """One logfmt line per record (ts=... level=info logger=cnav msg="..."): Loki's detected_level
    and Grafana's level filter read `level`. Tracebacks go into the err= field."""

    def format(self, record):
        line = (f"ts={datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec='milliseconds')} "
                f"level={record.levelname.lower()} logger={record.name} "
                f"msg={json.dumps(record.getMessage(), ensure_ascii=False)}")
        if record.exc_info:
            line += f" err={json.dumps(self.formatException(record.exc_info))}"
        return line


def setup_logging():
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(LogfmtFormatter())
    level = os.environ.get("CNAV_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=getattr(logging, level, logging.INFO), handlers=[handler], force=True)


log = logging.getLogger("cnav")
http_log = logging.getLogger("cnav.http")

STATIC_DIR = Path(os.environ.get("CNAV_STATIC", "/app/static"))
CONFIG_DIR = Path(os.environ.get("CNAV_CONFIG", "/etc/cnav"))
# enabled optional components of the cluster's profile (ConfigMap localdev-profile, written by the
# cluster:create / cluster:profile tasks). Missing file = show everything.
PROFILE_FILE = Path(os.environ.get("CNAV_PROFILE", "/etc/localdev-profile/components"))


def enabled_components():
    try:
        return set(PROFILE_FILE.read_text().split())
    except OSError:
        log.debug("no profile file at %s: showing every component", PROFILE_FILE)
        return None


def for_profile(items):
    """Drop cards/status items tagged `tool: <component>` whose component isn't in the profile."""
    enabled = enabled_components()
    if enabled is None:
        return items
    return [i for i in items if not isinstance(i, dict) or not i.get("tool") or i["tool"] in enabled]
# the cluster's k3d config (host port mappings), mounted from the node's /localdev/k3d.yaml
K3D_CONFIG = Path(os.environ.get("CNAV_K3D_CONFIG", "/etc/localdev/k3d.yaml"))
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
    except Exception as e:
        log.debug("pod metrics unavailable for %s (%s): %s", ns, selector, e)
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
            log.warning("check %s %s/%s for %r failed: %s", kind, c.get("namespace", "-"), c.get("name"),
                        item.get("name"), e)
            r = result("red", f"check error: {e}"[:160])
        results.append(r)

    worst = max(results, key=lambda r: RANK[r["state"]], default=result("amber", "no checks"))
    # namespace(s) of the item's workloads (HelmChart checks live in kube-system; not shown)
    namespaces = list(dict.fromkeys(c["namespace"] for c in item.get("checks", [])
                                    if c.get("kind") in WORKLOADS and c.get("namespace")))
    counted = [r for r in results if r["desired"] is not None]
    mems = [r["mem"] for r in results if r["mem"] is not None]
    return {
        "name": item.get("name", "?"),
        "namespace": item.get("namespace", ",".join(namespaces)),
        "state": worst["state"],
        "reason": worst["reason"],
        "ready": sum(r["ready"] for r in counted) if counted else None,
        "desired": sum(r["desired"] for r in counted) if counted else None,
        "mem": sum(mems) if mems else None,
        "link": item.get("link", link),
    }


def node_memory(kube):
    """(working-set bytes, allocatable bytes) summed over nodes; (None, None) if unavailable.
    Unlike the pod sums, this includes k3s itself, containerd, kernel and active file cache."""
    try:
        used = sum(quantity_bytes(n["usage"]["memory"])
                   for n in (kube.get("/apis/metrics.k8s.io/v1beta1/nodes") or {}).get("items", []))
        alloc = sum(quantity_bytes(n["status"]["allocatable"]["memory"])
                    for n in (kube.get("/api/v1/nodes") or {}).get("items", []))
        return used or None, alloc or None
    except Exception as e:
        log.debug("node memory unavailable: %s", e)
        return None, None


_last_states, _states_lock = {}, threading.Lock()


def log_transitions(groups):
    """Log status changes once (not on every poll): red = warning, amber or recovery = info."""
    with _states_lock:
        for g in groups:
            for i in g["items"]:
                key, state = (g["group"], i["name"]), i["state"]
                before = _last_states.get(key)
                _last_states[key] = state
                if state == before or (before is None and state == "green"):
                    continue
                level = logging.WARNING if state == "red" else logging.INFO
                log.log(level, "status %s/%s: %s -> %s%s", g["group"], i["name"], before or "new", state,
                        f" ({i['reason']})" if i["reason"] else "")


def status():
    config = yaml.safe_load((CONFIG_DIR / "status.yaml").read_text()) or []
    for g in config:
        g["items"] = for_profile(g.get("items", []))
    config = [g for g in config if g["items"]]
    kube = Kube()
    groups = []
    for g in config:
        items = [evaluate_item(kube, i) for i in g.get("items", [])]
        state = max((i["state"] for i in items), key=RANK.get, default="green")
        mem = sum(i["mem"] for i in items if i["mem"] is not None)
        groups.append({"group": g.get("group", "?"), "state": state, "mem": mem, "items": items})
    log_transitions(groups)
    node_used, node_alloc = node_memory(kube)
    return {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "components": sorted(enabled_components() or []),
        "mem": sum(g["mem"] for g in groups),
        "node_mem": node_used,
        "node_alloc": node_alloc,
        "groups": groups,
    }


# --- ports ------------------------------------------------------------------

def parse_k3d_port(spec):
    """k3d port spec "[HOST:][HOSTPORT:]CONTAINERPORT[/PROTO][@FILTER]" -> (host ip, host port, node port, proto)."""
    spec = spec.split("@", 1)[0]
    spec, _, proto = spec.partition("/")
    parts = spec.split(":")
    host_port = parts[-2] if len(parts) >= 2 else None    # none = docker picks a random port
    host_ip = parts[-3] if len(parts) >= 3 else None
    return host_ip, host_port, int(parts[-1]), (proto or "tcp").upper()


def endpoints_ready(kube, ns, svc):
    """(ready, total) endpoints of a Service, from its EndpointSlices."""
    slices = (kube.get(f"/apis/discovery.k8s.io/v1/namespaces/{ns}/endpointslices",
                       labelSelector=f"kubernetes.io/service-name={svc}") or {}).get("items", [])
    eps = [e for sl in slices for e in sl.get("endpoints") or []]
    return sum(1 for e in eps if (e.get("conditions") or {}).get("ready")), len(eps)


def node_listener(services, node_port, proto):
    """The Service answering on a node port: a LoadBalancer port (k3s's klipper-lb binds it on the
    node) or a NodePort. Returns (service, its port entry) or (None, None)."""
    for s in services:
        typ = s["spec"].get("type")
        for sp in s["spec"].get("ports", []):
            if sp.get("protocol", "TCP") != proto:
                continue
            if (typ == "LoadBalancer" and sp.get("port") == node_port) or \
               (typ in ("LoadBalancer", "NodePort") and sp.get("nodePort") == node_port):
                return s, sp
    return None, None


def registry_up(host_port):
    try:   # the k3d registry container listens on :5000; k3d adds its name to CoreDNS
        with urllib.request.urlopen("http://localdev-registry:5000/v2/", timeout=2):
            return True
    except Exception as e:
        log.debug("registry not reachable: %s", e)
        return False


def ports():
    """Host ports k3d exposes (k3d.yaml) and what answers each one inside the cluster."""
    cfg = yaml.safe_load(K3D_CONFIG.read_text()) or {}
    kube = Kube()
    services = (kube.get("/api/v1/services") or {}).get("items", [])
    out = []
    for p in cfg.get("ports") or []:
        host_ip, host_port, node_port, proto = parse_k3d_port(str(p.get("port", "")))
        entry = {"host": f"{host_ip or 'localhost'}:{host_port or '(random)'}", "proto": proto,
                 "node_port": node_port, "via": ",".join(p.get("nodeFilters") or [])}
        svc, sp = node_listener(services, node_port, proto)
        if svc is None:
            entry.update(state="none", reason=f"nothing listens on node port {node_port} (not deployed?)")
        else:
            ns, name = svc["metadata"]["namespace"], svc["metadata"]["name"]
            ready, total = endpoints_ready(kube, ns, name)
            entry.update(namespace=ns, service=name, port_name=sp.get("name"), svc_port=sp.get("port"),
                         target=str(sp.get("targetPort", sp.get("port"))), ready=ready, endpoints=total,
                         state="green" if ready else "red",
                         reason="" if ready else "no ready endpoints",
                         link=f"{HEADLAMP}/services/{ns}/{name}")
        out.append(entry)
    reg = (cfg.get("registries") or {}).get("create") or {}
    if reg.get("hostPort"):
        up = registry_up(reg["hostPort"])
        out.append({"host": f"localhost:{reg['hostPort']}", "proto": "TCP", "via": "docker",
                    "service": reg.get("name", "registry"), "namespace": None, "target": "5000",
                    "external": True, "state": "green" if up else "red",
                    "reason": "" if up else "registry not reachable",
                    "note": "image registry (docker container, outside the cluster)"})
    return {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "ports": out}


# --- http -------------------------------------------------------------------

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(STATIC_DIR), **kw)

    no_store = False

    def end_headers(self):
        if self.no_store:
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

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
                log.exception("/api/status failed")
                return self.send_json(503, {"error": str(e)})
        if path == "/api/ports":
            try:
                return self.send_json(200, ports())
            except Exception as e:
                log.exception("/api/ports failed")
                return self.send_json(503, {"error": str(e)})
        if path in ("/cards.yaml", "/status.yaml"):
            f = CONFIG_DIR / path.lstrip("/")
            data = f.read_bytes()
            if path == "/cards.yaml":   # only cards for tools in the cluster's profile
                cards = yaml.safe_load(data) or []
                data = yaml.safe_dump(for_profile(cards), sort_keys=False, allow_unicode=True).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/yaml; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path in ("/", "/index.html") or not (STATIC_DIR / path.lstrip("/")).is_file():
            self.no_store = True   # the page must always go through SSO, never come from cache
        if path == "/signed-out":
            self.path = "/signed-out.html"  # landing page after /oauth2/sign_out (outside SSO)
        elif not (STATIC_DIR / path.lstrip("/")).is_file():
            self.path = "/index.html"  # catch-all: unknown paths land on the navigator
        return super().do_GET()

    # access log: errors loud, the page's own 10-second polling and probes only at debug
    QUIET = ("/healthz", "/api/status", "/api/ports")

    def log_request(self, code="-", size="-"):
        try:
            code = int(code)   # HTTPStatus or int; "-" when unknown
        except (TypeError, ValueError):
            code = 0
        parts = self.requestline.split()   # as requested (self.path may be rewritten, e.g. catch-all)
        target = parts[1] if len(parts) > 1 else self.path
        level = (logging.ERROR if code >= 500 and code not in (501, 505)   # 501/505: the client's fault
                 else logging.WARNING if code >= 400
                 else logging.DEBUG if urllib.parse.urlparse(target).path in self.QUIET else logging.INFO)
        user = self.headers.get("X-Auth-Request-Email") or self.headers.get("X-Auth-Request-User") or "-"
        http_log.log(level, "%s %s %s user=%s client=%s", self.command, target, code, user,
                     self.headers.get("X-Forwarded-For", self.client_address[0]))

    def log_error(self, fmt, *args):   # detail of a send_error(); log_request reports the request
        http_log.debug(fmt, *args)

    def log_message(self, fmt, *args):
        http_log.info(fmt, *args)


if __name__ == "__main__":
    setup_logging()

    def stop(signum, _frame):   # PID 1 gets no default SIGTERM handling: exit now, not after 30s
        log.info("received %s, shutting down", signal.Signals(signum).name)
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    log.info("cnav listening on :%d (config %s, log level %s)", PORT, CONFIG_DIR,
             logging.getLevelName(logging.getLogger().level).lower())
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
