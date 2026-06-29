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
# Q&A 중요 표시 토글: POST /<token>/q-<id>/important {important:bool}
_QNA_IMPORTANT_RE = re.compile(r"^/([A-Za-z0-9_\-]+)/q-(\d+)/important/?$")
# 공용 중요 표시(위키 페이지·KG 개체): POST /<token>/mark {kind,id,important}
_MARK_RE = re.compile(r"^/([A-Za-z0-9_\-]+)/mark/?$")
_MARK_KINDS = ("wiki", "kg_edge")
# 개인 메모(노트·Q&A·위키·KG 관계): POST /<token>/memo {kind,id,text}
_MEMO_RE = re.compile(r"^/([A-Za-z0-9_\-]+)/memo/?$")
_MEMO_KINDS = ("note", "qna", "wiki", "kg_edge")
# 알람(매일 HH:MM KST 텔레그램): POST /<token>/alarm {kind,id,hhmm}
_ALARM_RE = re.compile(r"^/([A-Za-z0-9_\-]+)/alarm/?$")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Study-note delete: id is a (url-encoded) slug, so capture the rest.
_NOTE_DELETE_RE = re.compile(r"^/([A-Za-z0-9_\-]+)/notes/(.+?)/?$")
# Study-note 종류별 manual override: POST /<token>/notes/<id>/category
_NOTE_CAT_RE = re.compile(r"^/([A-Za-z0-9_\-]+)/notes/(.+)/category/?$")
_NOTE_CATS = ("주식", "공부", "그외")
# Study-note 중요 표시 토글: POST /<token>/notes/<id>/important {important:bool}
_NOTE_IMPORTANT_RE = re.compile(r"^/([A-Za-z0-9_\-]+)/notes/(.+)/important/?$")
_ASK_RE = re.compile(r"^/([A-Za-z0-9_\-]+)/ask/?$")
_ASK_GET_RE = re.compile(r"^/([A-Za-z0-9_\-]+)/ask/(\d+)/?$")
# GET /<token>/kg/entity?e=<name> → all edges for an entity (full degree set,
# not just the top-3000-by-confidence the static KG page loads).
_KG_ENTITY_RE = re.compile(r"^/([A-Za-z0-9_\-]+)/kg/entity/?$")
# Delete one KG relation (dashboard 🗑): POST /<token>/kg/<edge_id>/delete
_KG_DELETE_RE = re.compile(r"^/([A-Za-z0-9_\-]+)/kg/(\d+)/delete/?$")
# Browser-extension one-click ingest: POST /<token>/ingest {url,target}
# (target = rag|note). Status poll: GET /<token>/ingest/<id>.
_INGEST_RE = re.compile(r"^/([A-Za-z0-9_\-]+)/ingest/?$")
_INGEST_STATUS_RE = re.compile(r"^/([A-Za-z0-9_\-]+)/ingest/(\d+)/?$")

# Each natural-language ask is a real Gemini spend, so reject floods
# before they ever reach the bot. Single-owner dashboard → generous.
_ASK_FLOOD_WINDOW_SEC = 60
_ASK_FLOOD_MAX = 20
_ASK_MAX_LEN = 4000

