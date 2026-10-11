"""Postgres: connection pool, migrations, and writing event batches.

Connection settings come from the standard libpq environment (PGHOST, PGPORT, PGDATABASE, PGUSER,
PGPASSWORD) unless a conninfo string is given.
"""
import asyncio
import logging
from pathlib import Path

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

log = logging.getLogger("usage.store")
MIGRATIONS = Path(__file__).parent / "migrations"


class Store:
    def __init__(self, conninfo: str = ""):
        self.pool = AsyncConnectionPool(conninfo, min_size=1, max_size=4, open=False, timeout=5)

    async def open(self, attempts: int = 30):
        """Open the pool and apply migrations, retrying while the database is unreachable."""
        await self.pool.open(wait=False)
        for attempt in range(1, attempts + 1):
            try:
                await self.migrate()
                return
            except Exception as e:  # noqa: BLE001 - any failure: retry, then give up loudly
                if attempt == attempts:
                    raise
                log.warning(f"database not ready (attempt {attempt}/{attempts}): {e}")
                await asyncio.sleep(2)

    async def close(self):
        await self.pool.close()

    async def migrate(self):
        """Apply migrations/NNN_*.sql not yet in schema_migrations, in order, in one transaction.
        An advisory lock serialises replicas starting together."""
        async with self.pool.connection() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('usage-migrations'))")
            await conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                               "version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
            done = {row[0] for row in await (await conn.execute("SELECT version FROM schema_migrations")).fetchall()}
            for f in sorted(MIGRATIONS.glob("*.sql")):
                if f.stem not in done:
                    await conn.execute(f.read_text())
                    await conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (f.stem,))
                    log.info(f"applied migration {f.stem}")

    async def ping(self) -> bool:
        try:
            async with self.pool.connection(timeout=2) as conn:
                await conn.execute("SELECT 1")
            return True
        except Exception:  # noqa: BLE001
            return False

    async def insert(self, events: list[dict]) -> tuple[list[dict], int]:
        """Store a validated batch in one transaction. Returns (new events, duplicate count).
        Duplicates (event_id already stored) are skipped entirely, so projections never apply twice."""
        new, duplicates = [], 0
        async with self.pool.connection() as conn:  # commits on success, rolls back on error
            for ev in events:
                cur = await conn.execute(
                    "INSERT INTO events (event_id, type, occurred_at, username, source_name, source_version, data)"
                    " VALUES (%s, %s, %s::timestamptz, %s, %s, %s, %s)"
                    " ON CONFLICT (event_id) DO NOTHING RETURNING event_id",
                    (ev["event_id"], ev["type"], ev["occurred_at"], ev["username"],
                     ev["source"]["name"], ev["source"]["version"], Jsonb(ev["data"])))
                if await cur.fetchone() is None:
                    duplicates += 1
                    continue
                handler = HANDLERS.get(ev["type"])
                if handler:
                    await handler(conn, ev)
                new.append(ev)
        return new, duplicates


# ---------- projections: event type -> typed tables ----------
# A type without a handler is still stored (in `events`). Every cluster handler first calls
# _touch_cluster: the cluster's row exists (whatever order events arrive in) and last_seen_at moves.

async def _touch_cluster(conn: AsyncConnection, ev: dict):
    await conn.execute(
        """INSERT INTO clusters (cluster_id, username, last_seen_at) VALUES (%s, %s, %s::timestamptz)
           ON CONFLICT (cluster_id) DO UPDATE SET
               last_seen_at = GREATEST(clusters.last_seen_at, EXCLUDED.last_seen_at)""",
        (ev["data"]["cluster_id"], ev["username"], ev["occurred_at"]))


async def _cluster_created(conn: AsyncConnection, ev: dict):
    d = ev["data"]
    await _touch_cluster(conn, ev)
    await conn.execute(
        """UPDATE clusters SET
               cluster_type = %s, cluster_role = %s, username = %s, created_at = %s::timestamptz,
               platform = %s, platform_version = %s, kubernetes_version = %s, arch = %s,
               profile = %s, components = %s, os = %s, os_variant = %s, os_version = %s,
               created_event_id = %s
           WHERE cluster_id = %s""",
        (d["cluster_type"], d["cluster_role"], ev["username"], ev["occurred_at"],
         d["platform"], d.get("platform_version"), d["kubernetes_version"], d.get("arch"),
         d.get("profile"), d.get("components"), d.get("os"), d.get("os_variant"), d.get("os_version"),
         ev["event_id"], d["cluster_id"]))


# cluster.updated may set these; the column names come from this list, never from the request
UPDATABLE = ("cluster_type", "cluster_role", "platform", "platform_version", "kubernetes_version", "arch",
             "os", "os_variant", "os_version")


async def _cluster_updated(conn: AsyncConnection, ev: dict):
    """Patch the cluster: the facts sent replace the stored ones, the rest stay."""
    d = ev["data"]
    await _touch_cluster(conn, ev)
    cols = [c for c in UPDATABLE if c in d]
    # username "unknown" (the sender doesn't know it) never replaces a known one
    assignments = ", ".join([f"{c} = %s" for c in cols]
                            + ["username = CASE WHEN %s = 'unknown' THEN username ELSE %s END"])
    await conn.execute(f"UPDATE clusters SET {assignments} WHERE cluster_id = %s",
                       [d[c] for c in cols] + [ev["username"], ev["username"], d["cluster_id"]])


async def _cluster_destroyed(conn: AsyncConnection, ev: dict):
    await _touch_cluster(conn, ev)
    await conn.execute(
        "UPDATE clusters SET destroyed_at = %s::timestamptz, destroyed_event_id = %s WHERE cluster_id = %s",
        (ev["occurred_at"], ev["event_id"], ev["data"]["cluster_id"]))


async def _cluster_event(conn: AsyncConnection, ev: dict):
    d = ev["data"]
    await _touch_cluster(conn, ev)
    await conn.execute(
        """INSERT INTO cluster_events
               (event_id, cluster_id, occurred_at, username, name, outcome, duration_ms, attributes)
           VALUES (%s, %s, %s::timestamptz, %s, %s, %s, %s, %s)""",
        (ev["event_id"], d["cluster_id"], ev["occurred_at"], ev["username"], d["name"], d.get("outcome"),
         d.get("duration_ms"), Jsonb(d.get("attributes", {}))))


async def _cluster_heartbeat(conn: AsyncConnection, ev: dict):
    d = ev["data"]
    await _touch_cluster(conn, ev)
    await conn.execute(
        """INSERT INTO cluster_heartbeats
               (event_id, cluster_id, occurred_at, nodes, cores, memory_bytes, pods, namespaces)
           VALUES (%s, %s, %s::timestamptz, %s, %s, %s, %s, %s)""",
        (ev["event_id"], d["cluster_id"], ev["occurred_at"], d["nodes"], d["cores"], d["memory_bytes"],
         d["pods"], d["namespaces"]))


HANDLERS = {
    "cluster.created": _cluster_created,
    "cluster.updated": _cluster_updated,
    "cluster.destroyed": _cluster_destroyed,
    "cluster.event": _cluster_event,
    "cluster.heartbeat": _cluster_heartbeat,
}
