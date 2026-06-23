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
import hmac
import json
import logging
import os
import re
import sqlite3
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dashboard.server")

_TOKEN = os.getenv("DASHBOARD_TOKEN", "").strip()
_PORT = int(os.getenv("DASHBOARD_PORT", "8082"))
_DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data")).resolve()
_DOC_ROOT = _DATA_DIR / "dashboard"
_QNA_DB = _DATA_DIR / "qna.db"
_NOTES_DB = _DATA_DIR / "notes.db"

_BASIC_USER = os.getenv("DASHBOARD_USER", "").strip()
_BASIC_PASS = os.getenv("DASHBOARD_PASSWORD", "").strip()
_BASIC_ENABLED = bool(_BASIC_USER and _BASIC_PASS)
_EXPECTED_AUTH = (
    "Basic " + base64.b64encode(f"{_BASIC_USER}:{_BASIC_PASS}".encode()).decode()
    if _BASIC_ENABLED else None
)

def _eq(a: str, b: str) -> bool:
    """Timing-safe string compare for auth values."""
    try:
        return hmac.compare_digest(a, b)
    except TypeError:
        return a == b


_DELETE_RE = re.compile(r"^/([A-Za-z0-9_\-]+)/q-(\d+)/?$")
# Study-note delete: id is a (url-encoded) slug, so capture the rest.
_NOTE_DELETE_RE = re.compile(r"^/([A-Za-z0-9_\-]+)/notes/(.+?)/?$")
# Study-note 종류별 manual override: POST /<token>/notes/<id>/category
_NOTE_CAT_RE = re.compile(r"^/([A-Za-z0-9_\-]+)/notes/(.+)/category/?$")
_NOTE_CATS = ("주식", "공부", "그외")
_ASK_RE = re.compile(r"^/([A-Za-z0-9_\-]+)/ask/?$")
_ASK_GET_RE = re.compile(r"^/([A-Za-z0-9_\-]+)/ask/(\d+)/?$")

