"""Fixtures. API tests need a Postgres where they may create and drop a throwaway database:
USAGE_TEST_ADMIN_URL (default: localdev's postgresql://app:localdev@localhost:5432/app). They are
skipped when it is unreachable."""
import copy
import json
import os
import uuid
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from usage_server.app import create_app
from usage_server.store import Store

ROOT = Path(__file__).resolve().parents[1]  # usage-server/: openapi.yaml, examples/
SPEC = ROOT / "openapi.yaml"
EXAMPLES = ROOT / "examples"
ADMIN_URL = os.environ.get("USAGE_TEST_ADMIN_URL", "postgresql://app:localdev@localhost:5432/app")


def example(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text())


def fresh(batch: dict) -> dict:
    """A copy of a batch with new event ids (so it isn't a duplicate of an earlier test's)."""
    batch = copy.deepcopy(batch)
    for ev in batch["events"]:
        ev["event_id"] = str(uuid.uuid4())
    return batch


@pytest.fixture(scope="session")
def database():
    """conninfo of a new, empty database; dropped afterwards."""
    try:
        admin = psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as e:
        pytest.skip(f"no test Postgres at {ADMIN_URL}: {e}")
    name = f"usage_test_{uuid.uuid4().hex[:8]}"
    admin.execute(f'CREATE DATABASE "{name}"')
    conninfo = psycopg.conninfo.make_conninfo(ADMIN_URL, dbname=name)
    yield conninfo
    admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
    admin.close()


@pytest.fixture(scope="session")
def client(database):
    with TestClient(create_app(Store(database), SPEC)) as c:  # runs lifespan: migrations
        yield c


@pytest.fixture
def db(database):
    with psycopg.connect(database, autocommit=True) as conn:
        yield conn
