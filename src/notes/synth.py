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

_PRINCIPLES = """너는 사용자의 개인 학습 노트를 만드는 조수다. 입력은 사용자가
공부한 자료의 본문이다. 이걸 '검색용 요약'이 아니라 '나중에 다시 열어
되새김질하는 노트'로 재구성한다.

원칙(가장 중요 — 신뢰가 최우선):
- ⛔ 자료에 명시적으로 있는 내용만 쓴다. 일반 지식·배경 설명·추측을 절대
  덧붙이지 마라. 숫자·고유명사·날짜·통계·주장·인용을 지어내지 않는다.
  자료에 근거가 없으면 그냥 쓰지 않는다.
- ⛔ 분량을 채우려고 부풀리거나 추측하지 마라. 자료가 짧거나 빈약하면
  노트도 짧아야 한다. '풍부함'은 오직 자료에 실제로 있는 내용에서만 나온다.
  없는 내용을 보태는 것은 신뢰를 깨는 가장 큰 잘못이다.
- 자료의 내용이 많을 때는 그것을 빠짐없이 구조화한다(없는 걸 보태라는
  뜻이 절대 아니다). 요약하지 말고 핵심 논리·정의·예시를 보존한다.
- ⚠️ 원문이 유튜브 자동 자막처럼 깨졌거나 불명확하면, 추정해서 매끄럽게
  만들지 마라. 불확실한 부분은 그대로 두거나 "(원문 불명확)"으로 표시하고,
  확실히 알 수 있는 것만 정리한다.
- 원문의 표는 반드시 올바른 마크다운 표(헤더행 + |---|---| 구분선행 + 데이터행)로
  재현한다. 구분선 빠진 깨진 표 금지. 수식은 $...$(인라인)/$$...$$(블록)으로 보존.
  숫자·기호·표 값을 임의로 바꾸지 않는다.
- ⛔ 취소선(~~...~~)을 절대 쓰지 마라. 값을 썼다가 고치는 교정 흔적(tracked
  changes)을 남기지 말고, 각 수치·연도는 하나로 확정해서 적는다. 자료에
  값이 엇갈리면 취소선으로 지우지 말고 "A(또는 B)"처럼 괄호로 병기하거나
  더 신뢰되는 값 하나만 쓴다.
- 시각화: '개념 지도'는 Mermaid flowchart로 그린다(```mermaid 블록, flowchart TD).
  ⚠️ Mermaid는 **flowchart만** 사용한다. xychart-beta·pie·graph 등 차트 문법은
  오류가 잦으니 절대 쓰지 말 것 — 수치·추세·비교 데이터는 반드시 **마크다운 표**로
  제시한다. flowchart 한글 라벨은 "큰따옴표"로 감싸고, 화살표는 --> 만 쓰며,
  노드 id는 영문/숫자로 한다(예: A["주도주"] --> B["소외주"]).
  ⛔ style·linkStyle·classDef·class 줄은 절대 쓰지 마라 — 존재하지 않는
  링크 번호 하나로 다이어그램 전체가 렌더 실패한다(실제 사고 2026-08-27).
  subgraph 를 쓸 땐 제목을 "큰따옴표"로 감싼다. 개념 지도도
  자료에 실제로 나온 개념·관계만으로 그린다.
- 한국어로 쓴다(원문 용어/고유명사는 원어 병기 가능).
- 문체: 자연스러운 한국어로 쓴다(번역기·AI 말투 금지). 상투구 "결론적으로·
  요컨대·~라고 할 수 있다·중요한 점은", 기계적 병렬 "첫째·둘째·셋째",
  불필요한 영어 병기, 헤지 "~인 것으로 보인다/~인 듯하다"의 남발을 피한다.
  자료로 단정 가능한 사실은 담백하게 단정하고, 문장 길이는 단조롭지 않게
  변주한다. (내용을 바꾸라는 게 아니라 표현만 자연스럽게.)

"""

