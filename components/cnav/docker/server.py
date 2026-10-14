"""cnav — cluster navigator.

Serves the static page, the config files (cards.yaml, status.yaml),
/cnav/api/status, which evaluates status.yaml against the Kubernetes API using the
pod's service account, /cnav/api/ports, the host ports k3d.yaml exposes and what
answers them in the cluster, /cnav/api/routes, every Ingress / IngressRoute with its host, path,
backend and auth (sso, api-key, open), /cnav/api/events, cluster events from Loki (72h, shipped
by Alloy) or, while Loki doesn't answer, from the Kubernetes API (last hour), and
/cnav/api/credentials, logins for things not behind SSO (credentials.yaml + the secret store;
values only from /cnav/api/credentials/<id>, when the page reveals one).
Stdlib only, plus PyYAML.
"""
import base64
import json
import logging
import os
import re
import signal
import ssl
import sys
import threading
import time
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
LOKI = os.environ.get("CNAV_LOKI", "http://loki.monitoring.svc:3100")

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


COMPONENT_LABEL = "localdev/component"     # on a workload: show it under this component's row
GROUP_LABEL = "localdev/status-group"      # optional: the group for that row (default: apps)


def add_discovered(kube, config):
    """Add a status item per component whose workloads are labelled localdev/component=<c>, so a
    component (including an external one) shows up without editing status.yaml. Its checks are all
    its labelled workloads. Components named by a `tool:` in status.yaml, and workloads it already
    checks, are left to status.yaml."""
    tools = {i.get("tool") for g in config for i in g.get("items", [])}
    checked = {(c.get("kind"), c.get("namespace"), c.get("name"))
               for g in config for i in g.get("items", []) for c in i.get("checks", [])}
    found = {}   # (group, component) -> checks
    for kind, (_, plural) in WORKLOADS.items():
        try:
            objs = (kube.get(f"/apis/apps/v1/{plural}", labelSelector=COMPONENT_LABEL) or {}).get("items", [])
        except Exception as e:  # discovery is extra: never sink the page
            log.warning("discovering labelled %s failed: %s", plural, e)
            continue
        for o in objs:
            meta = o["metadata"]
            comp = meta["labels"][COMPONENT_LABEL]
            if comp in tools or (kind, meta["namespace"], meta["name"]) in checked:
                continue
            group = meta["labels"].get(GROUP_LABEL, "apps")
            found.setdefault((group, comp), []).append({"kind": kind, "namespace": meta["namespace"],
                                                        "name": meta["name"]})
    for (group, comp), checks in sorted(found.items()):
        g = next((g for g in config if g.get("group") == group), None)
        if g is None:
            g = {"group": group, "items": []}
            config.append(g)
        g["items"].append({"name": comp, "checks": sorted(checks, key=lambda c: (c["namespace"], c["name"]))})


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
    kube = Kube()
    add_discovered(kube, config)
    for g in config:
        g["items"] = for_profile(g.get("items", []))
    config = [g for g in config if g["items"]]
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


# --- routes: every Ingress and Traefik IngressRoute, with who may call it ----

MW_ANNOTATION = "traefik.ingress.kubernetes.io/router.middlewares"
AUTH_ORDER = ("sso", "api-key", "forward-auth", "basic-auth")   # the gate a route reports, first found
TRAEFIK_SVC = ("kube-system", "traefik")                         # a route back into Traefik: a re-route


def middleware_info(m):
    """A Traefik Middleware -> {kind, detail}. kind: sso (oauth2-proxy), api-key (api-auth), another
    auth, or the spec's own type (stripPrefix, headers, redirectRegex, ...)."""
    spec = m.get("spec") or {}
    address = (spec.get("forwardAuth") or {}).get("address", "")
    if address:
        kind = "sso" if "oauth2-proxy" in address else "api-key" if "api-auth" in address else "forward-auth"
        return {"kind": kind, "detail": address}
    if "basicAuth" in spec or "digestAuth" in spec:
        return {"kind": "basic-auth", "detail": ""}
    kind = next(iter(spec), "other")
    body = spec.get(kind) or {}
    if kind == "stripPrefix":
        return {"kind": kind, "detail": "strip " + " ".join(body.get("prefixes") or [])}
    if kind == "headers" and (body.get("customRequestHeaders") or {}).get("Host"):
        return {"kind": kind, "detail": f"Host: {body['customRequestHeaders']['Host']}"}
    return {"kind": kind, "detail": kind}


