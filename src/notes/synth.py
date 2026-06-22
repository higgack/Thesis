"""LLM note synthesis — parsed study material → a rich study note.

NOT a summary. Produces a structured, re-readable note (concept map,
sections with tables/formulas preserved, key terms) plus active-recall
questions for the SRS layer.

Output format = **delimiter-based, NOT JSON**. Embedding a large
markdown note (with LaTeX `$\frac{}{}$`, tables, quotes, newlines)
inside a JSON string is fragile — LaTeX backslashes alone break
`json.loads`. Marker sections (===TITLE===/===NOTE===/===QUESTIONS===)
sidestep all escaping, so math-heavy notes parse reliably.

Uses `gemini.complete` on ANSWER_MODEL (flash).
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone, timedelta

from .. import config
from ..llm import gemini
from ..store import cost as _cost

log = logging.getLogger(__name__)

_KST = timezone(timedelta(hours=9))

_SYSTEM = """너는 사용자의 개인 학습 노트를 만드는 조수다. 입력은 사용자가
공부한 자료의 본문이다. 이걸 '검색용 요약'이 아니라 '나중에 다시 열어
되새김질하는 노트'로 재구성한다.

원칙:
- 요약하지 말고 구조화한다. 핵심 논리·정의·예시를 보존한다.
- ⭐ 자료가 길수록 정리를 더 풍부하게: 자료의 모든 주요 섹션/주제를
  빠짐없이 다루고, 각 섹션을 충분한 분량으로 설명한다. 한두 문단으로
  압축하지 말 것. 69쪽짜리면 그에 걸맞은 깊이의 노트를 만든다.
- 원문의 표는 반드시 올바른 마크다운 표(헤더행 + |---|---| 구분선행 + 데이터행)로
  재현한다. 구분선 빠진 깨진 표 금지. 수식은 $...$(인라인)/$$...$$(블록)으로 보존.
  숫자·기호·표 값을 임의로 바꾸지 않는다.
- 시각화: '개념 지도'는 Mermaid flowchart로 그린다(```mermaid 블록). 자료에
  추세·비교 등 수치가 있으면 Mermaid 차트로도 시각화한다(예: flowchart, pie,
  xychart-beta). Mermaid 한글 라벨은 "큰따옴표"로 감싸고 문법 오류가 없게 한다.
- 자료에 없는 내용을 지어내지 않는다. 불확실하면 적지 않는다.
- 한국어로 쓴다(원문 용어/고유명사는 원어 병기 가능).

⚠️ 출력은 정확히 아래 형식만(JSON 금지, 전체를 코드펜스로 감싸지 말 것, 마커
줄은 그대로). 단 NOTE 본문 안의 Mermaid/코드 블록은 ```로 표기한다:

===TITLE===
노트 제목 한 줄 (간결, 핵심 주제)
===NOTE===
## 🎯 한 줄 요지
...
## 🧠 개념 지도
```mermaid
flowchart TD
  A["핵심개념"] --> B["관련 개념"]
  B --> C["결과/적용"]
```
## 📖 정리
### 소제목
설명 (표는 마크다운 표, 수식은 $...$ 그대로)
## 🔑 핵심 용어
- **용어**: 정의
===QUESTIONS===
Q: 복습 질문
A: 답
TYPE: recall

Q: ...
A: ...
TYPE: concept

질문은 3~5개. recall/concept/application 유형을 섞고, 답은 노트 본문으로
검증 가능해야 한다. NOTE 본문 안에는 ===마커=== 문자열을 절대 쓰지 마라."""

_MAX_INPUT_CHARS = 200000  # ~50K tokens; covers a full long report
                           # (flash has a 1M-token context). 60K truncated
                           # 69-page PDFs to their first ~25 pages → thin notes.
_MARKER_RE = re.compile(r"(?m)^===(TITLE|NOTE|QUESTIONS)===\s*$")


def _split_sections(raw: str) -> dict[str, str]:
    parts = _MARKER_RE.split(raw)
    out: dict[str, str] = {}
    # parts = [pre, MARKER, body, MARKER, body, ...]
    for i in range(1, len(parts) - 1, 2):
        out[parts[i]] = parts[i + 1].strip()
    return out


def _parse_questions(block: str) -> list[dict]:
    qs: list[dict] = []
    cur: dict = {}
    for line in block.splitlines():
        s = line.strip()
        if s.startswith("Q:"):
            if cur.get("question"):
                qs.append(cur)
            cur = {"question": s[2:].strip(), "q_type": "recall"}
        elif s.startswith("A:") and cur:
            cur["answer"] = s[2:].strip()
        elif s.upper().startswith("TYPE:") and cur:
            cur["q_type"] = s.split(":", 1)[1].strip() or "recall"
    if cur.get("question"):
        qs.append(cur)
    return [q for q in qs if q.get("question")]


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
    t0 = time.monotonic()
    try:
        out = await gemini.complete(
            config.ANSWER_MODEL, _SYSTEM, user,
            max_tokens=8192, temperature=0.3, purpose="note_synth")
    except Exception as e:
        log.warning("note synth call failed: %s", str(e)[:160])
        return None
    gen_seconds = round(time.monotonic() - t0, 1)
    # Exact KRW from the call we just recorded; fall back to a token
    # estimate if usage_metadata was absent.
    cc = _cost.last_call("note_synth")
    if cc:
        cost_krw = cc["cost_krw"]
    else:
        cost_krw = _cost._price_krw(
            config.ANSWER_MODEL, len(user) // 4, len(out or "") // 4)
    cost_krw = round(cost_krw, 2)

    sections = _split_sections(out or "")
    note_md = sections.get("NOTE", "").strip()
    if not note_md:
        # Model ignored the format — salvage the raw body as the note so
        # a missing marker doesn't lose the whole note.
        note_md = (out or "").strip()
        if len(note_md) < 40:
            log.warning("note synth: empty/unparseable output (%d chars)",
                        len(out or ""))
            return None
        log.warning("note synth: NOTE marker missing, using raw output")

    today = datetime.now(_KST).date().isoformat()
    llm_title = (sections.get("TITLE") or "").splitlines()[0].strip() \
        if sections.get("TITLE") else ""
    note_title = (title or llm_title or source_ref or "노트").strip()
    header = (f"# {llm_title or note_title}\n"
              f"> 출처: {source_ref} · 학습일: {today} · 유형: {source_type}\n\n")
    return {
        "title": llm_title or note_title,
        "source_type": source_type,
        "source_ref": source_ref,
        "md": header + note_md + "\n",
        "questions": _parse_questions(sections.get("QUESTIONS", "")),
        "cost_krw": cost_krw,
        "gen_seconds": gen_seconds,
    }
