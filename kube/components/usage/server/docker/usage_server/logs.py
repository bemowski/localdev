"""logfmt logging: one line per record (ts=... level=info logger=usage msg="..."), so Loki's
detected_level and Grafana's level filter work. Tracebacks go into the err= field."""
import json
import logging
import os
import sys
from datetime import datetime, timezone


class LogfmtFormatter(logging.Formatter):
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
    level = os.environ.get("USAGE_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=getattr(logging, level, logging.INFO), handlers=[handler], force=True)
