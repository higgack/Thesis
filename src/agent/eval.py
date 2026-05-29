"""Answer-quality regression eval for the Q&A agent.

Load a JSON golden set from data/eval_golden.json, run each entry
through agent.run(), and score:
  - source_hit: any expected_sources keyword found in returned source
    titles or answer text (OR match — one keyword is enough).
  - fact_hit: fraction of expected_facts present (case-insensitive
    substring) in the answer text (AND — every fact must appear).
  - passed: source_hit AND fact_hit == 1.0.

Empty arrays ([]) for either key skip that dimension.

Run via the /eval Telegram command (owner-only).
"""
import json
import logging
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)

GOLDEN_PATH = config.DATA_DIR / "eval_golden.json"

_TEMPLATE = {
    "_instructions": [
        "각 항목을 실제 학습된 문서의 질문으로 교체하세요.",
        "expected_sources: 반환 출처 제목에 포함돼야 할 키워드 (OR — 하나라도 매치되면 통과).",
        "expected_facts: 답변 본문에 모두 포함돼야 통과 (AND, 대소문자 무시).",
        "둘 다 [] → 해당 체크 스킵. id는 고유한 짧은 문자열.",
        "/eval 로 실행. 수정 후 재실행하면 최신 결과 반영.",
    ],
    "items": [],
}


def load_golden(path: Path = GOLDEN_PATH) -> list[dict]:
    """Load and return the items list from the golden JSON file."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text("utf-8"))
        return data.get("items") or []
    except Exception as e:
        log.error("eval: failed to load %s: %s", path, e)
        return []


def ensure_template(path: Path = GOLDEN_PATH) -> None:
    """Write the template file if it doesn't exist yet."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_TEMPLATE, ensure_ascii=False, indent=2), "utf-8")
    log.info("eval: created template at %s", path)


def _norm_q(q: str) -> str:
    return " ".join((q or "").lower().split())


# Answer strings that mean "no result" — don't seed eval items from
# turns the agent couldn't answer (nothing useful to regress against).
_REFUSAL_MARKERS = ("찾지 못했", "없습니다", "학습되지 않", "정보가 없")


def seed_from_qna(n: int = 20, path: Path = GOLDEN_PATH) -> dict:
    """Build a starter golden set from recent REAL Q&A turns.

    Objective retrieval-regression seed:
      • query            = the actual past question
      • expected_sources = a distinctive slice of the recorded source
                           titles (so the eval checks "does the right
                           doc still surface?" — the recorded sources
                           were what the agent actually cited)
      • expected_facts   = [] (left blank; the user fills the 1-2 facts
                           per item they want fact-checked, so we avoid
                           circularly grading the agent against its own
                           prior wording)

    APPENDS new queries to any existing golden set — never clobbers
    user-curated items (dedup by normalised query). Skips refusals and
    trivially short answers. Returns {added, total, scanned}."""
    from ..store import qna as _qna

    existing: list[dict] = []
    if path.exists():
        try:
            existing = (json.loads(path.read_text("utf-8")).get("items") or [])
        except Exception:
            existing = []
    seen = {_norm_q(it.get("query", "")) for it in existing}

    rows = _qna.recent(limit=max(200, n * 8))
    seeded: list[dict] = []
    for r in rows:
        q = (r.get("question") or "").strip()
        a = (r.get("answer") or "").strip()
        srcs = [s for s in (r.get("sources") or []) if (s or "").strip()]
        if not q or len(a) < 80:
            continue
        if any(m in a for m in _REFUSAL_MARKERS) and not srcs:
            continue
        nq = _norm_q(q)
        if nq in seen:
            continue
        seen.add(nq)
        exp_src = [s.strip()[:30] for s in srcs[:2]]
        seeded.append({
            "id": f"seed{len(existing) + len(seeded) + 1:03d}",
            "query": q[:200],
            "expected_sources": exp_src,
            "expected_facts": [],
        })
        if len(seeded) >= n:
            break

    payload = {"_instructions": _TEMPLATE["_instructions"],
               "items": existing + seeded}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)
    log.info("eval: seeded %d new items (total %d)", len(seeded),
             len(payload["items"]))
    return {"added": len(seeded), "total": len(payload["items"]),
            "scanned": len(rows)}


