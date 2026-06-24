"""Document → atomic factual triples (kg-gen inspired), via the existing
Gemini stack (Flash-Lite) — no kg-gen/LiteLLM/DSPy dependency, no GPU.

Delimiter output (not JSON) for robustness, and fidelity-first prompting
(same lesson as note synthesis): only triples explicitly supported by the
text, no invented relations. Each line: `주어|관계|목적어|신뢰도`.
"""
import logging

from .. import config
from ..llm import gemini

log = logging.getLogger(__name__)

_SYSTEM = """입력 자료에서 '사실 트리플'을 추출한다. 각 트리플은
(주어 | 관계 | 목적어) 형식의 원자적 사실이다.

원칙:
- ⛔ 자료에 명시적으로 있는 사실만 추출한다. 추측·일반지식·배경설명 금지.
- 주어/목적어는 구체적 개체(회사·인물·제품·기술·지표·개념). 관계는 짧은
  동사구(예: 주력제품, 인수, 경쟁사, 공급, 실적증가, 소속).
- 한국어로 쓰되 고유명사는 원문대로.
- 같은 개체명은 일관되게 표기(약칭/풀네임 섞지 말 것).
- 최대 30개. 핵심·중요 관계 우선.

출력은 한 줄에 하나씩, 정확히 이 형식만(번호·설명·머리말 금지):
주어|관계|목적어|신뢰도
신뢰도는 0~1 (자료에 분명하면 0.9+, 암시적이면 낮게).
예) 삼성전기|주력제품|MLCC|0.95
추출할 사실이 없으면 아무것도 출력하지 않는다."""


def _parse(out: str) -> list[dict]:
    triples: list[dict] = []
    for line in (out or "").splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        src, rel, dst = parts[0], parts[1], parts[2]
        conf = 0.5
        if len(parts) >= 4:
            try:
                conf = max(0.0, min(1.0, float(parts[3])))
            except Exception:
                conf = 0.5
        if src and rel and dst:
            triples.append({"src": src, "rel": rel, "dst": dst,
                            "confidence": conf})
    return triples[:30]


async def extract(title: str, text: str) -> list[dict]:
    """Return atomic triples for one document. Cheap (Flash-Lite, one
    call). Empty on too-short input or failure."""
    body = (text or "").strip()
    if len(body) < 40:
        return []
    user = f"[제목] {title}\n[자료]\n{body[:8000]}"
    try:
        out = await gemini.complete(
            config.SUMMARY_MODEL, _SYSTEM, user,
            max_tokens=2048, temperature=0.1, purpose="kg_extract")
    except Exception as e:
        log.warning("kg extract failed: %s", str(e)[:120])
        return []
    return _parse(out)
