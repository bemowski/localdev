"""cluster-agent tests: stdlib unittest. The contract test (events vs ../../openapi.yaml) needs
jsonschema + pyyaml and is skipped without them (`mise run test` provides them)."""
import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "docker"))
import agent  # noqa: E402

SPEC = HERE.parents[1] / "openapi.yaml"
CLUSTER_ID = "1a678a1e-7ac7-4534-9040-6717cc42f427"
HOST = {"username": "alice", "cluster_type": "localdev", "cluster_role": "perftest", "platform": "k3d",
        "os": "windows", "os_variant": "wsl2", "os_version": "Ubuntu 24.04", "platform_version": "v5.9.0"}


class FakeKube:
    def __init__(self):
        self.objects = {
            "/api/v1/namespaces/kube-system": {"metadata": {"uid": CLUSTER_ID,
                                                            "creationTimestamp": "2026-10-10T20:51:43Z"}},
            "/api/v1/namespaces/kube-system/configmaps/localdev-host": {"data": dict(HOST)},
            "/api/v1/namespaces/cnav/configmaps/localdev-profile": {
                "metadata": {"resourceVersion": "100"},
                "data": {"profile": "dev", "components": "postgres\nnats\ndocs\n"}},
            "/api/v1/nodes": {"items": [
                {"status": {"nodeInfo": {"kubeletVersion": "v1.36.5+k3s1", "architecture": "amd64"},
                            "capacity": {"cpu": "24", "memory": "32486696Ki"}}},
                {"status": {"nodeInfo": {"kubeletVersion": "v1.36.5+k3s1", "architecture": "amd64"},
                            "capacity": {"cpu": "1500m", "memory": "2Gi"}}}]},
            "/api/v1/pods": {"items": [{"metadata": {"name": f"p{i}"}} for i in range(7)]},
            "/api/v1/namespaces": {"items": [
                {"metadata": {"name": "kube-system"}},
                {"metadata": {"name": "default"}},
                {"metadata": {"name": "pg", "labels": {agent.ADDON_LABEL: "abc"}}},
                {"metadata": {"name": "team-x", "labels": {"owner": "me"}}}]},
            "/apis/apps/v1/deployments": {"items": [
                deployment("default", "web", "u1", 1, "nginx:1.27"),
                deployment("pg", "pgweb", "u2", 1, "sosedoff/pgweb"),
                deployment("kube-system", "coredns", "u3", 1, "coredns")]},
        }

    def get(self, path, **_query):
        return self.objects.get(path)


def deployment(ns, name, uid, gen, image):
    return {"metadata": {"namespace": ns, "name": name, "uid": uid, "generation": gen,
                         "creationTimestamp": "2026-10-10T21:00:00Z"},
            "spec": {"replicas": 1, "template": {"spec": {"containers": [{"name": name, "image": image}]}}}}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make_agent(tmp, kube=None, url="http://127.0.0.1:9"):
    cfg = agent.Config(usage_url=url, heartbeat_s=900, spool_dir=Path(tmp), spool_max_files=1000,
                       spool_max_age_s=3600, http_timeout_s=2)
    spool = agent.Spool(cfg.spool_dir, cfg.spool_max_files, cfg.spool_max_age_s)
    clock = Clock()
    a = agent.Agent(cfg, kube or FakeKube(), spool, agent.Flusher(spool, url, 2), clock=clock)
    return a, spool, clock


def spooled(spool):
    return [e for _, e in spool.oldest(10_000)]


def names(events):
    return [e["data"].get("name") or e["type"] for e in events]


class PollTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_first_poll_emits_created_apps_and_heartbeat(self):
        a, spool, _ = make_agent(self.tmp)
        a.poll()
        events = spooled(spool)
        self.assertEqual(names(events), ["cluster.created", "app.deployed", "cluster.heartbeat"])
        created = events[0]
        self.assertEqual(created["event_id"], agent.event_id(CLUSTER_ID, "created"))
        self.assertEqual(created["occurred_at"], "2026-10-10T20:51:43Z")
        self.assertEqual(created["username"], "alice")
        d = created["data"]
        self.assertEqual((d["cluster_type"], d["cluster_role"], d["platform"]), ("localdev", "perftest", "k3d"))
        self.assertEqual(d["components"], ["docs", "nats", "postgres"])
        self.assertEqual(d["kubernetes_version"], "v1.36.5+k3s1")
        self.assertEqual(d["os_variant"], "wsl2")
        self.assertEqual(events[1]["data"]["attributes"]["name"], "web")   # only the user namespace
        beat = events[2]["data"]
        self.assertEqual(beat, {"cluster_id": CLUSTER_ID, "nodes": 2, "cores": 25.5,
                                "memory_bytes": 32486696 * 1024 + 2 * 2**30, "pods": 7, "namespaces": 4})

    def test_env_only_cluster(self):
        """No localdev ConfigMaps: type/role/username from env, platform from the kubelet version."""
        kube = FakeKube()
        del kube.objects["/api/v1/namespaces/kube-system/configmaps/localdev-host"]
        del kube.objects["/api/v1/namespaces/cnav/configmaps/localdev-profile"]
        a, spool, _ = make_agent(self.tmp, kube)
        a.cfg.cluster_type, a.cfg.cluster_role = "durable", "prod"
        a.poll()
        created = spooled(spool)[0]
        self.assertEqual(created["username"], "system")
        self.assertEqual(created["data"], {"cluster_id": CLUSTER_ID, "cluster_type": "durable",
                                           "cluster_role": "prod", "platform": "k3s",
                                           "kubernetes_version": "v1.36.5+k3s1", "arch": "amd64"})

    def test_nothing_until_host_configmap_exists(self):
        kube = FakeKube()
        del kube.objects["/api/v1/namespaces/kube-system/configmaps/localdev-host"]
        a, spool, _ = make_agent(self.tmp, kube)
        a.poll()
        self.assertEqual(len(spool), 0)
        kube.objects["/api/v1/namespaces/kube-system/configmaps/localdev-host"] = {"data": dict(HOST)}
        a.poll()
        self.assertIn("cluster.created", names(spooled(spool)))

    def test_later_polls_only_emit_changes(self):
        kube = FakeKube()
        a, spool, clock = make_agent(self.tmp, kube)
        a.poll()
        before = len(spool)
        a.poll()
        self.assertEqual(len(spool), before)   # nothing new, heartbeat not due

        kube.objects["/api/v1/namespaces/cnav/configmaps/localdev-profile"] = {
            "metadata": {"resourceVersion": "101"},
            "data": {"profile": "usage", "components": "postgres\nusage\n"}}
        kube.objects["/apis/apps/v1/deployments"]["items"][0]["metadata"]["generation"] = 2
        clock.t += 901
        a.poll()
        new = spooled(spool)[before:]
        self.assertEqual(names(new), ["profile.changed", "app.deployed", "cluster.heartbeat"])
        self.assertEqual(new[0]["data"]["attributes"], {"profile": "usage", "components": "postgres,usage"})
        self.assertEqual(new[0]["event_id"], agent.event_id(CLUSTER_ID, "profile/101"))
        self.assertEqual(new[1]["data"]["attributes"]["generation"], 2)

    def test_restart_rebuilds_the_same_ids(self):
        a1, s1, _ = make_agent(self.tmp)
        a1.poll()
        a2, s2, _ = make_agent(tempfile.mkdtemp())
        a2.poll()
        ids = lambda s: [e["event_id"] for e in spooled(s) if e["type"] != "cluster.heartbeat"]
        self.assertEqual(ids(s1), ids(s2))


class QuantityTests(unittest.TestCase):
    def test_quantities(self):
        self.assertEqual(agent.quantity_cores("8000m"), 8.0)
        self.assertEqual(agent.quantity_cores("24"), 24.0)
        self.assertEqual(agent.quantity_bytes("32486696Ki"), 32486696 * 1024)
        self.assertEqual(agent.quantity_bytes("16Gi"), 16 * 2**30)
        self.assertEqual(agent.quantity_bytes("1G"), 10**9)
        self.assertEqual(agent.platform_from("v1.35.2-eks-a1b2c3"), "eks")
        self.assertEqual(agent.platform_from("v1.36.0"), "kubernetes")