# Extension ingest is cheaper per call than a paid ask but still costs
# (synth/embed). Generous single-owner cap so a digest paste doesn't 429.
_INGEST_FLOOD_WINDOW_SEC = 300
_INGEST_FLOOD_MAX = 60
_INGEST_MAX_URL = 2000


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

    def end_headers(self):
        # Static dashboard pages are regenerated in place every ~15s. Without
        # forcing revalidation, the browser serves a cached copy so a
        # just-deleted note/card looks like it "came back" until a hard
        # refresh (Ctrl+Shift+R). no-cache still allows a cheap 304 when the
        # file is unchanged — it only forces the browser to check first.
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        super().end_headers()

    def _send_json(self, obj, code: int = 200, cors: bool = False):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        # CORS preflight (browsers send no credentials here, so it must
        # bypass basic auth). The actual ingest request is token-gated.
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "content-type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("content-length", "0")
        self.end_headers()

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        # Extension ingest-status poll is token-gated (no basic auth), so
        # the extension only needs the token — handle before the gate.
        mis = _INGEST_STATUS_RE.match(parsed.path)
        if mis:
            self._handle_ingest_status(mis.group(1), int(mis.group(2)))
            return
        if not self._check_basic_auth():
            return
        mke = _KG_ENTITY_RE.match(parsed.path)
        if mke:
            ename = (parse_qs(parsed.query).get("e") or [""])[0]
            self._handle_kg_entity(mke.group(1), ename)
            return
        m = _ASK_GET_RE.match(self.path)
        if m:
            self._handle_ask_get(m.group(1), int(m.group(2)))
            return
        super().do_GET()

    def _handle_kg_entity(self, token: str, ename: str):
        """Return every edge touching `ename` (full graph), enriched with
        ★/memo/alarm state + source doc title/url, so the KG page can show
        all of a chip's relations on click."""
        if not _TOKEN or not _eq(token, _TOKEN):
            self.send_error(403, "forbidden")
            return
        ename = (ename or "").strip()
        if not ename:
            self._send_json({"edges": [], "count": 0})
            return
        try:
            from ..store import kg as _kg, meta as _meta, marks as _marks
            edges = _kg.edges_for_entity(ename, 1200)
            doc_ids = list({e.get("doc_id") for e in edges if e.get("doc_id")})
            docs = _meta.get_docs_batch(doc_ids) if doc_ids else {}
            imp = _marks.marked("kg_edge")
            memos = _marks.memos("kg_edge")
            alarms = _marks.alarm_map("kg_edge")
            out = []
            for e in edges:
                eid = str(e.get("id") or "")
                d = docs.get(e.get("doc_id") or "") or {}
                al = alarms.get(eid, {})
                out.append({
                    "id": eid, "src": e["src"], "rel": e["rel"], "dst": e["dst"],
                    "c": round(e.get("confidence") or 0, 2),
                    "important": 1 if eid in imp else 0,
                    "memo": (memos.get(eid) or "").strip(),
                    "src_title": (d.get("title") or "").strip(),
                    "src_url": (d.get("source") or "").strip(),
                    "ahhmm": al.get("hhmm", "") or "",
                    "adate": al.get("date", "") or "",
                    "ts": e.get("ts") or "",  # 학습된 날짜(추출 시각)
                })
            self._send_json({"edges": out, "count": len(out)})
        except Exception as e:
            log.exception("kg entity lookup failed")
            self.send_error(500, f"lookup failed: {e}")

    def _handle_ingest_post(self, token: str):
        """Park a one-click ingest from the browser extension. Token-gated
        (the path token is the secret) + CORS-enabled so an HTTPS YouTube
        page's background worker can POST cross-origin. Flood-capped
        because each ingest is real Gemini spend. Returns {id} for polling."""
        if not _TOKEN or not _eq(token, _TOKEN):
            self._send_json({"error": "forbidden"}, 403, cors=True)
            return
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 10_000:
            self._send_json({"error": "빈 요청"}, 400, cors=True)
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            url = (data.get("url") or "").strip()
            target = (data.get("target") or "rag").strip().lower()
        except Exception:
            self._send_json({"error": "잘못된 요청"}, 400, cors=True)
            return
        if not url.startswith(("http://", "https://")):
            self._send_json({"error": "URL이 아님"}, 400, cors=True)
            return
        if len(url) > _INGEST_MAX_URL:
            self._send_json({"error": "URL이 너무 깁니다"}, 400, cors=True)
            return
        if target not in ("rag", "note"):
            target = "rag"
        try:
            from ..store import dash_ingest
            # 같은 (URL,종류)가 이미 대기/처리 중이면 큐에 또 넣지 않음
            # (실수로 여러 번 눌러 같은 영상이 쌓이는 것 방지).
            if dash_ingest.has_active(url, target):
                self._send_json(
                    {"duplicate": True, "detail": "이미 학습 큐에 있어요"},
                    cors=True)
                return
            if dash_ingest.recent_count(_INGEST_FLOOD_WINDOW_SEC) >= _INGEST_FLOOD_MAX:
                self._send_json(
                    {"error": "요청이 너무 많아요. 잠시 후 다시."}, 429, cors=True)
                return
            rid = dash_ingest.enqueue(url, target)
        except Exception as e:
            log.exception("ingest enqueue failed")
            self._send_json({"error": f"큐 오류: {e}"}, 500, cors=True)
            return
        self._send_json({"id": rid, "target": target}, cors=True)

    def _handle_ingest_status(self, token: str, rid: int):
        """Poll one parked ingest's status/result (token-gated, CORS)."""
        if not _TOKEN or not _eq(token, _TOKEN):
            self._send_json({"error": "forbidden"}, 403, cors=True)
            return
        try:
            from ..store import dash_ingest
            row = dash_ingest.get(rid)
        except Exception as e:
            log.exception("ingest status failed")
            self._send_json({"status": "error", "error": str(e)}, cors=True)
            return
        if not row:
            self._send_json({"status": "error", "error": "찾을 수 없음"}, cors=True)
            return
        self._send_json({
            "status": row["status"], "target": row["target"],
            "result": row["result"], "error": row["error"],
        }, cors=True)

    def _delete_kg_edge(self, token: str, edge_id: int):
        """Delete one KG relation (dashboard 🗑) + clean up its ★/메모/알람
        marks. Token-gated like the other write routes. sqlite-only, safe
        in the 200MB dashboard container (no chroma)."""
        if not _TOKEN or not _eq(token, _TOKEN):
            self.send_error(403, "forbidden")
            return
        try:
            from ..store import kg, marks, kg_ignore
            tri = kg.delete_edge(edge_id)
            if tri:
                # 영구 무시: 같은 (src,rel,dst)는 문서 재추출돼도 다시 안 들어감.
                try:
                    kg_ignore.add(tri.get("src"), tri.get("rel"), tri.get("dst"))
                except Exception:
                    log.warning("kg_ignore add failed", exc_info=True)
                eid = str(edge_id)
                try:
                    marks.set_mark("kg_edge", eid, False)
                    marks.set_memo("kg_edge", eid, "")
                    marks.clear_alarm("kg_edge", eid)
                except Exception:
                    log.warning("kg edge mark cleanup failed: %s", eid,
                                exc_info=True)
            self._send_ok(1 if tri else 0)
        except Exception as e:
            log.exception("kg edge delete failed")
            self.send_error(500, f"delete failed: {e}")

    def do_HEAD(self):
        if not self._check_basic_auth():
            return
        super().do_HEAD()

    def do_POST(self):
        # Extension ingest is token-gated (the path token is the secret),
        # so it bypasses basic auth — the extension carries only the token.
        ming = _INGEST_RE.match(self.path)
        if ming:
            self._handle_ingest_post(ming.group(1))
            return
        if not self._check_basic_auth():
            return
        mc = _NOTE_CAT_RE.match(self.path)
        if mc:
            self._set_note_category(mc.group(1), unquote(mc.group(2)))
            return
        mi = _NOTE_IMPORTANT_RE.match(self.path)
        if mi:
            self._set_note_important(mi.group(1), unquote(mi.group(2)))
            return
        mq = _QNA_IMPORTANT_RE.match(self.path)
        if mq:
            self._set_qna_important(mq.group(1), int(mq.group(2)))
            return
        mkd = _KG_DELETE_RE.match(self.path)
        if mkd:
            self._delete_kg_edge(mkd.group(1), int(mkd.group(2)))
            return
        mk = _MARK_RE.match(self.path)
        if mk:
            self._set_mark(mk.group(1))
            return
        mm = _MEMO_RE.match(self.path)
        if mm:
            self._set_memo(mm.group(1))
            return
        ma = _ALARM_RE.match(self.path)
        if ma:
            self._set_alarm(ma.group(1))
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

    def _set_note_important(self, token: str, nid: str):
        """Toggle a note's 중요(important) flag from the dashboard ✓ button.
        sqlite-only (mirrors _set_note_category), persisted so it survives
        the static re-render."""
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
            important = 1 if data.get("important") else 0
        except Exception:
            self._send_json({"error": "잘못된 요청"}, 400)
            return
        try:
            with sqlite3.connect(str(_NOTES_DB), timeout=30) as c:
                cols = {r[1] for r in c.execute("PRAGMA table_info(notes)")}
                if "important" not in cols:
                    c.execute("ALTER TABLE notes ADD COLUMN "
                              "important INTEGER DEFAULT 0")
                n = c.execute("UPDATE notes SET important=? WHERE id=?",
                              (important, nid)).rowcount
            if not n:
                self.send_error(404)
                return
            log.info("note %s important → %d", nid, important)
        except Exception as e:
            log.exception("note important set failed")
            self.send_error(500, f"set failed: {e}")
            return
        body = json.dumps({"ok": True, "important": bool(important)}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _set_mark(self, token: str):
        """Generic 중요 표시 토글 for wiki pages / KG edges via the shared
        marks store. Body: {kind, id, important}."""
        if not _TOKEN or not _eq(token, _TOKEN):
            self.send_error(403, "forbidden")
            return
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 2000:
            self._send_json({"error": "빈 요청"}, 400)
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            kind = (data.get("kind") or "").strip()
            item_id = (data.get("id") or "").strip()
            important = bool(data.get("important"))
        except Exception:
            self._send_json({"error": "잘못된 요청"}, 400)
            return
        if kind not in _MARK_KINDS or not item_id:
            self._send_json({"error": "허용되지 않은 대상"}, 400)
            return
        try:
            from ..store import marks
            marks.set_mark(kind, item_id, important)
            log.info("mark %s/%s → %s", kind, item_id[:60], important)
        except Exception as e:
            log.exception("mark set failed")
            self.send_error(500, f"set failed: {e}")
            return
        self._send_json({"ok": True, "important": important})

    def _set_memo(self, token: str):
        """Personal memo upsert for notes/Q&A/wiki/KG-entity via the shared
        marks store. Body: {kind, id, text}. Empty text deletes."""
        if not _TOKEN or not _eq(token, _TOKEN):
            self.send_error(403, "forbidden")
            return
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 20000:
            self._send_json({"error": "빈/과대 요청"}, 400)
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            kind = (data.get("kind") or "").strip()
            item_id = (data.get("id") or "").strip()
            text = data.get("text") or ""
        except Exception:
            self._send_json({"error": "잘못된 요청"}, 400)
            return
        if kind not in _MEMO_KINDS or not item_id:
            self._send_json({"error": "허용되지 않은 대상"}, 400)
            return
        try:
            from ..store import marks
            marks.set_memo(kind, item_id, text)
            log.info("memo %s/%s (%d자)", kind, item_id[:60], len(text.strip()))
        except Exception as e:
            log.exception("memo set failed")
            self.send_error(500, f"set failed: {e}")
            return
        self._send_json({"ok": True, "len": len(text.strip())})

    def _set_alarm(self, token: str):
        """Set/clear a daily KST Telegram alarm for an item. Body:
        {kind, id, hhmm}. Empty hhmm clears it."""
        if not _TOKEN or not _eq(token, _TOKEN):
            self.send_error(403, "forbidden")
            return
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 2000:
            self._send_json({"error": "빈 요청"}, 400)
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            kind = (data.get("kind") or "").strip()
            item_id = (data.get("id") or "").strip()
            hhmm = (data.get("hhmm") or "").strip()
            sdate = (data.get("date") or "").strip()
        except Exception:
            self._send_json({"error": "잘못된 요청"}, 400)
            return
        if kind not in _MEMO_KINDS or not item_id:
            self._send_json({"error": "허용되지 않은 대상"}, 400)
            return
        if hhmm and not _HHMM_RE.match(hhmm):
            self._send_json({"error": "시간 형식(HH:MM) 오류"}, 400)
            return
        if sdate and not _DATE_RE.match(sdate):
            self._send_json({"error": "날짜 형식(YYYY-MM-DD) 오류"}, 400)
            return
        try:
            from ..store import marks
            marks.set_alarm(kind, item_id, hhmm, sdate)
            log.info("alarm %s/%s → %s%s", kind, item_id[:60],
                     hhmm or "(해제)", f" @{sdate}" if sdate else "")
        except Exception as e:
            log.exception("alarm set failed")
            self.send_error(500, f"set failed: {e}")
            return
        self._send_json({"ok": True, "hhmm": hhmm, "date": sdate})

    def _set_qna_important(self, token: str, qid: int):
        """Toggle a Q&A row's 중요 flag from the dashboard ★ button.
        sqlite-only (mirrors _set_note_important)."""
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
            important = 1 if data.get("important") else 0
        except Exception:
            self._send_json({"error": "잘못된 요청"}, 400)
            return
        try:
            with sqlite3.connect(str(_QNA_DB), timeout=30) as c:
                cols = {r[1] for r in c.execute("PRAGMA table_info(qna)")}
                if "important" not in cols:
                    c.execute("ALTER TABLE qna ADD COLUMN "
                              "important INTEGER DEFAULT 0")
                n = c.execute("UPDATE qna SET important=? WHERE id=?",
                              (important, qid)).rowcount
            if not n:
                self.send_error(404)
                return
            log.info("qna #%d important → %d", qid, important)
        except Exception as e:
            log.exception("qna important set failed")
            self.send_error(500, f"set failed: {e}")
            return
        body = json.dumps({"ok": True, "important": bool(important)}).encode()
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
