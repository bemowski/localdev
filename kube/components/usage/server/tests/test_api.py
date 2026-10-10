"""The HTTP API end to end, against a real (throwaway) Postgres database."""
import uuid

from conftest import example, fresh


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_serves_its_spec(client):
    r = client.get("/openapi.yaml")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/yaml")
    assert "title: Usage events API" in r.text


def test_batch_is_stored_and_projected(client, db):
    batch = fresh(example("batch.json"))
    r = client.post("/v1/events", json=batch)
    assert r.status_code == 202
    assert r.json() == {"accepted": 4, "duplicates": 0}

    ids = [ev["event_id"] for ev in batch["events"]]
    assert db.execute("SELECT count(*) FROM events WHERE event_id = ANY(%s::uuid[])", (ids,)).fetchone()[0] == 4

    cluster_id = batch["events"][0]["data"]["cluster_id"]
    row = db.execute("SELECT username, profile, components, os, os_variant, kubernetes_version,"
                     " created_at IS NOT NULL, destroyed_at IS NOT NULL"
                     " FROM localdev_clusters WHERE cluster_id = %s", (cluster_id,)).fetchone()
    assert row == ("alice", "dev", ["postgres", "nats", "docs"], "windows", "wsl2", "v1.33.4+k3s1", True, True)

    names = db.execute("SELECT name, outcome, duration_ms, attributes->>'suite' FROM localdev_cluster_events"
                       " WHERE event_id = ANY(%s::uuid[]) ORDER BY occurred_at", (ids,)).fetchall()
    assert names == [("tests.run", "success", 48210, "smoke"), ("app.deployed", None, None, None)]


def test_resending_is_idempotent(client, db):
    batch = fresh(example("cluster-event.json"))
    assert client.post("/v1/events", json=batch).json() == {"accepted": 2, "duplicates": 0}
    assert client.post("/v1/events", json=batch).json() == {"accepted": 0, "duplicates": 2}
    ids = [ev["event_id"] for ev in batch["events"]]
    n = db.execute("SELECT count(*) FROM localdev_cluster_events WHERE event_id = ANY(%s::uuid[])", (ids,))
    assert n.fetchone()[0] == 2


def test_out_of_order_lifecycle(client, db):
    """destroyed and an ad-hoc event arrive before created (spool order isn't guaranteed)."""
    cluster_id = str(uuid.uuid4())
    created, event, destroyed = (fresh(example(f)) for f in
                                 ("cluster-created.json", "cluster-event.json", "cluster-destroyed.json"))
    for b in (created, event, destroyed):
        for ev in b["events"]:
            ev["data"]["cluster_id"] = cluster_id

    assert client.post("/v1/events", json=destroyed).status_code == 202
    assert client.post("/v1/events", json=event).status_code == 202
    row = db.execute("SELECT created_at, destroyed_at IS NOT NULL FROM localdev_clusters WHERE cluster_id = %s",
                     (cluster_id,)).fetchone()
    assert row == (None, True)

    assert client.post("/v1/events", json=created).status_code == 202
    row = db.execute("SELECT profile, created_at IS NOT NULL, destroyed_at IS NOT NULL FROM localdev_clusters"
                     " WHERE cluster_id = %s", (cluster_id,)).fetchone()
    assert row == ("dev", True, True)


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
    assert r.json()["details"] == [{"index": 0, "path": "type",
                                    "message": "unknown event type 'localdev.cluster.paused'"}]


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