def match_parts(match):
    """IngressRoute match -> (host or None, path or None, exact). Only the simple forms localdev uses;
    anything else keeps the raw rule as the path."""
    host = re.search(r"Host\(`([^`]+)`\)", match)
    prefix = re.search(r"PathPrefix\(`([^`]+)`\)", match)
    exact = re.search(r"Path\(`([^`]+)`\)", match)
    path = prefix.group(1) if prefix else exact.group(1) if exact else None
    if path is None and not host:
        path = match
    return (host.group(1) if host else None), path, bool(exact and not prefix)


def routes():
    """Every route Traefik serves: host, path, auth (sso | api-key | open | ...), backend, middlewares.
    Read live from the cluster, so it follows profile switches."""
    kube = Kube()
    mws = {}
    for m in (kube.get("/apis/traefik.io/v1alpha1/middlewares") or {}).get("items", []):
        mws[(m["metadata"]["namespace"], m["metadata"]["name"])] = middleware_info(m)
    by_ref = {f"{ns}-{name}@kubernetescrd": info for (ns, name), info in mws.items()}

    ready_cache = {}

    def backend_ready(ns, name):
        if (ns, name) not in ready_cache:
            try:
                ready_cache[(ns, name)] = endpoints_ready(kube, ns, name)
            except Exception as e:  # a missing Service: shown as not ready
                log.debug("endpoints of %s/%s: %s", ns, name, e)
                ready_cache[(ns, name)] = (0, 0)
        return ready_cache[(ns, name)]

    def row(kind, ns, name, host, path, exact, middlewares, svc_ns, svc, port, link, priority=None):
        kinds = [m["kind"] for m in middlewares]
        auth = next((a for a in AUTH_ORDER if a in kinds), "open")
        extras = [m["detail"] for m in middlewares if m["kind"] not in AUTH_ORDER]
        reroute = next((m["detail"].split(": ", 1)[1] for m in middlewares
                        if m["kind"] == "headers" and m["detail"].startswith("Host: ")), None) \
            if (svc_ns, svc) == TRAEFIK_SVC else None
        if reroute and auth == "open":
            auth = "re-route"     # the target host's routes decide (e.g. each API's own key)
        ready, total = backend_ready(svc_ns, svc) if svc else (0, 0)
        return {"source": kind, "namespace": ns, "name": name, "host": host, "path": path or "/",
                "exact": exact, "auth": auth, "middlewares": extras, "priority": priority,
                "backend": {"namespace": svc_ns, "service": svc, "port": port},
                "reroute": reroute, "ready": ready, "endpoints": total,
                "state": "green" if ready or reroute else "red", "link": link}

    out = []
    for ing in (kube.get("/apis/networking.k8s.io/v1/ingresses") or {}).get("items", []):
        meta = ing["metadata"]
        ns = meta["namespace"]
        refs = [r.strip() for r in (meta.get("annotations") or {}).get(MW_ANNOTATION, "").split(",") if r.strip()]
        middlewares = [by_ref.get(r, {"kind": "missing", "detail": f"{r} (missing)"}) for r in refs]
        for rule in (ing.get("spec") or {}).get("rules") or []:
            for p in ((rule.get("http") or {}).get("paths")) or []:
                svc = (p.get("backend") or {}).get("service") or {}
                port = (svc.get("port") or {}).get("name") or (svc.get("port") or {}).get("number")
                out.append(row("Ingress", ns, meta["name"], rule.get("host"), p.get("path"),
                               p.get("pathType") == "Exact", middlewares, ns, svc.get("name"), port,
                               f"{HEADLAMP}/ingresses/{ns}/{meta['name']}"))
    for ir in (kube.get("/apis/traefik.io/v1alpha1/ingressroutes") or {}).get("items", []):
        meta = ir["metadata"]
        ns = meta["namespace"]
        for r in (ir.get("spec") or {}).get("routes") or []:
            host, path, exact = match_parts(r.get("match", ""))
            middlewares = [mws.get((m.get("namespace") or ns, m["name"]),
                                   {"kind": "missing", "detail": f"{m['name']} (missing)"})
                           for m in r.get("middlewares") or []]
            svc = (r.get("services") or [{}])[0]
            out.append(row("IngressRoute", ns, meta["name"], host, path, exact, middlewares,
                           svc.get("namespace") or ns, svc.get("name"), svc.get("port"), None,
                           priority=r.get("priority")))

    # a re-route (e.g. localhost/api/* -> api.localhost) makes every route on its target host
    # reachable under its prefix too: list that alias on those routes
    for rr in [r for r in out if r["reroute"]]:
        prefix = rr["path"].rstrip("/")
        for r in out:
            if r["host"] == rr["reroute"] and r["path"] != "/":
                r.setdefault("aliases", []).append(f"{rr['host'] or 'localhost'}{prefix}{r['path']}")

    out.sort(key=lambda r: (r["host"] is not None, r["host"] or "", r["path"] == "/", r["path"]))
    return {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "routes": out}