# Each natural-language ask is a real Gemini spend, so reject floods
# before they ever reach the bot. Single-owner dashboard → generous.
_ASK_FLOOD_WINDOW_SEC = 60
_ASK_FLOOD_MAX = 20
_ASK_MAX_LEN = 4000


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
        if _EXPECTED_AUTH is not None and _eq(provided, _EXPECTED_AUTH):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Second Brain"')
        self.send_header("content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"auth required")
        return False

    def _send_json(self, obj, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._check_basic_auth():
            return
        m = _ASK_GET_RE.match(self.path)
        if m:
            self._handle_ask_get(m.group(1), int(m.group(2)))
            return
        super().do_GET()

    def do_HEAD(self):
        if not self._check_basic_auth():
            return
        super().do_HEAD()

    def do_POST(self):
        if not self._check_basic_auth():
            return
        mc = _NOTE_CAT_RE.match(self.path)
        if mc:
            self._set_note_category(mc.group(1), unquote(mc.group(2)))
            return
        m = _ASK_RE.match(self.path)
        if m:
            self._handle_ask_post(m.group(1))
            return
        self.send_error(404)

    def _set_note_category(self, token: str, nid: str):
        """Manual 종류별 override from the dashboard badge. Sets category +
        category_locked=1 so the bot's version-gated auto-reclassify won't
        overwrite the user's choice. Runs in the 200MB container → sqlite
        only (mirrors _delete_note)."""
        if not _TOKEN or not _eq(token, _TOKEN):
            self.send_error(403, "forbidden")
            return
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 1000:
            self._send_json({"error": "빈 요청"}, 400)
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            cat = (data.get("cat") or "").strip()
        except Exception:
            self._send_json({"error": "잘못된 요청"}, 400)
            return
        if cat not in _NOTE_CATS:
            self._send_json({"error": "허용되지 않은 종류"}, 400)
            return
        try:
            with sqlite3.connect(str(_NOTES_DB), timeout=30) as c:
                # Defensive: the column normally exists (bot init_db adds
                # it), but ensure it so a fresh DB can't 500 the click.
                cols = {r[1] for r in c.execute("PRAGMA table_info(notes)")}
                if "category_locked" not in cols:
                    c.execute("ALTER TABLE notes ADD COLUMN "
                              "category_locked INTEGER DEFAULT 0")
                n = c.execute(
                    "UPDATE notes SET category=?, category_locked=1 "
                    "WHERE id=?", (cat, nid)).rowcount
            if not n:
                self.send_error(404)
                return
            log.info("note %s category → %s (locked)", nid, cat)
        except Exception as e:
            log.exception("note category set failed")
            self.send_error(500, f"set failed: {e}")
            return
        body = json.dumps({"ok": True, "category": cat}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_ask_post(self, token: str):
        """Park a dashboard search-box query for the bot to run. Returns
        {id} for the browser to poll. Token-gated like the delete route;
        flood-capped because each Q&A costs real money."""
        if not _TOKEN or not _eq(token, _TOKEN):
            self.send_error(403, "forbidden")
            return
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 100_000:
            self._send_json({"error": "빈 요청"}, 400)
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            q = (data.get("q") or "").strip()
        except Exception:
            self._send_json({"error": "잘못된 요청"}, 400)
            return
        if not q:
            self._send_json({"error": "빈 질문"}, 400)
            return
        if len(q) > _ASK_MAX_LEN:
            self._send_json({"error": f"질문이 너무 깁니다 (최대 {_ASK_MAX_LEN}자)"}, 400)
            return
        try:
            from ..store import dash_queries
            if dash_queries.recent_count(_ASK_FLOOD_WINDOW_SEC) >= _ASK_FLOOD_MAX:
                self._send_json(
                    {"error": "요청이 너무 많아요. 잠시 후 다시 시도해주세요."}, 429)
                return
            qid = dash_queries.enqueue(q)
        except Exception as e:
            log.exception("ask enqueue failed")
            self._send_json({"error": f"큐 오류: {e}"}, 500)
            return
        self._send_json({"id": qid})

    def _handle_ask_get(self, token: str, qid: int):
        """Poll one parked query's status/answer."""
        if not _TOKEN or not _eq(token, _TOKEN):
            self.send_error(403, "forbidden")
            return
        try:
            from ..store import dash_queries
            row = dash_queries.get(qid)
        except Exception as e:
            log.exception("ask get failed")
            self._send_json({"status": "error", "error": str(e)})
            return
        if not row:
            self._send_json({"status": "error", "error": "찾을 수 없는 요청"})
            return
        self._send_json({
            "status": row["status"],
            "kind": row["kind"],
            "answer": row["answer"],
            "sources": row["sources"],
            "error": row["error"],
        })

    def _send_ok(self, n: int):
        body = json.dumps({"ok": True, "deleted": n}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _delete_note(self, token: str, nid: str):
        """Delete one study note: rows (notes/srs/questions/reviews —
        FK cascade is off in SQLite so do it explicitly) + vault .md +
        dashboard page. Runs in the 200MB container → sqlite only."""
        if not _TOKEN or not _eq(token, _TOKEN):
            self.send_error(403, "forbidden")
            return
        if not nid or nid == "index.html":
            self.send_error(404)
            return
        try:
            with sqlite3.connect(str(_NOTES_DB), timeout=30) as c:
                row = c.execute(
                    "SELECT md_path FROM notes WHERE id=?", (nid,)).fetchone()
                md_path = row[0] if row else None
                c.execute("DELETE FROM questions WHERE note_id=?", (nid,))
                c.execute("DELETE FROM reviews WHERE note_id=?", (nid,))
                c.execute("DELETE FROM note_srs WHERE note_id=?", (nid,))
                n = c.execute("DELETE FROM notes WHERE id=?", (nid,)).rowcount
            log.info("deleted note %s (rows=%d)", nid, n)
            for p in (md_path and Path(md_path),
                      Path(_DOC_ROOT) / token / "notes" / f"note-{nid}.html"):
                if p:
                    try:
                        p.unlink(missing_ok=True)
                    except OSError:
                        log.warning("note unlink failed: %s", p)
        except Exception as e:
            log.exception("note delete failed")
            self.send_error(500, f"delete failed: {e}")
            return
        self._send_ok(n)

    def do_DELETE(self):
        if not self._check_basic_auth():
            return
        m = _DELETE_RE.match(self.path)
        if not m:
            mn = _NOTE_DELETE_RE.match(self.path)
            if mn:
                self._delete_note(mn.group(1), unquote(mn.group(2)))
                return
            self.send_error(404)
            return
        token, qid = m.group(1), int(m.group(2))
        if not _TOKEN or not _eq(token, _TOKEN):
            self.send_error(403, "forbidden")
            return
        try:
            with sqlite3.connect(str(_QNA_DB), timeout=30) as c:
                cur = c.execute("DELETE FROM qna WHERE id = ?", (qid,))
                n = cur.rowcount
            log.info("deleted qna #%d (rows=%d)", qid, n)
            # Drop the now-orphaned detail page; the dashboard JS already
            # removed the card client-side and the bot's 60s tick rebuilds
            # index.html. The previous full regenerate() here pulled the
            # chroma/vector import chain into this 200MB-capped container
            # (OOM risk) and hung the request thread for the whole render.
            try:
                (Path(_DOC_ROOT) / token / f"q-{qid}.html").unlink(missing_ok=True)
            except OSError:
                log.warning("detail page unlink failed for q-%d", qid)
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
    # Until the bot's first regenerate writes the public index, a bare
    # directory would be auto-listed by SimpleHTTPRequestHandler and
    # reveal the <token>/ directory name. Park a placeholder.
    pub = _DOC_ROOT / "index.html"
    if not pub.exists():
        try:
            pub.write_text("<!doctype html><title>second brain</title>",
                           encoding="utf-8")
        except OSError:
            pass
    httpd = ThreadingHTTPServer(("0.0.0.0", _PORT), Handler)
    log.info("dashboard server on :%d, root=%s, basic_auth=%s",
             _PORT, _DOC_ROOT, "on" if _BASIC_ENABLED else "off")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")


if __name__ == "__main__":
    main()