_FORMAT_NORMAL = """⚠️ 출력은 정확히 아래 형식만(JSON 금지, 전체를 코드펜스로 감싸지 말 것, 마커
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
===CATEGORY===
종목

질문은 3~5개. recall/concept/application 유형을 섞고, 답은 노트 본문으로
검증 가능해야 한다. NOTE 본문 안에는 ===마커=== 문자열을 절대 쓰지 마라.
CATEGORY는 노트 내용의 종류를 '종목'/'산업'/'전략'/'투자론'/'반도체'/'AI'/
'코인'/'대학원'/'부동산'/'공부'/'그외' 중 하나로만 적는다. 우선순위(위에서부터 먼저 적용):
- 종목 = 개별 기업/종목에 대한 투자·실적 분석. 반도체 회사를 포함해
  특정 회사를 분석하는 자료면 기술 내용이 많아도 투자/실적 관점이
  핵심이면 '종목'이다(반도체·AI보다 우선).
- 산업 = 특정 기업이 아닌 산업/섹터 전체의 동향·전망 (반도체는 아래
  '반도체' 카테고리가 우선 — 반도체 산업 동향도 '반도체'로).
- 전략 = 특정 종목·산업이 아닌 투자/매매 전략·방법론·포트폴리오 구성·
  퀀트 모델·밸류에이션 기법. '어떻게 사고팔/운용할 것인가'라는 실행
  방법이 핵심이면 전략이다(투자론보다 우선).
- 투자론 = 실행 방법이 아니라 투자의 이론·원칙·철학을 다루는 자료.
  가치투자/성장투자 같은 사조, 시장 효율성·리스크·자산배분 이론,
  행동재무학, 투자 대가의 사고방식·투자 원칙, 투자 고전 해설 등.
  바로 따라 할 매매 규칙이 아니라 '투자란 무엇이며 왜 그런가'를
  설명하는 쪽이면 투자론이다.
- 반도체 = 특정 기업 실적·투자 분석이 아닌, 반도체 공정·기술·산업 동향
  자체를 다루는 자료.
- AI = AI·머신러닝·LLM 등 인공지능 기술·산업·모델을 다루는 자료
  (특정 회사 투자분석이 아닌 것).
- 코인 = 암호화폐·비트코인·이더리움·스테이블코인·블록체인·크립토 시장을
  다루는 자료.
- 대학원 = 본인의 대학원 수업·연구·논문 관련 자료.
- 부동산 = 부동산 시장·정책·매매/전세/청약 등 부동산 관련 자료.
- 공부 = 위 어디에도 안 속하는, 특정 기업·투자와 무관한 순수 지식·기술·
  과학·학문 학습.
- 그외 = 일반 거시경제·통화·정책·통계 등 위 열 어디에도 안 맞는 것.
단어 하나만, 설명 없이."""

_SYSTEM = _PRINCIPLES + _FORMAT_NORMAL

# 책 모드 (book-to-skill 아이디어 차용, 2026-08-27 사용자 요청):
# 장문 자료(책·긴 리포트)를 평면 노트가 아니라 되새김질용 구조 —
# 핵심 모델 → 장별 색인 → 용어집 → 치트시트 — 로 합성한다. 원칙부는
# 일반 모드와 완전히 공유하고(신뢰 원칙이 갈라지면 안 됨), 형식부만
# 다르다. 호출은 여전히 1회 — 비용은 출력 토큰만큼만 늘어난다.
_FORMAT_BOOK = """⚠️ 출력은 정확히 아래 형식만(JSON 금지, 전체를 코드펜스로 감싸지 말 것, 마커
줄은 그대로). 단 NOTE 본문 안의 Mermaid/코드 블록은 ```로 표기한다:

===TITLE===
노트 제목 한 줄 (책/자료 제목 중심)
===NOTE===
## 🎯 한 줄 요지
이 자료 전체를 관통하는 주장 한 줄.
## 🧠 핵심 모델
자료 전체를 지배하는 사고 틀·프레임워크 2~5개. 각각 이름 + 2~4문장 설명.
자료에 실제로 제시된 틀만. (관계가 자료에 명시돼 있으면 mermaid flowchart 허용)
## 📑 장별 핵심
자료의 장/섹션 순서 그대로. 각 장마다:
### N장. 장 제목
- 핵심 논지 (자료의 논리·정의·예시 보존, 요약으로 뭉개지 말 것)
- 표·수식이 있으면 마크다운 표 / $...$ 로 재현
## 📖 용어집
- **용어**: 자료 안의 정의 그대로 (자료에 정의가 없으면 싣지 않는다)
## ⚡ 치트시트
실전 참조용 표 하나. 자료 성격에 맞는 형태를 고른다:
- 판단·매매 기준/절차가 있는 자료 → | 상황 | 판단/행동 | 근거 장 |
  행 순서는 실전에서 겪는 시간 순서 — 신호 포착/발생 → 판단 →
  진입/대응 → 모니터링 → 청산/손절.
- 기업/산업 분석처럼 기준이 아니라 사실·논거 중심 자료 →
  | 핵심 포인트 | 근거 (수치·사실) | 근거 장 | 형태로. 판단 기준이 없는
  자료에 상황/행동을 억지로 지어 맞추지 마라.
작성 규칙:
- 셀은 개조식 짧은 구절(한 셀 40자 안팎). "~을 어필", "~하고 있음" 같은
  서술형·홍보형 문장 금지 — 명사구로 끊는다.
- 근거 장 칸은 장/절 번호만 짧게: "1장", "5.8절". 특정할 수 없으면 "-".
  "내용 중 일부분" 같은 모호한 표기 금지.
- 행은 6~12개. 자료에 쓸 내용이 없으면 이 섹션은 "해당 없음" 한 줄만.
===QUESTIONS===
Q: 복습 질문
A: 답
TYPE: recall

Q: ...
A: ...
TYPE: concept
===CATEGORY===
종목
"""

