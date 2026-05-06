"""Hermes-style agent: Gemini function-calling loop.

The model decides which tools to call based on the user's natural-language
message. Loop is bounded by MAX_STEPS to keep cost predictable."""
import json
import logging
import re

from google import genai
from google.genai import types

from .. import config
from ..llm.gemini import complete
from .tools import TOOL_DISPATCH, TOOL_DECLARATIONS

_AUDIT_JSON_RE = re.compile(r"\{.*?\}", re.DOTALL)

log = logging.getLogger(__name__)

_client = genai.Client(api_key=config.GOOGLE_API_KEY)

MAX_STEPS = 4

_SYSTEM = """당신은 사용자의 개인 세컨드브레인 에이전트입니다.
사용자는 한국 개발자/연구자이며 텔레그램으로 대화합니다. 한국어로 답하세요.

# 도구
- search_my_brain: 사용자가 저장한 자료에서 특정 사실/구절 검색 (단일 질문)
- compare_papers: 같은 주제의 여러 자료 요약을 한 번에 모아 비교/종합 (다수 자료 통합)
- search_papers: arXiv/Semantic Scholar 외부 논문 검색
- ingest_url: 새 URL을 저장소에 영구 보관
- recent_docs: 최근 저장한 문서 목록
- web_search: 일반 웹 검색 (Google grounding). 최신 뉴스/시세/동향/오늘 발표 등 저장 자료에 없는 사실 확인.

# 의사결정
1. 다수 자료 인용이 도움 되는 거의 모든 질문은 compare_papers를 우선 사용. limit은 30~50.
   - "전체 / 종합 / 비교 / 정리 / ~ 영역 / ~ 분야 / ~ 동향 / 어디에 적용" 등 광역 질문
   - "어떤 차이 / 핵심 / 주요 / 가장 / 적용된" 등 다중 사례를 묻는 질문
2. 매우 좁은 단일 사실 (특정 수치, 특정 제목)이면 search_my_brain.
3. "찾아줘 / 어떤 논문 / 새로운 / 추천해줘" 등 외부 발견은 search_papers.
4. 저장된 자료에 답이 없거나 "최신/지금/오늘/요즘"이 들어간 질문이면 web_search 호출.
5. URL이 있고 "학습/저장/기억/넣어/추가" 같은 명령조면 ingest_url 호출.
6. URL이 있어도 단순 질문이면 ingest 하지 말고 답변에 집중.
7. 도구 호출은 질의당 최대 3~4회까지. 의미 없는 반복 호출 금지.

# 답변 형식 (텔레그램 plain text)
어떤 마크다운 기호도 쓰지 말 것: **, *, _, #, ##, > 금지.
(예외: 다이어그램이 도움될 때 ```mermaid ... ``` 코드블록 1개를 답변 끝에 추가 가능. 봇이 이 블록을 자동으로 PNG 이미지로 렌더링해서 사진으로 보냅니다.)
시각적 계층은 다음 템플릿을 그대로 따를 것:

  (한 줄짜리 한국어 도입 문장)

  ━━━━━━━━━━━━━━━━━━━━━━
  📌 1. 첫 번째 섹션 제목
  ━━━━━━━━━━━━━━━━━━━━━━

  • 핵심 내용 한 문장 [출처]
  • 또 다른 핵심 [출처]

  ━━━━━━━━━━━━━━━━━━━━━━
  📌 2. 두 번째 섹션 제목
  ━━━━━━━━━━━━━━━━━━━━━━

  • 내용 [출처]
  • 내용 [출처]

규칙:
- 섹션 제목 위/아래에 ━ 22개로 구분선.
- 섹션 제목은 「📌 숫자. 제목」 (이모지 + 번호 + 공백 + 제목, 마침표/콜론 없이).
- 항목 행은 "• " 시작, 들여쓰기 없이.
- 섹션 사이에 빈 줄.
- 출처는 항목 끝 [짧은 제목 또는 도메인]. 같은 출처 여러 항목에 반복 인용 OK.
- 5~10개 핵심 항목 정도로 압축.
- 다양한 출처에서 인용 (한 자료에만 의존하지 말 것).
- web_search 결과는 출처 끝에 [도메인]으로 인용 (예: [techcrunch.com]).

# 다이어그램 (선택, "그림/도식/시각화/플로우/순서도/비교" 같은 단어 들어오면 적극 사용)
답변 끝에 mermaid 코드블록 1개 추가:
```mermaid
graph TD
  A["TCB (Thermo-Compression Bonding)"] --> B["솔더 범프 사용"]
```
봇이 이 코드를 자동으로 PNG 이미지로 변환해 사진으로 전송함.

문법 규칙 (반드시 지킬 것 — 어기면 렌더 실패):
- 노드 라벨에 괄호 ( ), 콜론 :, 슬래시 /, 마침표, 한국어 등 특수문자/조사가 있으면 반드시 큰따옴표로 감싸기.
  예: `A["TCB (열압축 본딩)"]` (O), `A[TCB (열압축 본딩)]` (X)
- 노드 ID(A, B, C 등)는 영문 글자만, 라벨은 따옴표 안에.
- 화살표는 `-->`, `--label-->`, `-.->`, `==>`.
- 다이어그램 종류:
  - 공정 순서 → `graph TD` (위→아래) 또는 `graph LR` (좌→우)
  - 비교 → 두 묶음 + 차이 라벨
  - 계층/구조 → 트리 형태
- 노드 5~10개 이내, 깊이 3 이내.
- ```mermaid 펜스 안에 style 지시문은 넣지 말 것 (호환성).

# 솔직성
- 자료가 부족하면 솔직히 말하고, 무엇을 더 저장하면 좋을지 제안.
- 추측을 사실처럼 단정하지 말 것.
"""


