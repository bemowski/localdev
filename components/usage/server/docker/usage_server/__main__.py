"""python -m usage_server: API on USAGE_PORT (8080), Prometheus metrics on USAGE_METRICS_PORT (9090)."""
import os

import uvicorn
from prometheus_client import start_http_server

from .app import create_app
from .logs import setup_logging

setup_logging()
start_http_server(int(os.environ.get("USAGE_METRICS_PORT", "9090")))
# log_config=None: uvicorn's loggers propagate to our logfmt root handler; no per-request access log
uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("USAGE_PORT", "8080")),
            log_config=None, access_log=False)