_SYSTEM_BOOK = _PRINCIPLES + _FORMAT_BOOK

_MAX_INPUT_CHARS = 200000  # ~50K tokens; covers a full long report
                           # (flash has a 1M-token context). 60K truncated
                           # 69-page PDFs to their first ~25 pages → thin notes.
# Mermaid 살균 (2026-08-27): 모델이 style/linkStyle/classDef 를 붙이면
# 링크 번호 하나만 어긋나도 Mermaid 가 파스 단계에서 통째로 실패해 노트에
# 원문 코드가 그대로 노출된다(KB 원전 르네상스 북모드 사고). 스타일 줄은
# 순수 장식이라 지워도 정보 손실이 없고, 따옴표 없는 subgraph 제목은
# 감싸서 살린다. 프롬프트 금지 조항의 2차 방어선.
_MM_STYLE_RE = re.compile(r"^\s*(?:style|linkStyle|classDef|class)\b")
_MM_SUBGRAPH_RE = re.compile(r"^(\s*subgraph\s+)(.+)$")


def _sanitize_mermaid(md: str) -> str:
    out, in_mm = [], False
    for line in (md or "").split("\n"):
        st = line.strip()
        if st.startswith("```"):
            in_mm = st.lower().startswith("```mermaid") if not in_mm else False
            out.append(line)
            continue
        if in_mm:
            if _MM_STYLE_RE.match(line):
                continue
            m = _MM_SUBGRAPH_RE.match(line)
            if m:
                title = m.group(2).strip()
                # id["제목"] 형태나 이미 따옴표면 그대로 둔다
                if title and '"' not in title and "[" not in title:
                    line = f'{m.group(1)}"{title}"'
        out.append(line)
    return "\n".join(out)


_MARKER_RE = re.compile(r"(?m)^===(TITLE|NOTE|QUESTIONS|CATEGORY)===\s*$")

# Order matters: _norm_cat() substring-matches in sequence, so 전략 must
# stay ahead of 투자론 — a model answer of "투자전략" then resolves to 전략,
# while a bare "투자론" falls through to 투자론 (neither is a substring of
# the other). 스터디 is deliberately absent: it is the one manual-only
# bucket, curated from the dashboard (2026-08-26).
_CATS = ("종목", "산업", "전략", "투자론", "반도체", "AI", "코인", "대학원",
        "부동산", "공부", "그외")


def _norm_cat(raw: str) -> str:
    """Coerce model output to one of the three categories; default 그외."""
    s = (raw or "").strip()
    for cat in _CATS:
        if cat in s:
            return cat
    return "그외"


