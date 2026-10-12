"""usage cluster-agent: watches a Kubernetes cluster and sends usage events to a usage server.

Stdlib only. Runs as one pod in kube-system. Events (see ../../openapi.yaml):
  cluster.created                        once per cluster (type, role, platform, versions, ...)
  cluster.updated                        the cluster's facts changed or became known (backfill)
  cluster.heartbeat                      every HEARTBEAT_S, with nodes/cores/memory/pods/namespaces
  cluster.event  profile.changed         the localdev profile or its components changed
  cluster.event  app.deployed            a Deployment in a user namespace was created or changed

Best-effort delivery: events are spooled as files in SPOOL_DIR (ephemeral) and POSTed in batches.
Most event ids are derived from the cluster id (uuid5), so a restarted agent rebuilds the same
events and the server drops the repeats: no persistent state needed.

Cluster facts the API can't tell, best effort: on localdev, ConfigMap kube-system/localdev-host
(written by localdev's tasks: username, cluster_type, cluster_role, host OS, k3d version) and
cnav/localdev-profile (profile, components). Elsewhere, CLUSTER_TYPE / CLUSTER_ROLE /
CLUSTER_USERNAME / CLUSTER_PLATFORM env. The ConfigMap wins over env; anything still missing is
"unknown" (optional facts are left out). The agent starts sending right away, and when the facts
change (e.g. the ConfigMap is created later: a backfill) it sends cluster.updated with what changed.
On start it also sends cluster.updated with all current facts; its id is derived from the facts, so
the server drops it when it already has them, and catches up when they changed while the agent was down.

Environment (defaults): USAGE_URL (http://usage.usage.svc), POLL_S (30), FLUSH_S (30),
HEARTBEAT_S (900), SPOOL_DIR (/spool), SPOOL_MAX_FILES (1000), SPOOL_MAX_AGE_S (259200),
HTTP_TIMEOUT_S (5), LOG_LEVEL (info), CLUSTER_TYPE, CLUSTER_ROLE, CLUSTER_USERNAME (all: unknown),
CLUSTER_PLATFORM (derived from the kubelet version).
"""
import hashlib
import json
import logging
import os
import signal
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.3.0"
SOURCE = {"name": "localdev-cluster-agent", "version": VERSION}
UNKNOWN = "unknown"                             # the API's value for a fact nobody reported (yet)
# facts cluster.updated can carry (the envelope's username goes with them)
UPDATABLE = ("cluster_type", "cluster_role", "platform", "platform_version", "kubernetes_version", "arch",
             "os", "os_variant", "os_version")
ADDON_LABEL = "objectset.rio.cattle.io/hash"   # k3s puts it on everything localdev's manifests create
SYSTEM_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease"}
MAX_BATCH = 500                                 # the API's limit

log = logging.getLogger("usage-agent")


# --- config and logging -------------------------------------------------------

@dataclass
class Config:
    usage_url: str = field(default_factory=lambda: os.environ.get("USAGE_URL", "http://usage.usage.svc").rstrip("/"))
    poll_s: int = field(default_factory=lambda: int(os.environ.get("POLL_S", "30")))
    flush_s: int = field(default_factory=lambda: int(os.environ.get("FLUSH_S", "30")))
    heartbeat_s: int = field(default_factory=lambda: int(os.environ.get("HEARTBEAT_S", "900")))
    spool_dir: Path = field(default_factory=lambda: Path(os.environ.get("SPOOL_DIR", "/spool")))
    spool_max_files: int = field(default_factory=lambda: int(os.environ.get("SPOOL_MAX_FILES", "1000")))
    spool_max_age_s: int = field(default_factory=lambda: int(os.environ.get("SPOOL_MAX_AGE_S", "259200")))
    http_timeout_s: float = field(default_factory=lambda: float(os.environ.get("HTTP_TIMEOUT_S", "5")))
    # cluster facts for clusters without kube-system/localdev-host (empty: unknown)
    cluster_type: str = field(default_factory=lambda: os.environ.get("CLUSTER_TYPE", ""))
    cluster_role: str = field(default_factory=lambda: os.environ.get("CLUSTER_ROLE", ""))
    cluster_username: str = field(default_factory=lambda: os.environ.get("CLUSTER_USERNAME", ""))
    cluster_platform: str = field(default_factory=lambda: os.environ.get("CLUSTER_PLATFORM", ""))