def _score_item(item: dict, result: dict) -> dict:
    out = {
        "id": item.get("id", "?"),
        "query": item.get("query", ""),
        "source_hit": False,
        "fact_hit": 0.0,
        "fact_detail": {},
        "passed": False,
        "status": "ok",
    }

    if result.get("status") == "pending_pro_confirmation":
        out["status"] = "pro_gate"
        return out

    answer = result.get("text") or ""
    sources = result.get("sources") or []

    if not answer or (
        any(m in answer for m in ("찾지 못했", "없습니다", "학습되지 않", "정보가 없"))
        and not sources
    ):
        out["status"] = "no_result"

    expected_src = item.get("expected_sources") or []
    if expected_src:
        # Search in source titles + full answer text
        haystack = " ".join(s.lower() for s in sources) + " " + answer.lower()
        out["source_hit"] = any(kw.lower() in haystack for kw in expected_src)
    else:
        out["source_hit"] = True  # no expectation → dimension skipped

    expected_facts = item.get("expected_facts") or []
    if expected_facts:
        detail = {f: f.lower() in answer.lower() for f in expected_facts}
        out["fact_detail"] = detail
        out["fact_hit"] = sum(detail.values()) / len(detail)
    else:
        out["fact_hit"] = 1.0  # dimension skipped

    out["passed"] = out["source_hit"] and out["fact_hit"] >= 1.0
    return out


async def run_eval(path: Path = GOLDEN_PATH) -> dict:
    """Run all golden items through the agent and return scored results."""
    from . import agent as _agent  # lazy — avoids google-genai at import time

    items = load_golden(path)
    if not items:
        return {"items": [], "total": 0, "passed": 0, "score": 0.0,
                "error": "empty_golden"}

    scored = []
    for item in items:
        try:
            result = await _agent.run(item["query"], deep=False)
        except Exception as e:
            log.exception("eval: agent failed for id=%s", item.get("id"))
            scored.append({
                "id": item.get("id", "?"),
                "query": item.get("query", ""),
                "status": f"error: {e}",
                "passed": False,
                "source_hit": False,
                "fact_hit": 0.0,
                "fact_detail": {},
            })
            continue
        scored.append(_score_item(item, result))

    passed = sum(1 for r in scored if r.get("passed"))
    total = len(scored)
    return {
        "items": scored,
        "total": total,
        "passed": passed,
        "score": passed / total if total else 0.0,
    }


def format_report(ev: dict) -> str:
    """Format eval results as a Telegram-ready HTML string."""
    if ev.get("error") == "empty_golden":
        return (
            "📋 <b>Eval 골든셋이 비어있어요</b>\n\n"
            "<code>data/eval_golden.json</code>을 편집해서 항목을 추가하세요.\n\n"
            "<b>항목 형식:</b>\n"
            '<code>{"id":"q001", "query":"질문 내용",\n'
            ' "expected_sources":["출처 키워드"],\n'
            ' "expected_facts":["답변에 포함돼야 할 사실"]}</code>\n\n'
            "• <code>expected_sources</code> — 반환 출처 제목의 키워드 (OR)\n"
            "• <code>expected_facts</code> — 답변 본문에 모두 포함돼야 통과 (AND)\n"
            "• 빈 배열 <code>[]</code> → 해당 체크 스킵\n\n"
            "항목 추가 후 <code>/eval</code> 재실행."
        )

    total = ev["total"]
    passed = ev["passed"]
    score = ev["score"]
    icon = "✅" if score >= 0.8 else ("⚠️" if score >= 0.5 else "❌")
    lines = [f"{icon} <b>Eval {passed}/{total} 통과 ({score*100:.0f}%)</b>\n"]

    for r in ev["items"]:
        pid = r.get("id", "?")
        q = (r.get("query") or "")[:45]
        status = r.get("status", "ok")

        if status == "pro_gate":
            lines.append(f"⏸ <code>{pid}</code> {q}… — Pro확인 필요")
        elif status == "no_result":
            lines.append(f"❌ <code>{pid}</code> {q}… — 결과없음")
        elif str(status).startswith("error"):
            lines.append(f"💥 <code>{pid}</code> {q}… — 오류")
        elif r.get("passed"):
            fp = int(r.get("fact_hit", 1.0) * 100)
            lines.append(f"✅ <code>{pid}</code> {q}… — 출처✓ 사실{fp}%")
        else:
            src = "✓" if r.get("source_hit") else "✗"
            fp = int(r.get("fact_hit", 0.0) * 100)
            lines.append(f"❌ <code>{pid}</code> {q}… — 출처{src} 사실{fp}%")

        # Surface which facts missed on failures
        if not r.get("passed") and r.get("fact_detail"):
            missed = [f for f, hit in r["fact_detail"].items() if not hit]
            if missed:
                for mf in missed[:3]:
                    lines.append(f"    ✗ '{mf[:40]}'")

    return "\n".join(lines)
