"""Hermes-style agent: Gemini function-calling loop.

The model decides which tools to call based on the user's natural-language
message. Loop is bounded by MAX_STEPS to keep cost predictable."""
import logging
from google import genai
from google.genai import types

from .. import config
from .tools import TOOL_DISPATCH, TOOL_DECLARATIONS

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

# 의사결정
1. "X편 비교해줘 / 전체 정리해줘 / 종합 리뷰" 같이 다수 자료 통합이면 compare_papers를 호출하되 limit은 충분히 크게 (30 이상).
2. 평범한 단일 질문이면 search_my_brain 먼저 호출.
3. "찾아줘 / 어떤 논문 / 새로운 / 추천해줘" 등 발견 의도면 search_papers.
4. URL이 있고 "학습/저장/기억/넣어/추가" 같은 명령조면 ingest_url 호출 후 그 결과를 알려주기.
5. URL이 있어도 단순 질문이면 ingest 하지 말고 답변에 집중.
6. search_my_brain이 비면 search_papers로 폴백 가능.
7. 도구 호출은 질의당 최대 3~4회까지. 의미 없는 반복 호출 금지.

# 답변 형식 (텔레그램 메시지에서 그대로 보임)
- 마크다운 금지: **, *, _, #, ## 등 절대 쓰지 말 것. 텔레그램은 마크다운 렌더링 안 해서 기호가 그대로 보임.
- 섹션 제목은 한 줄로 쓰고 콜론(:)으로 끝맺기. 예: "1. 핵심 기술:" 또는 "🔑 핵심 기술".
- 항목은 "• " 또는 "- "로 시작. 들여쓰기 없이.
- 빈 줄로 섹션 구분.
- 한 항목 끝에 출처는 한 번만, 형식: [짧은 제목]. 같은 출처가 여러 항목에 나와도 매번 표기.
- 인용한 논문 수가 많으면 좋음 — 사용자는 다양한 자료가 인용된 답을 선호함.
- 답변이 길어질 것 같으면 가장 중요한 5~7개 항목으로 압축.

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
    return {
        "text": text or "도구 호출 한도에 도달했지만 답변을 만들 수 없었습니다.",
        "sources": sources,
        "tool_calls": tool_calls,
        "steps": MAX_STEPS,
        "model": model,
    }