# --- events -----------------------------------------------------------------

EVENTS_QUERY = '{job="loki.source.kubernetes_events"}'   # Alloy, 20-3-logging.yaml
EVENTS_WINDOW_S = 72 * 3600     # Loki's retention
EVENTS_MAX = 200

# reason -> category (crash, image, unhealthy, deploy); other Warnings are "warning",
# other Normals "routine" (hidden unless ?all=1). "Deployed" is for localdev's own events.
REASONS = {
    "BackOff": "crash", "CrashLoopBackOff": "crash", "OOMKilling": "crash", "Evicted": "crash",
    "ErrImagePull": "image", "ImagePullBackOff": "image", "InspectFailed": "image",
    "Unhealthy": "unhealthy", "FailedScheduling": "unhealthy", "NodeNotReady": "unhealthy",
    "ProbeWarning": "unhealthy", "FailedMount": "unhealthy", "FailedAttachVolume": "unhealthy",
    "ScalingReplicaSet": "deploy", "AppliedManifest": "deploy", "Deployed": "deploy",
}
SEVERITY = {"crash": "red", "image": "red", "unhealthy": "amber", "warning": "amber", "restart": "amber"}
HEADLAMP_ROUTES = {"Pod": "pods", "Deployment": "deployments", "StatefulSet": "statefulsets",
                   "DaemonSet": "daemonsets", "ReplicaSet": "replicasets", "Job": "jobs",
                   "CronJob": "cronjobs", "Service": "services", "Node": "nodes"}

# generated name suffixes use this alphabet (no vowels, no 0/1/3)
_SFX = "[bcdfghjklmnpqrstvwxz2456789]"
_POD_OF_RS = re.compile(rf"^(.+)-{_SFX}{{6,10}}-{_SFX}{{5}}$")   # deployment pod
_POD_OF_STS = re.compile(r"^(.+)-\d+$")                          # statefulset pod
_POD_OF_OTHER = re.compile(rf"^(.+)-{_SFX}{{5}}$")               # daemonset / job pod
_RS = re.compile(rf"^(.+)-{_SFX}{{6,10}}$")


def workload_name(kind, name):
    """The component an object belongs to: pod/replicaset names minus their generated suffixes."""
    if kind == "Pod":
        for rx in (_POD_OF_RS, _POD_OF_STS, _POD_OF_OTHER):
            m = rx.match(name)
            if m:
                return m.group(1)
    elif kind == "ReplicaSet":
        m = _RS.match(name)
        if m:
            return m.group(1)
    return name


def event_category(etype, reason, kind, message):
    if reason in ("Failed", "BackOff") and "pull" in message.lower():   # "Back-off pulling image"
        return "image"
    if reason in REASONS:
        return REASONS[reason]
    if reason == "Killing":   # every rollout stops old containers; only a failed probe is news
        return "restart" if "failed" in message.lower() else "routine"
    if reason in ("SuccessfulCreate", "SuccessfulDelete") and kind in ("StatefulSet", "DaemonSet", "Job"):
        return "deploy"
    if etype == "Warning":
        return "crash" if reason == "Failed" else "warning"
    return "routine"


