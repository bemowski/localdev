"""HTTP API: implements openapi.yaml (POST /v1/events, GET /ping, GET /healthz, GET /openapi.yaml).

Environment:
  PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD   database (libpq)
  USAGE_SPEC          path to openapi.yaml (default /app/openapi.yaml, the image's copy)
"""
import json
import logging
import os
from collections import Counter as Tally
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import Counter
from psycopg import OperationalError
from psycopg_pool import PoolTimeout

from .store import Store
from .validation import BatchValidator

log = logging.getLogger("usage")
MAX_BODY = 1 << 20  # 1 MiB, per the spec
DEFAULT_SPEC = "/app/openapi.yaml"

EVENTS = Counter("usage_events", "Events received, by type and result", ["type", "result"])
BATCHES = Counter("usage_batches", "Batches received, by response status", ["status"])


def error(status: int, code: str, message: str, details: list | None = None) -> JSONResponse:
    BATCHES.labels(str(status)).inc()
    body = {"error": code, "message": message}
    if details:
        body["details"] = details
    return JSONResponse(body, status_code=status)


def create_app(store: Store | None = None, spec_path: Path | None = None) -> FastAPI:
    spec_path = Path(spec_path or os.environ.get("USAGE_SPEC") or DEFAULT_SPEC)
    validator = BatchValidator.from_file(spec_path)
    store = store or Store()

    @asynccontextmanager
    async def lifespan(_app):
        await store.open()
        log.info(f"ready (spec {validator.spec['info']['version']})")
        yield
        await store.close()

    # FastAPI's generated docs are off: openapi.yaml is the contract, served as-is
    app = FastAPI(lifespan=lifespan, openapi_url=None, docs_url=None, redoc_url=None)

    @app.post("/v1/events")
    async def submit_events(request: Request):
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            return error(413, "too_large", f"Request body exceeds {MAX_BODY} bytes")
        raw = bytearray()
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > MAX_BODY:
                return error(413, "too_large", f"Request body exceeds {MAX_BODY} bytes")
        try:
            body = json.loads(raw)
        except ValueError:  # includes UnicodeDecodeError
            return error(400, "malformed_request", "Body is not valid JSON")

        details = validator.errors(body)
        if details:
            bad = len({d["index"] for d in details if "index" in d})
            message = f"{bad} event{'s' if bad != 1 else ''} invalid" if bad else "Invalid batch"
            log.info(f"rejected batch: {message}; first: {details[0]}")
            return error(422, "validation_failed", message, details)

        events = body["events"]
        try:
            new, duplicates = await store.insert(events)
        except (OperationalError, PoolTimeout) as e:
            log.error(f"database unavailable: {e}")
            return error(503, "unavailable", "Database unavailable, retry later")

        new_ids = {ev["event_id"] for ev in new}
        for ev in events:
            EVENTS.labels(ev["type"], "accepted" if ev["event_id"] in new_ids else "duplicate").inc()
        BATCHES.labels("202").inc()
        types = ", ".join(f"{t}={n}" for t, n in sorted(Tally(ev["type"] for ev in new).items()))
        log.info(f"stored {len(new)} event(s), {duplicates} duplicate(s)" + (f": {types}" if types else ""))
        return JSONResponse({"accepted": len(new), "duplicates": duplicates}, status_code=202)

    @app.get("/ping")
    async def ping():
        return {"ping": "pong"}

    @app.get("/healthz")
    async def health():
        ok = await store.ping()
        return JSONResponse({"status": "ok" if ok else "unavailable"}, status_code=200 if ok else 503)

    @app.get("/openapi.yaml")
    async def spec():
        return Response(validator.spec_text, media_type="application/yaml")

    return app
