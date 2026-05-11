"""Static dashboard server with a one-tap delete endpoint.

Replaces the bare `python3 -m http.server` invocation. Same static
file serving, plus a single authenticated route:

    DELETE /<DASHBOARD_TOKEN>/q-<id>     → drop one Q&A row

Pure-stdlib so the container can be a thin alpine/slim image. The
60s scheduler in bot regenerates HTML, but the JS in the dashboard
also yanks the card client-side immediately so deletion feels
instant. Token comes via env (loaded by docker compose env_file).

Optional HTTP Basic Auth via DASHBOARD_USER / DASHBOARD_PASSWORD env.
When both are set, every request — static files and DELETE — must
carry a matching Authorization: Basic header before reaching the
token-path stage.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import sqlite3
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dashboard.server")

_TOKEN = os.getenv("DASHBOARD_TOKEN", "").strip()
_PORT = int(os.getenv("DASHBOARD_PORT", "8082"))
_DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data")).resolve()
_DOC_ROOT = _DATA_DIR / "dashboard"
_QNA_DB = _DATA_DIR / "qna.db"

_BASIC_USER = os.getenv("DASHBOARD_USER", "").strip()
_BASIC_PASS = os.getenv("DASHBOARD_PASSWORD", "").strip()
_BASIC_ENABLED = bool(_BASIC_USER and _BASIC_PASS)
_EXPECTED_AUTH = (
    "Basic " + base64.b64encode(f"{_BASIC_USER}:{_BASIC_PASS}".encode()).decode()
    if _BASIC_ENABLED else None
)

_DELETE_RE = re.compile(r"^/([A-Za-z0-9_\-]+)/q-(\d+)/?$")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(_DOC_ROOT), **kwargs)

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)

    def _check_basic_auth(self) -> bool:
        """Returns True if request passes Basic Auth (or auth disabled).
        Sends 401 response and returns False otherwise."""
        if not _BASIC_ENABLED:
            return True
        provided = self.headers.get("Authorization", "")
        if provided == _EXPECTED_AUTH:
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Second Brain"')
        self.send_header("content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"auth required")
        return False

    def do_GET(self):
        if not self._check_basic_auth():
            return
        super().do_GET()

    def do_HEAD(self):
        if not self._check_basic_auth():
            return
        super().do_HEAD()

    def do_DELETE(self):
        if not self._check_basic_auth():
            return
        m = _DELETE_RE.match(self.path)
        if not m:
            self.send_error(404)
            return
        token, qid = m.group(1), int(m.group(2))
        if not _TOKEN or token != _TOKEN:
            self.send_error(403, "forbidden")
            return
        try:
            with sqlite3.connect(str(_QNA_DB)) as c:
                cur = c.execute("DELETE FROM qna WHERE id = ?", (qid,))
                n = cur.rowcount
            log.info("deleted qna #%d (rows=%d)", qid, n)
        except Exception as e:
            log.exception("delete failed")
            self.send_error(500, f"delete failed: {e}")
            return
        body = json.dumps({"ok": True, "deleted": n}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    if not _TOKEN:
        sys.exit("DASHBOARD_TOKEN must be set in .env")
    if not _DOC_ROOT.exists():
        _DOC_ROOT.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer(("0.0.0.0", _PORT), Handler)
    log.info("dashboard server on :%d, root=%s, basic_auth=%s",
             _PORT, _DOC_ROOT, "on" if _BASIC_ENABLED else "off")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")


if __name__ == "__main__":
    main()