def normalize_event(ts, etype, reason, kind, name, namespace, message, count):
    category = event_category(etype or "", reason or "", kind or "", message or "")
    route = HEADLAMP_ROUTES.get(kind)
    link = None
    if route and name:
        link = f"{HEADLAMP}/{route}/{namespace}/{name}" if namespace else f"{HEADLAMP}/{route}/{name}"
    return {"ts": ts, "type": category, "severity": SEVERITY.get(category, "normal"),
            "event_type": etype, "reason": reason, "kind": kind, "name": name,
            "namespace": namespace or None, "component": workload_name(kind, name or "") or kind,
            "message": (message or "").strip(), "count": int(count or 1), "link": link}


def iso(ns_epoch):
    return datetime.fromtimestamp(int(ns_epoch) / 1e9, timezone.utc).isoformat(timespec="seconds")


def collapse_into(rows, ev):
    """One row per event: k8s updates an Event in place (count, last time) and Alloy logs each update."""
    key = (ev["namespace"], ev["kind"], ev["name"], ev["reason"], ev["message"])
    seen = rows.get(key)
    if seen is None or (ev["ts"] or "") > (seen["ts"] or ""):
        if seen:
            ev["count"] = max(ev["count"], seen["count"])
        rows[key] = ev
    else:
        seen["count"] = max(ev["count"], seen["count"])


class LokiEvents:
    """Collapsed events from Loki, kept in memory and updated incrementally: the first call backfills
    the 72h window, each later call asks only for lines since the newest one seen (minus a small
    overlap for lines that reach Loki late, deduplicated). A full resync every RESYNC_S self-heals."""

    PAGE = 1000
    OVERLAP_NS = 60 * 10**9
    RESYNC_S = 900
    BACKFILL_MAX = 50_000          # lines; well above what a dev cluster logs in 72h

    def __init__(self):
        self.lock = threading.Lock()
        self.rows = {}
        self.last_ns = 0           # newest line timestamp seen
        self.recent = set()        # (ts_ns, line) inside the overlap window, to skip re-reads
        self.synced = 0.0          # monotonic time of the last full backfill

    def _query(self, **params):
        q = urllib.parse.urlencode({"query": EVENTS_QUERY, "limit": self.PAGE, **params})
        with urllib.request.urlopen(f"{LOKI}/loki/api/v1/query_range?{q}", timeout=4) as r:
            streams = json.load(r)["data"]["result"]
        return [(int(ts), line, st["stream"].get("namespace")) for st in streams for ts, line in st["values"]]

    def _add(self, rows, ts, line, ns):
        try:
            e = json.loads(line)
        except ValueError:
            return
        collapse_into(rows, normalize_event(iso(ts), e.get("type"), e.get("reason"), e.get("kind"),
                                            e.get("name"), ns, e.get("msg"), e.get("count")))

    def _backfill(self, now_ns):
        """Page backwards through the window, newest first."""
        rows, lines, end = {}, [], now_ns + 1
        start = now_ns - EVENTS_WINDOW_S * 10**9
        while len(lines) < self.BACKFILL_MAX:
            page = self._query(start=start, end=end, direction="backward")
            lines += page
            if len(page) < self.PAGE:
                break
            end = min(ts for ts, _, _ in page)   # end is exclusive; lines at exactly this ns are re-read
        for ts, line, ns in lines:
            self._add(rows, ts, line, ns)
        self.rows = rows
        self.last_ns = max((ts for ts, _, _ in lines), default=0)
        self.recent = {(ts, line) for ts, line, _ in lines if ts >= self.last_ns - self.OVERLAP_NS}
        self.synced = time.monotonic()
        log.debug("events: backfilled %d lines into %d rows", len(lines), len(rows))

    def _update(self, now_ns):
        """Lines since the newest seen (forward), page by page."""
        start, added = self.last_ns - self.OVERLAP_NS, 0
        while True:
            page = self._query(start=start, end=now_ns + 1, direction="forward")
            for ts, line, ns in page:
                if (ts, line) in self.recent:
                    continue
                self.recent.add((ts, line))
                added += 1
                self._add(self.rows, ts, line, ns)
                self.last_ns = max(self.last_ns, ts)
            if len(page) < self.PAGE:
                break
            start = max(ts for ts, _, _ in page)
        floor = self.last_ns - self.OVERLAP_NS
        self.recent = {k for k in self.recent if k[0] >= floor}
        if added:
            log.debug("events: %d new lines", added)

    def rows_now(self):
        """Current collapsed rows (raises when Loki doesn't answer)."""
        with self.lock:
            now_ns = time.time_ns()
            if not self.last_ns or time.monotonic() - self.synced > self.RESYNC_S:
                self._backfill(now_ns)
            else:
                self._update(now_ns)
            cutoff = iso(now_ns - EVENTS_WINDOW_S * 10**9)
            self.rows = {k: ev for k, ev in self.rows.items() if (ev["ts"] or "") >= cutoff}
            return [dict(ev) for ev in self.rows.values()]   # copies: later updates mutate rows


