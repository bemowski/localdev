"""The validator, built from openapi.yaml, against the shared examples. No database."""
import pytest

from conftest import EXAMPLES, SPEC, example
from usage_server.validation import BatchValidator

validator = BatchValidator.from_file(SPEC)

# invalid example -> the (index, path) the error must point at
EXPECTED = {
    "bad-cluster-role.json": (0, "data.cluster_role"),
    "bad-event-id.json": (0, "event_id"),
    "bad-event-name.json": (0, "data.name"),
    "bad-os.json": (0, "data.os"),
    "bad-timestamp.json": (0, "occurred_at"),
    "empty-batch.json": (None, "events"),
    "heartbeat-missing-stat.json": (0, "data"),
    "missing-required.json": (0, "data"),
    "nested-attribute.json": (0, "data.attributes.nested"),
    "unknown-envelope-field.json": (0, ""),
    "unknown-type.json": (0, "type"),
    "updated-nothing.json": (0, "data"),
}


@pytest.mark.parametrize("name", sorted(p.name for p in EXAMPLES.glob("*.json")))
def test_valid_examples(name):
    assert validator.errors(example(name)) == []


def test_every_invalid_example_has_an_expectation():
    assert sorted(p.name for p in (EXAMPLES / "invalid").glob("*.json")) == sorted(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_invalid_examples_point_at_the_problem(name):
    index, path = EXPECTED[name]
    details = validator.errors(example(f"invalid/{name}"))
    assert len(details) == 1, details
    assert details[0].get("index") == index
    assert details[0]["path"] == path


def test_errors_carry_each_bad_events_index():
    batch = example("batch.json")  # created, heartbeat, tests.run, app.deployed, updated, destroyed
    batch["events"][2]["data"]["outcome"] = "maybe"
    batch["events"][4]["username"] = ""
    details = validator.errors(batch)
    assert [(d["index"], d["path"]) for d in details] == [(2, "data.outcome"), (4, "username")]


def test_missing_type():
    batch = example("cluster-destroyed.json")
    del batch["events"][0]["type"]
    [detail] = validator.errors(batch)
    assert detail["index"] == 0 and "type" in detail["message"]


def test_batch_limits():
    one = example("cluster-destroyed.json")["events"][0]
    assert validator.errors({"events": [one] * 500}) == []
    assert validator.errors({"events": [one] * 501})[0]["path"] == "events"
    assert validator.errors({"events": [one], "extra": 1})[0]["path"] == ""
    assert validator.errors([one])[0]["path"] == ""