class LogfmtFormatter(logging.Formatter):
    """One logfmt line per record (ts=... level=info logger=usage-agent msg="...")."""

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
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=getattr(logging, level, logging.INFO), handlers=[handler], force=True)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --- kubernetes API -----------------------------------------------------------

class Kube:
    """Minimal read-only client: the pod's service account, urllib, 404 -> None."""
    SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")

    def __init__(self):
        host = os.environ.get("KUBERNETES_SERVICE_HOST")
        if not host:
            raise RuntimeError("not running in a cluster (KUBERNETES_SERVICE_HOST unset)")
        self.base = f"https://{host}:{os.environ.get('KUBERNETES_SERVICE_PORT', '443')}"
        self.ctx = ssl.create_default_context(cafile=str(self.SA / "ca.crt"))

    def get(self, path, accept="application/json", **query):
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        token = (self.SA / "token").read_text()  # re-read: projected tokens rotate
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": accept})
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=10) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise


# --- events (pure functions) --------------------------------------------------

# list calls that only need a count: ask for metadata only, not whole objects
METADATA_ONLY = "application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1,application/json"
UNITS = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12}


def quantity_bytes(q: str) -> int:
    """A k8s memory quantity ('32486696Ki', '16Gi', '123456') in bytes."""
    for suffix in sorted(UNITS, key=len, reverse=True):
        if q.endswith(suffix):
            return int(float(q[: -len(suffix)]) * UNITS[suffix])
    return int(float(q))


def quantity_cores(q: str) -> float:
    """A k8s CPU quantity ('24', '8000m', '0.5') in cores."""
    return float(q[:-1]) / 1000 if q.endswith("m") else float(q)


def platform_from(kubelet_version: str) -> str:
    """Best guess at the distribution from the kubelet version (v1.36.5+k3s1, v1.35.2-eks-...)."""
    for token in ("k3s", "rke2", "eks", "gke", "aks"):
        if token in kubelet_version:
            return token
    return "kubernetes"


def event_id(cluster_id: str, key: str) -> str:
    """Deterministic per cluster: the same fact always gets the same id."""
    return str(uuid.uuid5(uuid.UUID(cluster_id), key))


def envelope(etype, eid, occurred_at, username, data):
    return {"event_id": eid, "type": etype, "occurred_at": occurred_at, "username": username,
            "source": dict(SOURCE), "data": data}


def created_event(cluster_id, created_at, facts, profile, components, node_info):
    """facts: username, cluster_type, cluster_role, platform, and optionally platform_version and
    the localdev host facts (os, os_variant, os_version). profile/components: None outside localdev."""
    data = {
        "cluster_id": cluster_id,
        "cluster_type": facts["cluster_type"],
        "cluster_role": facts["cluster_role"],
        "platform": facts["platform"],
        "kubernetes_version": node_info["kubeletVersion"],
        "arch": node_info["architecture"],
        "platform_version": facts.get("platform_version"),
        "profile": profile,
        "components": components,
        "os": facts.get("os"),
        "os_variant": facts.get("os_variant"),
        "os_version": facts.get("os_version"),
    }
    data = {k: v for k, v in data.items() if v not in (None, "")}   # optional fields: omit when unknown
    return envelope("cluster.created", event_id(cluster_id, "created"), created_at, facts["username"], data)


def stated_facts(facts, node_info):
    """The facts as cluster.created / cluster.updated state them: unknown optional ones left out."""
    out = {
        "cluster_type": facts["cluster_type"], "cluster_role": facts["cluster_role"], "platform": facts["platform"],
        "platform_version": facts.get("platform_version"), "kubernetes_version": node_info["kubeletVersion"],
        "arch": node_info["architecture"],
        "os": facts.get("os"), "os_variant": facts.get("os_variant"), "os_version": facts.get("os_version"),
    }
    return {k: v for k, v in out.items() if v not in (None, "")}


def updated_event(cluster_id, username, stated, changed=None):
    """cluster.updated with `changed` (default: all) of the stated facts. The id is derived from the
    full facts + username, so the same state always gets the same id (the server drops repeats)."""
    digest = hashlib.sha256(json.dumps([username, stated], sort_keys=True).encode()).hexdigest()[:16]
    fields = stated if changed is None else {k: stated[k] for k in changed if k in stated}
    return envelope("cluster.updated", event_id(cluster_id, f"updated/{digest}"), now_iso(), username,
                    {"cluster_id": cluster_id, **fields})


def cluster_event(cluster_id, eid, occurred_at, username, name, attributes):
    return envelope("cluster.event", eid, occurred_at, username,
                    {"cluster_id": cluster_id, "name": name, "attributes": attributes})