async def classify_category(title: str, text: str) -> str:
    """Cheap Flash-Lite classification for backfilling notes that predate
    the category field (new notes get it inline from synthesize()). One
    word: 종목 / 산업 / 전략 / 투자론 / 반도체 / AI / 코인 / 대학원 /
    부동산 / 공부 / 그외."""
    body = (text or "").strip()[:3000]
    if not body:
        return "그외"
    system = (
        "학습 노트의 종류를 한 단어로만 답하라: "
        "종목 / 산업 / 전략 / 투자론 / 반도체 / AI / 코인 / 대학원 / "
        "공부 / 부동산 / 그외.\n"
        "- 종목 = 개별 기업/종목에 대한 투자·실적 분석. 반도체 회사를 포함해 "
        "특정 회사를 분석하는 자료면 기술 내용이 많아도 투자/실적 관점이 "
        "핵심이면 종목(반도체·AI보다 우선).\n"
        "- 산업 = 특정 기업이 아닌 산업/섹터 전체의 동향·전망(반도체 산업은 "
        "'반도체'가 우선).\n"
        "- 전략 = 특정 종목·산업이 아닌 투자/매매 전략·방법론·포트폴리오 "
        "구성·퀀트 모델·밸류에이션 기법. '어떻게 사고팔/운용할 것인가'라는 "
        "실행 방법이 핵심이면 전략(투자론보다 우선).\n"
        "- 투자론 = 실행 방법이 아니라 투자의 이론·원칙·철학. 가치/성장투자 "
        "같은 사조, 시장 효율성·리스크·자산배분 이론, 행동재무학, 투자 "
        "대가의 사고방식·원칙, 투자 고전 해설. 따라 할 매매 규칙이 아니라 "
        "'투자란 무엇이며 왜 그런가'를 설명하면 투자론.\n"
        "- 반도체 = 특정 기업 실적/투자 분석이 아닌, 반도체 공정·기술·산업 "
        "동향 자체를 다루는 자료.\n"
        "- AI = AI·머신러닝·LLM 등 인공지능 기술·산업·모델을 다루는 자료 "
        "(특정 회사 투자분석이 아닌 것).\n"
        "- 코인 = 암호화폐·비트코인·이더리움·스테이블코인·블록체인·크립토 "
        "시장을 다루는 자료.\n"
        "- 대학원 = 본인의 대학원 수업·연구·논문 관련 자료.\n"
        "- 부동산 = 부동산 시장·정책·매매/전세/청약 등 부동산 관련 자료.\n"
        "- 공부 = 위 어디에도 안 속하는, 특정 기업·투자와 무관한 순수 지식·"
        "기술·과학·학문 학습.\n"
        "- 그외 = 일반 거시경제·통화·정책·통계 등 위 열 어디에도 안 맞는 것.\n"
        "다른 말 없이 단어 하나만.")
    user = f"[제목] {title}\n[본문 일부]\n{body}"
    try:
        out = await gemini.complete(
            config.SUMMARY_MODEL, system, user,
            max_tokens=8, temperature=0.0, purpose="note_classify")
    except Exception as e:
        log.warning("note classify failed: %s", str(e)[:120])
        return "그외"
    return _norm_cat(out)


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
                     title: str | None = None,
                     mode: str = "normal") -> dict | None:
    """Return a note dict ready for store.save_note, or None on failure.
    keys: title, source_type, source_ref, md, questions
    mode: "normal" (기본 노트) | "book" (핵심 모델·장별 색인·용어집·
    치트시트 구조 — 장문 자료 되새김질용). 시스템 프롬프트만 다르고
    호출·파싱·비용 기록 경로는 동일하다."""
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
            config.ANSWER_MODEL,
            _SYSTEM_BOOK if mode == "book" else _SYSTEM, user,
            max_tokens=32768, temperature=0.1, purpose="note_synth",
            timeout=300)
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
    note_md = _sanitize_mermaid(note_md)

    today = datetime.now(_KST).date().isoformat()
    llm_title = (sections.get("TITLE") or "").splitlines()[0].strip() \
        if sections.get("TITLE") else ""
    note_title = (title or llm_title or source_ref or "노트").strip()
    header = (f"# {llm_title or note_title}\n"
              f"> 출처: {source_ref} · 학습일: {today} · 유형: {source_type}"
              + (" · 📚 책 모드" if mode == "book" else "") + "\n\n")
    return {
        "title": llm_title or note_title,
        "source_type": source_type,
        "source_ref": source_ref,
        "md": header + note_md + "\n",
        "questions": _parse_questions(sections.get("QUESTIONS", "")),
        "category": _norm_cat(sections.get("CATEGORY", "")),
        "cost_krw": cost_krw,
        "gen_seconds": gen_seconds,
        "mode": mode,
    }
