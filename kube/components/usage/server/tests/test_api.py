"""The HTTP API end to end, against a real (throwaway) Postgres database."""
import uuid

from conftest import example, fresh


def with_cluster(batch: dict, cluster_id: str) -> dict:
    for ev in batch["events"]:
        ev["data"]["cluster_id"] = cluster_id
    return batch


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_serves_its_spec(client):
    r = client.get("/openapi.yaml")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/yaml")
    assert "title: Usage events API" in r.text


def test_batch_is_stored_and_projected(client, db):
    cluster_id = str(uuid.uuid4())
    batch = with_cluster(fresh(example("batch.json")), cluster_id)
    r = client.post("/v1/events", json=batch)
    assert r.status_code == 202
    assert r.json() == {"accepted": 5, "duplicates": 0}

    ids = [ev["event_id"] for ev in batch["events"]]
    assert db.execute("SELECT count(*) FROM events WHERE event_id = ANY(%s::uuid[])", (ids,)).fetchone()[0] == 5

    row = db.execute("SELECT cluster_type, cluster_role, username, platform, profile, components, os, os_variant,"
                     " kubernetes_version, created_at IS NOT NULL, destroyed_at IS NOT NULL, last_seen_at"
                     " FROM clusters WHERE cluster_id = %s", (cluster_id,)).fetchone()
    assert row[:11] == ("localdev", "dev", "alice", "k3d", "dev", ["postgres", "nats", "docs"], "windows", "wsl2",
                        "v1.36.5+k3s1", True, True)
    assert row[11].isoformat() == "2026-10-10T19:25:41+00:00"   # latest event: app.deployed (14:25:41-05:00)

    events = db.execute("SELECT name, outcome, duration_ms, attributes->>'suite' FROM cluster_events"
                        " WHERE cluster_id = %s ORDER BY occurred_at", (cluster_id,)).fetchall()
    assert events == [("tests.run", "success", 48210, "smoke"), ("app.deployed", None, None, None)]

    beat = db.execute("SELECT nodes, cores, memory_bytes, pods, namespaces FROM cluster_heartbeats"
                      " WHERE cluster_id = %s", (cluster_id,)).fetchone()
    assert beat == (1, 8, 16777216000, 42, 14)


def test_heartbeats_stay_out_of_cluster_events(client, db):
    cluster_id = str(uuid.uuid4())
    client.post("/v1/events", json=with_cluster(fresh(example("cluster-heartbeat.json")), cluster_id))
    assert db.execute("SELECT count(*) FROM cluster_events WHERE cluster_id = %s", (cluster_id,)).fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM cluster_heartbeats WHERE cluster_id = %s",
                      (cluster_id,)).fetchone()[0] == 1


def test_non_localdev_cluster(client, db):
    batch = fresh(example("cluster-created.json"))
    batch["events"] = batch["events"][1:]   # the durable/prod/eks one: no localdev fields
    cluster_id = batch["events"][0]["data"]["cluster_id"] = str(uuid.uuid4())
    assert client.post("/v1/events", json=batch).status_code == 202
    row = db.execute("SELECT cluster_type, cluster_role, platform, platform_version, profile, os"
                     " FROM clusters WHERE cluster_id = %s", (cluster_id,)).fetchone()
    assert row == ("durable", "prod", "eks", None, None, None)


def test_last_seen_only_moves_forward(client, db):
    cluster_id = str(uuid.uuid4())
    late = with_cluster(fresh(example("cluster-heartbeat.json")), cluster_id)
    early = with_cluster(fresh(example("cluster-heartbeat.json")), cluster_id)
    late["events"][0]["occurred_at"] = "2026-10-10T18:00:00Z"
    early["events"][0]["occurred_at"] = "2026-10-10T08:00:00Z"
    client.post("/v1/events", json=late)
    client.post("/v1/events", json=early)   # arrives later, happened earlier
    seen = db.execute("SELECT last_seen_at FROM clusters WHERE cluster_id = %s", (cluster_id,)).fetchone()[0]
    assert seen.isoformat() == "2026-10-10T18:00:00+00:00"


def test_resending_is_idempotent(client, db):
    batch = with_cluster(fresh(example("cluster-event.json")), str(uuid.uuid4()))
    assert client.post("/v1/events", json=batch).json() == {"accepted": 2, "duplicates": 0}
    assert client.post("/v1/events", json=batch).json() == {"accepted": 0, "duplicates": 2}
    ids = [ev["event_id"] for ev in batch["events"]]
    n = db.execute("SELECT count(*) FROM cluster_events WHERE event_id = ANY(%s::uuid[])", (ids,))
    assert n.fetchone()[0] == 2


def test_out_of_order_lifecycle(client, db):
    """destroyed and an ad-hoc event arrive before created (spool order isn't guaranteed)."""
    cluster_id = str(uuid.uuid4())
    created, event, destroyed = (with_cluster(fresh(example(f)), cluster_id) for f in
                                 ("cluster-created.json", "cluster-event.json", "cluster-destroyed.json"))
    created["events"] = created["events"][:1]

    assert client.post("/v1/events", json=destroyed).status_code == 202
    assert client.post("/v1/events", json=event).status_code == 202
    row = db.execute("SELECT created_at, cluster_type, destroyed_at IS NOT NULL FROM clusters"
                     " WHERE cluster_id = %s", (cluster_id,)).fetchone()
    assert row == (None, None, True)

    assert client.post("/v1/events", json=created).status_code == 202
    row = db.execute("SELECT cluster_type, profile, created_at IS NOT NULL, destroyed_at IS NOT NULL,"
                     " last_seen_at FROM clusters WHERE cluster_id = %s", (cluster_id,)).fetchone()
    assert row[:4] == ("localdev", "dev", True, True)
    assert row[4].isoformat() == "2026-10-10T19:25:41+00:00"   # created (earlier) didn't move it back


def test_invalid_batch_stores_nothing(client, db):
    batch = fresh(example("batch.json"))
    batch["events"][2]["data"]["outcome"] = "maybe"
    r = client.post("/v1/events", json=batch)
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "validation_failed"
    assert body["message"] == "1 event invalid"
    assert body["details"][0]["index"] == 2 and body["details"][0]["path"] == "data.outcome"
    ids = [ev["event_id"] for ev in batch["events"]]
    assert db.execute("SELECT count(*) FROM events WHERE event_id = ANY(%s::uuid[])", (ids,)).fetchone()[0] == 0


def test_unknown_type(client):
    r = client.post("/v1/events", json=example("invalid/unknown-type.json"))
    assert r.status_code == 422
    assert r.json()["details"] == [{"index": 0, "path": "type", "message": "unknown event type 'cluster.paused'"}]


def test_malformed_json(client):
    r = client.post("/v1/events", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 400 and r.json()["error"] == "malformed_request"


def test_too_large(client):
    r = client.post("/v1/events", content=b" " * ((1 << 20) + 1), headers={"content-type": "application/json"})
    assert r.status_code == 413 and r.json()["error"] == "too_large"


def test_timestamps_keep_their_offset(client, db):
    batch = fresh(example("cluster-event.json"))  # 2nd event: 14:25:41-05:00
    client.post("/v1/events", json=batch)
    ts = db.execute("SELECT occurred_at AT TIME ZONE 'UTC' FROM events WHERE event_id = %s",
                    (batch["events"][1]["event_id"],)).fetchone()[0]
    assert ts.isoformat() == "2026-10-10T19:25:41"