def heartbeat_event(cluster_id, username, stats):
    return envelope("cluster.heartbeat", str(uuid.uuid4()), now_iso(), username, {"cluster_id": cluster_id, **stats})


def cluster_stats(kube):
    """Size of the cluster right now: nodes, summed CPU/memory capacity, pods, namespaces."""
    nodes = (kube.get("/api/v1/nodes") or {}).get("items", [])
    capacity = [n.get("status", {}).get("capacity", {}) for n in nodes]
    pods = (kube.get("/api/v1/pods", accept=METADATA_ONLY) or {}).get("items", [])
    namespaces = (kube.get("/api/v1/namespaces", accept=METADATA_ONLY) or {}).get("items", [])
    cores = sum(quantity_cores(c.get("cpu", "0")) for c in capacity)
    return {"nodes": len(nodes), "cores": int(cores) if cores.is_integer() else round(cores, 3),
            "memory_bytes": sum(quantity_bytes(c.get("memory", "0")) for c in capacity),
            "pods": len(pods), "namespaces": len(namespaces)}


def parse_profile(cm):
    """(profile, components, resourceVersion) from ConfigMap cnav/localdev-profile."""
    data = cm.get("data") or {}
    components = sorted(set((data.get("components") or "").split()))
    return data.get("profile") or "unknown", components, cm["metadata"].get("resourceVersion", "")


def user_namespaces(namespaces):
    """Namespaces made by people (kubectl, helm, kn), not by localdev's manifests or k8s itself."""
    return {ns["metadata"]["name"] for ns in namespaces.get("items", [])
            if ns["metadata"]["name"] not in SYSTEM_NAMESPACES
            and ADDON_LABEL not in (ns["metadata"].get("labels") or {})}


def app_events(cluster_id, username, deployments, user_ns, seen):
    """app.deployed for each Deployment generation in a user namespace not yet in `seen` (updated)."""
    events = []
    for d in deployments.get("items", []):
        meta = d["metadata"]
        if meta["namespace"] not in user_ns:
            continue
        gen = meta.get("generation", 1)
        key = (meta["uid"], gen)
        if key in seen:
            continue
        seen.add(key)
        containers = d.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        occurred = meta.get("creationTimestamp") if gen == 1 else None
        events.append(cluster_event(
            cluster_id, event_id(cluster_id, f"app/{meta['uid']}/{gen}"), occurred or now_iso(), username,
            "app.deployed",
            {"namespace": meta["namespace"], "name": meta["name"],
             "images": ",".join(c.get("image", "?") for c in containers)[:1024],
             "replicas": d.get("spec", {}).get("replicas", 1), "generation": gen}))
    return events


# --- spool and delivery -------------------------------------------------------

class Spool:
    """One JSON file per event, oldest first by name: <epoch_ms>-<seq>-<event_id>.json (seq keeps
    events from the same millisecond in emit order)."""

    def __init__(self, path: Path, max_files: int, max_age_s: int):
        self.path, self.max_files, self.max_age_s = Path(path), max_files, max_age_s
        self.path.mkdir(parents=True, exist_ok=True)
        self.seq = 0

    def files(self):
        return sorted(self.path.glob("*.json"))

    def put(self, event):
        self.seq = (self.seq + 1) % 1_000_000
        name = f"{int(time.time() * 1000):013d}-{self.seq:06d}-{event['event_id']}.json"
        tmp = self.path / f".{name}.tmp"
        tmp.write_text(json.dumps(event))
        tmp.rename(self.path / name)

    def oldest(self, n):
        """[(path, event)] for up to n of the oldest events; unreadable files are dropped."""
        out = []
        for f in self.files()[:n]:
            try:
                out.append((f, json.loads(f.read_text())))
            except (OSError, ValueError) as e:
                log.warning(f"dropping unreadable spool file {f.name}: {e}")
                f.unlink(missing_ok=True)
        return out

    def prune(self):
        cutoff_ms = (time.time() - self.max_age_s) * 1000
        files = self.files()
        old = [f for f in files if int(f.name.split("-", 1)[0]) < cutoff_ms]
        rest = [f for f in files if f not in old]
        over = rest[:max(0, len(rest) - self.max_files)]
        for f in old + over:
            f.unlink(missing_ok=True)
        if old or over:
            log.warning(f"spool pruned: {len(old)} expired, {len(over)} over the {self.max_files}-file limit")

    def __len__(self):
        return len(self.files())


