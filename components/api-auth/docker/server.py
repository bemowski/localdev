"""api-auth: API-key check for http://api.localhost/<api>/ (Traefik forwardAuth), see ../readme.md.

  /check     forwardAuth target (Middleware api/key). The API is the first segment of
             X-Forwarded-Uri; its keys are the api-key* values of secret store entry api-<api>
             (Secret secret-store/api-<api>). 200 if the request carries one, else 401.
  /healthz   probes
  anything   else: 404 (the api.localhost catch-all route: no API at that path)

Keys arrive as `Authorization: Bearer <key>` or `X-API-Key: <key>`. Unknown API or missing entry:
401 (fail closed). Keys are never logged.

Environment: API_AUTH_PORT (8080), API_AUTH_CACHE_S (5), API_AUTH_LOG_LEVEL (INFO)
"""
import base64
import hmac
import json
import logging
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("API_AUTH_PORT", "8080"))
CACHE_S = float(os.environ.get("API_AUTH_CACHE_S", "5"))
STORE_NS = "secret-store"
API_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")   # a DNS label, like the entry names

log = logging.getLogger("api-auth")


class Kube:
    SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")

    def __init__(self):
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        self.base = f"https://{host}:{os.environ.get('KUBERNETES_SERVICE_PORT', '443')}"
        self.ctx = ssl.create_default_context(cafile=str(self.SA / "ca.crt"))

    def secret(self, ns, name):
        """{key: value} of a Secret, or None if it doesn't exist."""
        token = (self.SA / "token").read_text()  # re-read: projected tokens rotate
        req = urllib.request.Request(f"{self.base}/api/v1/namespaces/{ns}/secrets/{name}",
                                     headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=5) as r:
                data = json.load(r).get("data") or {}
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        return {k: base64.b64decode(v).decode() for k, v in data.items()}


class Keys:
    """API name -> its keys, from the secret store, cached for CACHE_S (so a secrets:sync applies fast)."""

    def __init__(self, fetch):
        self.fetch, self.cache = fetch, {}

    def get(self, api):
        hit = self.cache.get(api)
        if hit and time.monotonic() - hit[0] < CACHE_S:
            return hit[1]
        values = self.fetch(STORE_NS, f"api-{api}")
        keys = None if values is None else [v for k, v in values.items() if k.startswith("api-key") and v]
        self.cache[api] = (time.monotonic(), keys)
        return keys


def api_name(uri):
    """'/usage/v1/events?x=1' -> 'usage'; None if the first segment isn't a valid name."""
    first = (uri or "").split("?", 1)[0].lstrip("/").split("/", 1)[0]
    return first if API_NAME.match(first) else None


def presented_key(headers):
    auth = headers.get("Authorization") or ""
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return (headers.get("X-API-Key") or "").strip()


def check(keys, uri, headers):
    """(status, reason) for one forwardAuth request. The reason is for logs and the 401 body."""
    api = api_name(uri)
    if not api:
        return 401, "no API in the path"
    valid = keys.get(api)
    if valid is None:
        log.warning(f"api {api!r}: no secret store entry api-{api}; add [api-{api}] with an api-key "
                    f"to the component's secrets.defaults.toml (denying all requests)")
        return 401, f"API {api!r} has no keys configured"
    key = presented_key(headers)
    if not key:
        return 401, "missing API key (Authorization: Bearer <key> or X-API-Key: <key>)"
    # compare against every key, constant time
    if any([hmac.compare_digest(key.encode(), k.encode()) for k in valid]):
        return 200, "ok"
    return 401, "invalid API key"


class Handler(BaseHTTPRequestHandler):
    keys: Keys = None
    protocol_version = "HTTP/1.1"

    def reply(self, status, body, extra=None):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def handle_any(self):
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            return self.reply(200, {"status": "ok"})
        if path == "/check":
            uri = self.headers.get("X-Forwarded-Uri")
            try:
                status, reason = check(self.keys, uri, self.headers)
            except (OSError, ValueError) as e:   # kube API unreachable / unexpected answer
                log.error(f"can't read keys: {e}")
                status, reason = 503, "API key check unavailable"
            log.log(logging.DEBUG if status == 200 else logging.INFO,
                    f"{self.headers.get('X-Forwarded-Method', '?')} {uri} -> {status} {reason}")
            if status == 200:
                return self.reply(200, {"status": "ok"})
            return self.reply(status, {"error": "unauthorized" if status == 401 else "unavailable",
                                       "message": reason},
                              {"WWW-Authenticate": 'Bearer realm="api.localhost"'} if status == 401 else None)
        return self.reply(404, {"error": "not_found", "message": f"No API at {path}"})

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = handle_any

    def log_message(self, fmt, *args):   # requests are logged by handle_any (/check) or not at all
        pass


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("API_AUTH_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    Handler.keys = Keys(Kube().secret)
    log.info(f"listening on :{PORT}")
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