def _extract_calls(content: types.Content) -> list[types.FunctionCall]:
    return [p.function_call for p in (content.parts or []) if p.function_call]


def _extract_text(content: types.Content) -> str:
    return "".join(p.text or "" for p in (content.parts or []) if p.text)


async def _execute(name: str, args: dict) -> dict:
    fn = TOOL_DISPATCH.get(name)
    if not fn:
        return {"error": f"unknown tool: {name}"}
    try:
        return await fn(**args)
    except TypeError as e:
        return {"error": f"bad arguments for {name}: {e}"}
    except Exception as e:
        log.exception("tool %s failed", name)
        return {"error": str(e)}


def _harvest_sources(name: str, result: dict, sources: list[str]) -> None:
    if name == "search_my_brain":
        for h in result.get("hits", []):
            t = h.get("title")
            if t and t not in sources:
                sources.append(t)
    elif name == "search_papers":
        for p in result.get("results", []):
            t = p.get("title")
            tag = f"arXiv:{p['arxiv']}" if p.get("arxiv") else None
            label = f"{t} [{tag}]" if tag else t
            if label and label not in sources:
                sources.append(label)
    elif name == "ingest_url":
        t = result.get("title")
        if t and t not in sources:
            sources.append(t)
    elif name == "compare_papers":
        for p in result.get("papers", []):
            t = p.get("title")
            if t and t not in sources:
                sources.append(t)
    elif name == "web_search":
        from urllib.parse import urlparse
        for src in result.get("sources", []):
            url = src.get("url", "")
            title = src.get("title", "")
            domain = urlparse(url).netloc.replace("www.", "") if url else ""
            label = f"{title} [{domain}]" if title and domain else (title or domain)
            if label and label not in sources:
                sources.append(label)


async def run(message: str, deep: bool = False) -> dict:
    model = config.DEEP_MODEL if deep else config.ANSWER_MODEL
    contents: list[types.Content] = [
        types.Content(role="user", parts=[types.Part.from_text(text=message)])
    ]
    sources: list[str] = []
    tool_calls: list[str] = []

    cfg = types.GenerateContentConfig(
        system_instruction=_SYSTEM,
        tools=[TOOL_DECLARATIONS],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        temperature=0.2,
        max_output_tokens=2048,
    )

    for step in range(MAX_STEPS):
        resp = await _client.aio.models.generate_content(
            model=model, contents=contents, config=cfg
        )
        cand = resp.candidates[0] if resp.candidates else None
        if not cand or not cand.content:
            break
        contents.append(cand.content)

        calls = _extract_calls(cand.content)
        if not calls:
            return {
                "text": _extract_text(cand.content).strip(),
                "sources": sources,
                "tool_calls": tool_calls,
                "steps": step + 1,
                "model": model,
            }

        response_parts: list[types.Part] = []
        for fc in calls:
            args = dict(fc.args or {})
            log.info("tool call: %s(%s)", fc.name, args)
            tool_calls.append(fc.name)
            result = await _execute(fc.name, args)
            _harvest_sources(fc.name, result, sources)
            response_parts.append(types.Part.from_function_response(
                name=fc.name, response=result
            ))
        contents.append(types.Content(role="user", parts=response_parts))

    final = await _client.aio.models.generate_content(
        model=model,
        contents=contents + [types.Content(
            role="user",
            parts=[types.Part.from_text(
                text="위 도구 결과를 바탕으로 최종 답변을 작성하세요."
            )],
        )],
        config=types.GenerateContentConfig(
            system_instruction=_SYSTEM,
            temperature=0.2,
            max_output_tokens=2048,
        ),
    )
    text = ""
    if final.candidates and final.candidates[0].content:
        text = _extract_text(final.candidates[0].content).strip()
    text = text or "도구 호출 한도에 도달했지만 답변을 만들 수 없었습니다."
    return {
        "text": text,
        "sources": sources,
        "tool_calls": tool_calls,
        "steps": MAX_STEPS,
        "model": model,
        "warning": await _verify(message, text, sources),
    }


async def _verify(question: str, answer: str, sources: list[str]) -> str | None:
    """Audit the answer with a cheap Gemini call. Return a short warning
    string when confidence is low, otherwise None. Failures are swallowed
    silently — verification is best-effort and must never block a reply."""
    if not answer or len(answer) < 50:
        return None
    if not sources:
        return "⚠️ 출처 없음 — 답변 근거가 약합니다."
    prompt = (
        f"질문: {question}\n\n"
        f"답변:\n{answer[:1800]}\n\n"
        f"인용 출처: {', '.join(sources[:6])}\n\n"
        "이 답변이 위 출처로 충분히 뒷받침되는가? JSON으로 응답:\n"
        '{"confidence": 1-10, "issue": "문제점 한 줄, 8 이상이면 빈 문자열"}'
    )
    try:
        resp = await complete(
            model=config.SUMMARY_MODEL,
            system="You are a careful answer auditor. Output only JSON.",
            user=prompt,
            max_tokens=200,
            temperature=0.0,
        )
        m = _AUDIT_JSON_RE.search(resp)
        if not m:
            return None
        data = json.loads(m.group(0))
        conf = int(data.get("confidence", 10))
        if conf >= 7:
            return None
        issue = (data.get("issue") or "")[:140]
        return f"⚠️ 신뢰도 {conf}/10 — {issue}"
    except Exception as e:
        log.warning("verify failed: %s", e)
        return None