def post_batch(url, events, timeout):
    """POST /v1/events. Returns (status, json body or None). Raises OSError on connection failure."""
    body = json.dumps({"events": events}).encode()
    req = urllib.request.Request(f"{url}/v1/events", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except ValueError:
            return e.code, None


class Flusher:
    def __init__(self, spool: Spool, url: str, timeout: float):
        self.spool, self.url, self.timeout = spool, url, timeout
        self.reachable = None  # unknown until the first attempt

    def flush(self):
        """Send spooled events until the spool is empty or the server is unavailable."""
        while batch := self.spool.oldest(MAX_BATCH):
            paths, events = [p for p, _ in batch], [e for _, e in batch]
            try:
                status, body = post_batch(self.url, events, self.timeout)
            except (OSError, ValueError) as e:   # URLError, timeouts, refused connections
                self._down(f"{type(e).__name__}: {getattr(e, 'reason', e)}")
                return
            if status == 202:
                self._up()
                for p in paths:
                    p.unlink(missing_ok=True)
                body = body or {}
                log.info(f"sent {len(events)} event(s): {body.get('accepted', '?')} new, "
                         f"{body.get('duplicates', '?')} duplicate(s); {len(self.spool)} left")
            elif status == 422:
                self._up()
                bad = sorted({d["index"] for d in (body or {}).get("details", []) if "index" in d})
                drop = [paths[i] for i in bad if i < len(paths)] or paths  # no index: the batch itself
                for p in drop:
                    p.unlink(missing_ok=True)
                first = ((body or {}).get("details") or [{}])[0]
                log.warning(f"server rejected {len(drop)} event(s), dropped them (first: {first})")
            elif 400 <= status < 500:
                self._up()
                for p in paths:
                    p.unlink(missing_ok=True)
                log.warning(f"server answered HTTP {status}: dropped {len(paths)} event(s) ({body})")
            else:
                self._down(f"HTTP {status}")
                return

    def _down(self, why):
        if self.reachable is not False:
            log.warning(f"usage server {self.url} unavailable ({why}); keeping {len(self.spool)} event(s) spooled")
        self.reachable = False

    def _up(self):
        if self.reachable is False:
            log.info(f"usage server {self.url} reachable again")
        self.reachable = True


# --- the agent ----------------------------------------------------------------

class Agent:
    def __init__(self, cfg: Config, kube, spool: Spool, flusher: Flusher, clock=time.monotonic):
        self.cfg, self.kube, self.spool, self.flusher, self.clock = cfg, kube, spool, flusher, clock
        self.cluster_id = self.created_at = None
        self.created_sent = False
        self.stated = None              # (username, facts) last sent in created/updated
        self.profile_seen = None        # (profile, components) last seen
        self.apps_seen = set()          # (deployment uid, generation)
        self.next_heartbeat = 0.0
        self.waiting_logged = False

    def emit(self, event):
        self.spool.put(event)
        d = event["data"]
        what = d.get("name") or event["type"]
        detail = d.get("attributes") or {k: v for k, v in d.items() if k != "cluster_id" and not isinstance(v, list)}
        log.info(f"event {what}: {detail}")

    def facts(self, host_cm):
        """Cluster facts, best effort: kube-system/localdev-host (localdev) over env (other clusters),
        else unknown."""
        host = (host_cm or {}).get("data") or {}
        return {
            "username": host.get("username") or self.cfg.cluster_username or UNKNOWN,
            "cluster_type": host.get("cluster_type") or self.cfg.cluster_type or UNKNOWN,
            "cluster_role": host.get("cluster_role") or self.cfg.cluster_role or UNKNOWN,
            "platform": host.get("platform") or self.cfg.cluster_platform,
            **{k: host[k] for k in ("platform_version", "os", "os_variant", "os_version") if host.get(k)},
        }

    def poll(self):
        if self.cluster_id is None:
            ns = self.kube.get("/api/v1/namespaces/kube-system")
            self.cluster_id, self.created_at = ns["metadata"]["uid"], ns["metadata"]["creationTimestamp"]
            log.info(f"cluster id {self.cluster_id} (created {self.created_at})")

        host_cm = self.kube.get("/api/v1/namespaces/kube-system/configmaps/localdev-host")
        facts = self.facts(host_cm)
        if host_cm is None and not self.waiting_logged:
            log.info("no ConfigMap kube-system/localdev-host: reporting what env and the API tell "
                     f"(type {facts['cluster_type']}, role {facts['cluster_role']}); "
                     "cluster.updated follows when it appears")
            self.waiting_logged = True
        elif host_cm is not None:
            self.waiting_logged = False
        username = facts["username"]
        profile_cm = self.kube.get("/api/v1/namespaces/cnav/configmaps/localdev-profile")
        profile, components, rv = parse_profile(profile_cm) if profile_cm else (None, None, None)

        node = (self.kube.get("/api/v1/nodes") or {}).get("items", [{}])[0]["status"]["nodeInfo"]
        guessed_platform = not facts["platform"]          # from the kubelet version: a fallback guess
        facts["platform"] = facts["platform"] or platform_from(node["kubeletVersion"])
        stated = stated_facts(facts, node)
        if not self.created_sent:
            self.emit(created_event(self.cluster_id, self.created_at, facts, profile, components, node))
            # and the current facts as an update: a no-op for the server if it has them, a catch-up
            # if they changed while this agent wasn't running (created is a duplicate then)
            # only known facts: an unknown or guessed one must not overwrite what the server may have
            self.emit(updated_event(self.cluster_id, username, stated,
                                    [k for k in UPDATABLE if k in stated and stated[k] != UNKNOWN
                                     and not (k == "platform" and guessed_platform)]))
            self.created_sent = True
        else:
            # known facts are sticky: a fact that goes back to unknown (or away, e.g. the ConfigMap was
            # deleted) keeps its last known value instead of overwriting it
            prev_user, prev = self.stated
            stated = {**prev, **{k: v for k, v in stated.items() if v != UNKNOWN or prev.get(k, UNKNOWN) == UNKNOWN}}
            if guessed_platform:              # nor does a guess replace a reported platform
                stated["platform"] = prev.get("platform", stated["platform"])
            if username == UNKNOWN:
                username = prev_user
            changed = [k for k in UPDATABLE if k in stated and stated.get(k) != prev.get(k)]
            if changed or username != prev_user:
                # username alone: still needs a fact in data (minProperties); cluster_type is always there
                self.emit(updated_event(self.cluster_id, username, stated, changed or ["cluster_type"]))
        self.stated = (username, stated)

        if profile_cm:
            current = (profile, tuple(components))
            if self.profile_seen is not None and current != self.profile_seen:
                self.emit(cluster_event(self.cluster_id, event_id(self.cluster_id, f"profile/{rv}"), now_iso(),
                                        username, "profile.changed",
                                        {"profile": profile, "components": ",".join(components)}))
            self.profile_seen = current

        user_ns = user_namespaces(self.kube.get("/api/v1/namespaces") or {})
        for ev in app_events(self.cluster_id, username, self.kube.get("/apis/apps/v1/deployments") or {},
                             user_ns, self.apps_seen):
            self.emit(ev)

        if self.clock() >= self.next_heartbeat:
            self.emit(heartbeat_event(self.cluster_id, username, cluster_stats(self.kube)))
            self.next_heartbeat = self.clock() + self.cfg.heartbeat_s

    def run(self, stop: threading.Event):
        log.info(f"usage-agent {VERSION}: server {self.cfg.usage_url}, spool {self.cfg.spool_dir}, "
                 f"poll {self.cfg.poll_s}s, flush {self.cfg.flush_s}s, heartbeat {self.cfg.heartbeat_s}s")
        next_poll = next_flush = 0.0
        while not stop.is_set():
            now = self.clock()
            if now >= next_poll:
                self._step("poll", self.poll)
                next_poll = now + self.cfg.poll_s
            if now >= next_flush:
                self._step("prune", self.spool.prune)
                self._step("flush", self.flusher.flush)
                next_flush = now + self.cfg.flush_s
            stop.wait(1)
        self._step("final flush", self.flusher.flush)
        log.info("stopped")

    @staticmethod
    def _step(name, fn):
        try:
            fn()
        except Exception:  # noqa: BLE001 - keep the loop alive; best-effort by design
            log.exception(f"{name} failed")


def main():
    setup_logging()
    cfg = Config()
    spool = Spool(cfg.spool_dir, cfg.spool_max_files, cfg.spool_max_age_s)
    agent = Agent(cfg, Kube(), spool, Flusher(spool, cfg.usage_url, cfg.http_timeout_s))
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):   # PID 1 gets no default SIGTERM handling
        signal.signal(sig, lambda *_: stop.set())
    agent.run(stop)


if __name__ == "__main__":
    main()