class SpoolTests(unittest.TestCase):
    def test_prune_by_count_and_age(self):
        tmp = Path(tempfile.mkdtemp())
        spool = agent.Spool(tmp, max_files=3, max_age_s=60)
        old = int((time.time() - 3600) * 1000)
        (tmp / f"{old:013d}-old.json").write_text("{}")
        for i in range(5):
            spool.put({"event_id": f"e{i}"})
            time.sleep(0.002)
        spool.prune()
        self.assertEqual([e["event_id"] for _, e in spool.oldest(10)], ["e2", "e3", "e4"])

    def test_unreadable_files_are_dropped(self):
        tmp = Path(tempfile.mkdtemp())
        spool = agent.Spool(tmp, 10, 60)
        (tmp / "0000000000001-bad.json").write_text("{not json")
        spool.put({"event_id": "ok"})
        self.assertEqual([e["event_id"] for _, e in spool.oldest(10)], ["ok"])
        self.assertEqual(len(spool), 1)


class StubServer:
    """Answers POST /v1/events with queued (status, body) responses; records requests."""

    def __init__(self):
        self.responses, self.requests = [], []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                stub.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                status, body = stub.responses.pop(0) if stub.responses else (202, {"accepted": 0, "duplicates": 0})
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


class FlushTests(unittest.TestCase):
    def setUp(self):
        self.server = StubServer()
        self.spool = agent.Spool(Path(tempfile.mkdtemp()), 1000, 3600)
        self.flusher = agent.Flusher(self.spool, self.server.url, 2)
        for i in range(3):
            self.spool.put({"event_id": f"e{i}"})
            time.sleep(0.002)

    def tearDown(self):
        self.server.close()

    def test_202_empties_the_spool(self):
        self.server.responses = [(202, {"accepted": 3, "duplicates": 0})]
        self.flusher.flush()
        self.assertEqual(len(self.spool), 0)
        self.assertEqual([e["event_id"] for e in self.server.requests[0]["events"]], ["e0", "e1", "e2"])

    def test_422_drops_only_the_rejected_events_then_sends_the_rest(self):
        self.server.responses = [(422, {"error": "validation_failed", "message": "x",
                                        "details": [{"index": 1, "path": "type", "message": "bad"}]}),
                                 (202, {"accepted": 2, "duplicates": 0})]
        self.flusher.flush()
        self.assertEqual(len(self.spool), 0)
        self.assertEqual([e["event_id"] for e in self.server.requests[1]["events"]], ["e0", "e2"])

    def test_5xx_keeps_everything(self):
        self.server.responses = [(503, {"error": "unavailable", "message": "db down"})]
        self.flusher.flush()
        self.assertEqual(len(self.spool), 3)
        self.assertFalse(self.flusher.reachable)

    def test_unreachable_server_keeps_everything(self):
        flusher = agent.Flusher(self.spool, "http://127.0.0.1:9", 1)   # nothing listens on port 9
        flusher.flush()
        self.assertEqual(len(self.spool), 3)
        self.assertFalse(flusher.reachable)


class ContractTest(unittest.TestCase):
    """Every kind of event the agent builds is valid according to the API spec."""

    def test_events_match_the_spec(self):
        try:
            import yaml
            from jsonschema import Draft202012Validator
            from referencing import Registry
            from referencing.jsonschema import DRAFT202012
        except ImportError:
            self.skipTest("jsonschema/pyyaml not installed")
        spec = yaml.safe_load(SPEC.read_text())
        registry = Registry().with_resource("urn:api", DRAFT202012.create_resource(spec))
        validator = Draft202012Validator({"$ref": "urn:api#/components/schemas/EventBatch"}, registry=registry,
                                         format_checker=Draft202012Validator.FORMAT_CHECKER)

        kube = FakeKube()
        a, spool, clock = make_agent(tempfile.mkdtemp(), kube)
        a.poll()
        kube.objects["/api/v1/namespaces/cnav/configmaps/localdev-profile"]["data"]["profile"] = "usage"
        kube.objects["/api/v1/namespaces/cnav/configmaps/localdev-profile"]["metadata"]["resourceVersion"] = "2"
        kube.objects["/api/v1/namespaces/kube-system/configmaps/localdev-host"]["data"].pop("os_variant")
        a.poll()
        events = spooled(spool)
        self.assertEqual(set(names(events)),
                         {"cluster.created", "app.deployed", "cluster.heartbeat", "profile.changed"})
        errors = [f"{list(e.absolute_path)}: {e.message}" for e in validator.iter_errors({"events": events})]
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