loki_events = LokiEvents()


def events_from_kube():
    """Events still held by the API server (it drops them after an hour)."""
    out = []
    for e in (Kube().get("/api/v1/events") or {}).get("items", []):
        obj = e.get("involvedObject") or {}
        ts = (e.get("lastTimestamp") or (e.get("series") or {}).get("lastObservedTime")
              or e.get("eventTime") or e["metadata"].get("creationTimestamp"))
        if ts:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat(timespec="seconds")
        out.append(normalize_event(ts, e.get("type"), e.get("reason"), obj.get("kind"), obj.get("name"),
                                   obj.get("namespace"), e.get("message"), e.get("count")))
    return out


def events(include_routine=False):
    source = "loki"
    try:
        rows = loki_events.rows_now()
    except Exception as e:
        log.info("events: loki not answering (%s), using the kubernetes api", e)
        rows = []
    if not rows:
        source, rows = "kubernetes", {}
        for ev in events_from_kube():
            collapse_into(rows, ev)
        rows = list(rows.values())
    out = sorted(rows, key=lambda ev: ev["ts"] or "", reverse=True)
    hidden = sum(1 for ev in out if ev["type"] == "routine")
    if not include_routine:
        out = [ev for ev in out if ev["type"] != "routine"]
    return {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source": source,
            "window": "72h" if source == "loki" else "1h", "routine": hidden, "events": out[:EVENTS_MAX]}


# --- credentials --------------------------------------------------------------

STORE_NS = "secret-store"     # the external-secrets component's local store, one Secret per entry
# keys whose values are masked on the page (and only sent on reveal); usernames, db names etc. aren't
SECRET_KEY = re.compile(r"pass|secret|token|api[-_]?key|private|credential", re.I)


def is_secret(key, template=None):
    """A key is secret by name; an extra (a template) is secret if it uses a secret key."""
    if SECRET_KEY.search(key):
        return True
    return bool(template) and any(SECRET_KEY.search(k) for k in re.findall(r"{(\w[\w-]*)}", template))


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def store_entries():
    """{entry: {key: value}} from the secret store's Secrets, minus entries declared by a component
    (label localdev/component, set by store.py) that isn't in the cluster's profile."""
    enabled = enabled_components()
    out = {}
    for sec in (Kube().get(f"/api/v1/namespaces/{STORE_NS}/secrets") or {}).get("items", []):
        owner = (sec["metadata"].get("labels") or {}).get("localdev/component")
        if owner and enabled is not None and owner not in enabled:
            continue
        data = sec.get("data") or {}
        out[sec["metadata"]["name"]] = {k: base64.b64decode(v).decode(errors="replace") for k, v in data.items()}
    return out


