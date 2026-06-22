"""LLM note synthesis — parsed study material → a rich study note.

NOT a summary. Produces a structured, re-readable note (concept map,
sections with tables/formulas preserved, key terms, related-note links)
plus active-recall questions for the SRS layer.

Uses `gemini.complete` on ANSWER_MODEL (flash) — notes are re-read
often, so quality matters more than the flash-lite summary path. Output
is requested as JSON so the markdown body and the recall questions come
back in one call (≈ ₩18/note).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone, timedelta

from .. import config
from ..llm import gemini

log = logging.getLogger(__name__)

_KST = timezone(timedelta(hours=9))

_SYSTEM = """너는 사용자의 개인 학습 노트를 만드는 조수다. 입력은 사용자가
공부한 자료의 본문이다. 이걸 '검색용 요약'이 아니라 '나중에 다시 열어
되새김질하는 노트'로 재구성한다.

원칙:
- 요약하지 말고 구조화한다. 핵심 논리·정의·예시를 보존한다.
- 원문의 표는 마크다운 표로, 수식은 $...$(인라인)/$$...$$(블록)으로 보존.
  숫자·기호·표 값을 임의로 바꾸지 않는다.
- 자료에 없는 내용을 지어내지 않는다. 불확실하면 적지 않는다.
- 한국어로 쓴다(원문 용어/고유명사는 원어 병기 가능).

반드시 아래 JSON 한 개만 출력한다(코드펜스 금지):
{
  "title": "노트 제목(간결, 핵심 주제)",
  "md": "마크다운 노트 본문 — 아래 섹션 순서를 따른다",
  "questions": [
    {"question": "...", "answer": "...", "q_type": "recall|concept|application"}
  ]
}

md 본문 섹션 순서:
## 🎯 한 줄 요지
## 🧠 개념 지도   (핵심 개념 - 개념 간 관계, 불릿)
## 📖 정리        (### 소제목들로 구조화, 표/수식 보존)
## 🔑 핵심 용어   (**용어**: 정의)
questions 는 3~5개. recall(사실 회상)/concept(개념 이해)/application(적용)
유형을 섞는다. 답은 노트 본문으로 검증 가능해야 한다."""

_MAX_INPUT_CHARS = 60000  # ~15K tokens; trims pathological inputs


def _parse_json(raw: str) -> dict | None:
    s = raw.strip()
    # Strip accidental ```json fences.
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except Exception:
        # Last resort: grab the outermost {...} block.
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


async def synthesize(source_type: str, source_ref: str, raw_text: str,
                     title: str | None = None) -> dict | None:
    """Return a note dict ready for store.save_note, or None on failure.

    keys: title, source_type, source_ref, md, questions
    """
    body = (raw_text or "").strip()
    if len(body) < 40:
        log.info("synth skipped: body too short (%d chars)", len(body))
        return None
    if len(body) > _MAX_INPUT_CHARS:
        body = body[:_MAX_INPUT_CHARS]

    user = (
        (f"[원본 제목] {title}\n" if title else "")
        + f"[출처 유형] {source_type}\n[출처] {source_ref}\n\n"
        + "[본문]\n" + body
    )
    try:
        out = await gemini.complete(
            config.ANSWER_MODEL,
            _SYSTEM,
            user,
            max_tokens=4096,
            temperature=0.3,
            purpose="note_synth",
        )
    except Exception as e:
        log.warning("note synth call failed: %s", str(e)[:160])
        return None

    data = _parse_json(out)
    if not data or not data.get("md"):
        log.warning("note synth: unparseable/empty output")
        return None

    today = datetime.now(_KST).date().isoformat()
    note_title = (title or data.get("title") or source_ref or "노트").strip()
    header = (f"# {data.get('title') or note_title}\n"
              f"> 출처: {source_ref} · 학습일: {today} · 유형: {source_type}\n\n")
    return {
        "title": data.get("title") or note_title,
        "source_type": source_type,
        "source_ref": source_ref,
        "md": header + data["md"].strip() + "\n",
        "questions": [
            {"question": q.get("question", ""),
             "answer": q.get("answer", ""),
             "q_type": q.get("q_type", "recall")}
            for q in (data.get("questions") or [])
            if q.get("question")
        ],
    }