def credential_rows(entries):
    """Curated rows (credentials.yaml, for the profile) + any other store entry with a password or
    an API key (api-<name> entries: keys for http://api.localhost/<name>/, see components/api-auth)."""
    try:
        curated = for_profile(yaml.safe_load((CONFIG_DIR / "credentials.yaml").read_text()) or [])
    except OSError:
        curated = []
    rows, used = [], set()
    for c in curated:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        values = dict(c.get("static") or {}) if "static" in c else entries.get(c.get("entry"))
        used.add(c.get("entry"))
        rows.append({"id": slug(c["name"]), "name": c["name"], "where": c.get("where"), "note": c.get("note"),
                     "entry": c.get("entry"), "source": "static" if "static" in c else "entry",
                     "values": values, "extras": c.get("extras") or {}})
    for entry, values in sorted(entries.items()):
        api = entry.startswith("api-") and any(k.startswith("api-key") for k in values)
        if entry not in used and ("password" in values or api):
            rows.append({"id": slug(f"store-{entry}"), "name": entry,
                         "where": f"http://api.localhost/{entry[4:]}/" if api else None,
                         "note": "API key: Authorization: Bearer <key> or X-API-Key: <key>" if api else None,
                         "entry": entry, "source": "discovered", "values": values, "extras": {}})
    return rows


def credentials():
    """The rows with their non-secret values (usernames, db names); secret ones only as key names."""
    out = []
    for r in credential_rows(store_entries()):
        values = r["values"] or {}
        fields = [{"key": k, "secret": is_secret(k), **({} if is_secret(k) else {"value": v})}
                  for k, v in sorted(values.items(), key=lambda kv: (is_secret(kv[0]), kv[0] != "username"))]
        fields += [{"key": k, "secret": True} if is_secret(k, t)
                   else {"key": k, "secret": False, "value": t.format(**values) if values else t}
                   for k, t in r["extras"].items()]
        out.append({"id": r["id"], "name": r["name"], "where": r["where"], "note": r["note"],
                    "entry": r["entry"], "source": r["source"], "missing": r["values"] is None,
                    "fields": fields})
    return {"credentials": out}


def credential_values(cid):
    """One row's values, extras rendered from them; None if there's no such row."""
    for r in credential_rows(store_entries()):
        if r["id"] == cid:
            values = dict(r["values"] or {})
            for name, template in r["extras"].items():
                try:
                    values[name] = template.format(**(r["values"] or {}))
                except (KeyError, IndexError, ValueError) as e:
                    values[name] = f"(template error: {e})"
            return {"id": cid, "values": values}
    return None


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
        if path == "/cnav/api/status":
            try:
                return self.send_json(200, status())
            except Exception as e:
                log.exception("/cnav/api/status failed")
                return self.send_json(503, {"error": str(e)})
        if path == "/cnav/api/ports":
            try:
                return self.send_json(200, ports())
            except Exception as e:
                log.exception("/cnav/api/ports failed")
                return self.send_json(503, {"error": str(e)})
        if path == "/cnav/api/routes":
            try:
                return self.send_json(200, routes())
            except Exception as e:
                log.exception("/cnav/api/routes failed")
                return self.send_json(503, {"error": str(e)})
        if path == "/cnav/api/events":
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                return self.send_json(200, events(include_routine=query.get("all", ["0"])[0] == "1"))
            except Exception as e:
                log.exception("/cnav/api/events failed")
                return self.send_json(503, {"error": str(e)})
        if path == "/cnav/api/credentials" or path.startswith("/cnav/api/credentials/"):
            try:
                if path == "/cnav/api/credentials":
                    return self.send_json(200, credentials())
                body = credential_values(path.rsplit("/", 1)[1])
                return self.send_json(200, body) if body else self.send_json(404, {"error": "no such credential"})
            except Exception as e:
                log.exception("%s failed", path)
                return self.send_json(503, {"error": str(e)})
        if path in ("/cards.yaml", "/status.yaml"):
            f = CONFIG_DIR / path.lstrip("/")
            data = f.read_bytes()
            if path == "/cards.yaml":   # only cards (and card links) for tools in the cluster's profile
                cards = for_profile(yaml.safe_load(data) or [])
                for c in cards:
                    if isinstance(c, dict) and c.get("links"):
                        c["links"] = for_profile(c["links"])
                data = yaml.safe_dump(cards, sort_keys=False, allow_unicode=True).encode()
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
    QUIET = ("/healthz", "/cnav/api/status", "/cnav/api/ports", "/cnav/api/routes", "/cnav/api/events")

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
