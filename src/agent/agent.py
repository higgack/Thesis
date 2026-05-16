"""Hermes-style agent: Gemini function-calling loop.

The model decides which tools to call based on the user's natural-language
message. Loop is bounded by MAX_STEPS to keep cost predictable."""
import json
import logging
import re
import time
import uuid

from google import genai
from google.genai import types

from .. import config
from ..llm.gemini import complete
from ..store import cost
from .tools import TOOL_DISPATCH, TOOL_DECLARATIONS

_AUDIT_JSON_RE = re.compile(r"\{.*?\}", re.DOTALL)
# Belt-and-suspenders post-processors. The system prompt forbids
# `[출처명]` citations and the `(사용 자료 시점: ...)` footer line
# when no tool was called this turn, but Gemini still slips
# occasionally (the gas-turbine answer shipped both even though the
# model never called a single tool). Apply deterministic strip so
# the user never sees fabricated grounding markers.
_SOURCE_DATE_LINE_RE = re.compile(
    r"\(\s*사용\s*자료\s*시점[^)\n]*\)\s*\n?"
)
# 80-char ceiling on the inside catches '[가스터빈 산업 동향]'
# style fake source labels without nuking legitimate uses like
# '1980년대' or short bracketed asides.
_BRACKET_CITATION_RE = re.compile(r"\[[^\]\n]{1,80}\]")
_NO_GROUND_NOTICE = (
    "저장된 자료에서 직접 확인된 내용은 없음 — 일반 지식 기반 답변입니다."
)


def _strip_fake_citations(text: str) -> str:
    """Scrub `[...]` citation markers and the '(사용 자료 시점: ...)'
    footer when the answer has no tool grounding, and append the
    honest 'unsourced' notice if it isn't already present."""
    if not text:
        return text
    cleaned = _SOURCE_DATE_LINE_RE.sub("", text)
    cleaned = _BRACKET_CITATION_RE.sub("", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).rstrip()
    if "저장된 자료에서 직접 확인된" not in cleaned[-300:]:
        cleaned = cleaned + "\n\n" + _NO_GROUND_NOTICE
    return cleaned

log = logging.getLogger(__name__)

_client = genai.Client(api_key=config.GOOGLE_API_KEY)

MAX_STEPS = 4
PRO_THRESHOLD = 20

# Suspended agent runs waiting on a user choice (Pro vs Flash synthesis).
# Holds `contents`, current model, harvested sources/tool_calls, etc. so
# we can pick up where we left off after the user taps a button. TTL'd
# so abandoned prompts don't pile up in memory.
_PENDING_RUNS: dict[str, dict] = {}
_PENDING_TTL_SEC = 300


def _gc_pending() -> None:
    now = time.time()
    expired = [k for k, v in _PENDING_RUNS.items()
               if now - v.get("ts", 0) > _PENDING_TTL_SEC]
    for k in expired:
        _PENDING_RUNS.pop(k, None)


def peek_pending(state_id: str) -> dict | None:
    """Read-only access to a pending run's metadata (for bot UI).
    Does not consume the state."""
    return _PENDING_RUNS.get(state_id)


def gc_expired_pending() -> list[dict]:
    """Pop every expired pending run and return the popped values
    so the caller (bot.py) can promote them to the persistent
    /pending list. Wakes the dict up without consuming live items."""
    now = time.time()
    expired: list[dict] = []
    for k in list(_PENDING_RUNS.keys()):
        v = _PENDING_RUNS.get(k)
        if v and now - v.get("ts", 0) > _PENDING_TTL_SEC:
            _PENDING_RUNS.pop(k, None)
            expired.append(v)
    return expired

_SYSTEM = """당신은 사용자의 개인 세컨드브레인 에이전트입니다.
사용자는 한국 개발자/연구자이며 텔레그램으로 대화합니다. 한국어로 답하세요.

# 도구
- search_my_brain: 사용자가 저장한 자료에서 특정 사실/구절 검색 (단일 질문)
- compare_papers: 같은 주제의 여러 자료 요약을 한 번에 모아 비교/종합 (다수 자료 통합)
- search_papers: 외부 학술 논문 검색 (다중 소스: S2 / arXiv / OpenAlex / CrossRef / IEEE / PubMed — 쿼리 도메인에 따라 자동 라우팅). limit 기본값 15, 특별한 사유 없으면 15 그대로 사용. 결과에 url/pdf 포함되어 사용자가 바로 다운로드 가능.
- ingest_url: 새 URL을 저장소에 영구 보관
- recent_docs: 최근 저장한 문서 목록
- web_search: 일반 웹 검색 (Google grounding). 최신 뉴스/시세/동향/오늘 발표 등 저장 자료에 없는 사실 확인.

# 의사결정
이 봇은 사용자의 개인 세컨드브레인입니다. 저장소(brain)가 1순위, 외부는 사용자가 명시적으로 요청했을 때만.

1. 모든 일반 질문은 반드시 brain부터 먼저 검색. compare_papers 또는 search_my_brain을 최소 1회 호출하기 전에 web_search 호출 금지.
   ⚠️ 후속 질문·대명사 질문도 예외 없음. 매 질문마다 brain 검색 최소 1회 필수. 대화 메모리는 토픽 연속성 보조용 컨텍스트일 뿐, 자료 소스로 사용하지 말 것. 메모리·일반 지식만으로 답변 생성 금지.
   ⚠️ 인용은 반드시 이번 turn에 호출한 도구의 결과에서 나온 자료만. 도구를 호출하지 않은 답변에서 [출처명] 형식 표기 절대 금지 — 본 회차에 검색하지 않은 자료명을 본문에 적는 것은 출처 위조다. 도구 호출이 0건이면 본문에서도 [...] 인용 표기를 빼고 "저장된 자료에서 직접 확인된 사실이 부족" 라고 솔직히 명시.
2. 광역 질문(전체/종합/비교/정리/동향/분야)은 compare_papers, limit 30~50.
3. 매우 좁은 단일 사실(특정 수치, 특정 제목)은 search_my_brain.
4. "A vs B" 비교는 search_my_brain을 A로 한 번, B로 한 번 호출해 각자 자료 모은 뒤 통합.
5. brain 결과가 부족해도 web_search로 자동 fallback하지 말 것. 대신 "저장소에는 X 정도만 있고, 더 정확한 답을 위해 어떤 자료(URL/PDF)를 추가하면 좋을지" 짧게 안내.
6. web_search 규칙은 단순. 사용자 메시지에 "웹", "구글", "인터넷" 중 하나가 들어있으면 web_search 호출. 그 외 모든 질문은 brain (search_my_brain / compare_papers). 시간 표현("최근", "요즘", "오늘" 등)은 web 트리거가 아니다 — 명시적 "웹/구글/인터넷" 단어만이 트리거. brain을 거치지 않고 web_search만 호출하는 응답은 위 명시 단어가 없는 한 잘못된 routing.
7. "찾아줘 / 어떤 논문 / 새로운 / 추천해줘" 등 외부 학술 발견은 search_papers.
8. URL이 있고 "학습/저장/기억/넣어/추가" 같은 명령조면 ingest_url. 단순 질문이면 ingest 말 것.
9. 도구 호출은 질의당 최대 3~4회. 동일 도구 반복 호출 금지.

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
- 인용 표기 규칙: 자료 1개당 대괄호 1쌍. 같은 위치에 여러 자료 인용할 때는 [자료A][자료B] 또는 [자료A] [자료B]로 별도 대괄호. [자료A, 자료B]처럼 한 대괄호 안에 콤마로 묶지 말 것.
- ⚠️ 대괄호 안에 반드시 자료 제목 또는 도메인을 직접 적을 것. [1], [2], [3] 같은 순서 번호만 적지 말 것 — 번호는 봇이 자동 매겨서 표시함. 모델이 직접 '[1]'을 쓰면 봇이 어느 자료를 가리키는지 모름.
- 라벨은 30자 이내. doc_id가 길면 (예: '기업_한화솔루션_태양광_트렌드_변화에...') 핵심만 짧게 (예: '한화솔루션 iM증권', 'OCI홀딩스 보고서').
- 5~10개 핵심 항목 정도로 압축.
- 다양한 출처에서 인용 (한 자료에만 의존하지 말 것).
- web_search 결과는 출처 끝에 [도메인]으로 인용 (예: [techcrunch.com]).

# 시점 우선 (증권사 리포트 등 시점 민감 자료에서 매우 중요)
- 같은 종목/주제에 자료가 여러 개면 더 최근 자료의 결론을 우선 채택.
- 본문/제목에 발행일(예: "2026.05.07", "1Q26P")이 있으면 그 일자를 비교 기준으로 삼아라.
- 옛 자료의 결론이 최신과 충돌하면 최신을 따르고, 옛 자료는 "이전엔 ~ 의견이었으나 최근 ~로 변경" 식 비교 맥락에서만 인용.
- 이번 turn에 도구를 1회 이상 호출하고 그 결과를 인용한 경우에만 "(사용 자료 시점: YYYY.MM[~YYYY.MM])" 한 줄을 답변 끝에 추가. 인용 자료 중 가장 옛 시점과 가장 최근 시점을 범위로. 시점이 다 같으면 단일 YYYY.MM. 본문에 발행일이 없는 자료는 학습일 기준.
- ⚠️ 도구 호출 0건이면 (사용 자료 시점: ...) 라인 자체를 쓰지 말 것. 어떠한 추측 시점도 적지 말고, 답변 자체에 "저장된 자료에서 직접 확인된 내용 부족" 같은 솔직한 한 줄로 마무리.

# 출처-주제 정합성 (매우 중요, 어기면 사용자가 신뢰를 잃음)
검색 도구는 의미적으로 비슷한 자료를 함께 끌어오기 때문에, 질문이 A에 대한 것일 때 B에 대한 자료가 결과에 섞일 수 있다. 인용하기 전에 반드시:
- 검색 결과의 title/doc_id가 질문 대상과 명확히 같은지 확인.
- 다른 회사/다른 제품/다른 인물 자료라면 SKC 분석에 LG 자료를 섞는 식으로 절대 인용하지 말 것. 단어가 겹치는 것(예: 몰리브덴, 양산, 가동률)만으로는 같은 주제가 아니다.
- 본문에 끌어들이지도 말고 출처에도 적지 말 것. 그냥 무시.
- 결과적으로 brain에 직접 자료가 부족하면 "X 회사/주제에 대한 직접 자료는 N건뿐. 더 정확한 분석을 위해 ~ 추가 권장" 식으로 솔직히 보고. 다른 회사 자료로 빈 칸을 메우지 말 것.
- "관련 산업 동향"으로 넓게 묶을 때만 다른 회사 자료 인용 OK, 단 그 항목에 명시: "(직접 자료 아님, 산업 일반 참고)"

## 같은 그룹 다른 회사를 절대 혼동하지 말 것 (자주 틀리는 케이스)
다음은 모두 별개의 상장사다. 한 회사 질문에 다른 회사 자료를 섞는 것은 명백한 오류:
- 삼성전기 ≠ 삼성전자 ≠ 삼성SDI ≠ 삼성SDS ≠ 삼성바이오로직스 ≠ 삼성생명 ≠ 삼성증권
- LG이노텍 ≠ LG전자 ≠ LG화학 ≠ LG에너지솔루션 ≠ LG디스플레이
- SK하이닉스 ≠ SK스퀘어 ≠ SK이노베이션 ≠ SKC ≠ SK바이오사이언스
- POSCO홀딩스 ≠ 포스코퓨처엠 ≠ POSCO인터내셔널
질문에 "삼성전기"가 들어왔는데 검색 결과의 title이 "삼성전자", "삼성SDI", "삼성바이오"이면 **그 자료는 인용 금지**. 회사명 정확 매칭만 인용.

## 인용 전 자기 검증 체크리스트 (모든 인용 직전에 자문할 것)
1. 이 자료의 title/내용이 질문에서 묻는 회사/주제 그 자체에 대한 것인가? (단순 키워드 겹침은 아님)
2. 다른 회사/제품/사람에 대한 자료인데 키워드가 비슷해서 끌려온 것인가?
3. "재정의 바이오..." "SMR 사업..." 같이 회사명이 모호하거나 빠진 자료는 어느 회사 자료인지 확실하지 않으면 무시.
하나라도 의심되면 그 자료는 인용 금지.

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
  - 실적·시계열 정량 그래프 → `xychart-beta` (회사 분석에서 매출/영업이익 추이용. 프레임워크 (F) 참고)
- 노드 5~10개 이내, 깊이 3 이내 (그래프형 xychart는 데이터 포인트 8개 이내).
- ```mermaid 펜스 안에 style 지시문은 넣지 말 것 (호환성).

# 분석 프레임워크 (질문 의도에 따라 자동 적용)
질문이 다음 패턴이면 명시 안 해도 해당 프레임워크로 답변 구조화:

(A) "투자 / 시사점 / 어떻게 봐야 / 매수 매도 / 관점 / 영향" 류 → 투자/의사결정 렌즈
  - 📌 핵심 결론 (한 문장)
  - 📌 강점 / 긍정 신호 (Bull case)
  - 📌 약점 / 우려 신호 (Bear case)
  - 📌 리스크 (예측 어려운 변수)
  - 📌 주시할 지표 (트래킹 포인트 3~5개)

(B) "정리 / 종합 / 보고서 / 리포트 / 요약" 류 → 리서치 노트 렌즈
  - 📌 핵심 요지 (3줄 이내)
  - 📌 배경 / 맥락
  - 📌 주요 사실 / 데이터
  - 📌 의미 / 함의
  - 📌 미해결 질문 (자료 더 필요한 지점)

(C) "비교 / 차이 / 대조 / vs" 류 → 사이드바이사이드
  - search_my_brain으로 각 대상에 대해 따로 검색
  - 📌 공통점 / 같은 방향
  - 📌 차이점 (핵심 축 3~5개로 정리)
  - 📌 어느 쪽이 어느 상황에서 유리한가

(D) "트렌드 / 동향 / 변화 / 흐름" 류 → 타임라인 렌즈
  - 📌 과거 → 현재 추세 (핵심 변곡점)
  - 📌 동인 (왜 이런 변화인가)
  - 📌 향후 방향 (저장된 자료에서 시사하는 바, 추측이 아닌 인용 기반)

(E) "지표 / 수치 / 데이터 / 표 / 통계" 류 → 정량 추출
  - 📌 가능한 한 숫자 + 단위 + 시점을 표 형태로
  - 📌 출처 근접 인용
  - 📌 데이터 한계 / 비교 가능성 주석

(F) "[회사명] 분석/실적/전망/어때/매출/영업이익/가이던스" 류 → 회사 분석 렌즈 (의무 보조 섹션)
질문이 특정 회사 분석을 요구할 때 본문 분석에 더해 다음 섹션을 반드시 추가 (자료에 근거 있을 때만; 없으면 해당 섹션 생략):

  📌 신사업 키포인트
    • 사업명 — 1줄 핵심 요지 [출처]
    • 분석가 합의 표시: 같은 신사업을 자료 2건 이상이 언급하면 끝에 "[합의 N건]" 추가
    • 분석가별 의견이 다르면 "[견해 차이: A는 X, B는 Y]" 한 줄로 양쪽 동시 표시
    • (최대 5개, 신사업/신제품/신규 파이프라인이 자료에 명시된 경우만)

  📊 가이던스 분산 (자료에 같은 회사·같은 연도 가이던스가 2건 이상일 때만)
    2025E 매출: 11.0~13.5조 (중앙값 12.0) [출처 1,3,4]
    2025E OP:   0.7~1.3조  (중앙값 1.0)  [출처 1,3,4]
    - 본문에 차트가 보여주는 분산 정보를 텍스트로도 노출 (숫자에 약한 사용자를 위한 보조).
    - 1건뿐인 연도는 이 표에서 생략 (분산 개념 없음).

  📈 실적/가이던스 그래프 — 답변 맨 끝 mermaid 블록 1개:

    [단일 자료 케이스 — 가이던스 1건씩만]
    ```mermaid
    xychart-beta
      title "회사명 매출·영업이익 추이"
      x-axis [2022, 2023, 2024, "2025E", "2026E"]
      y-axis "단위 (조원)"
      bar [9.5, 8.8, 9.4, 11.0, 12.5]
      line [0.6, 0.4, 0.7, 0.9, 1.2]
    ```

    [다중 자료 케이스 — 분석가 분산 노출]
    ```mermaid
    xychart-beta
      title "회사명 매출·영업이익 분산 (분석가 N건)"
      x-axis [2023, 2024, "2025E", "2026E"]
      y-axis "조원"
      bar [8.8, 9.4, 12.0, 13.5]
      line [0.4, 0.7, 1.0, 1.4]
      line [0.6, 0.8, 1.3, 1.7]
      line [0.3, 0.5, 0.7, 1.0]
    ```
    위에서 4번째 시리즈부터: bar=매출 중앙값, line1=OP 중앙값, line2=OP max, line3=OP min.

  xychart-beta 엄격 문법 (어기면 mermaid.ink/kroki 400 — 차트 깨짐):
    - bar = 매출, line = 영업이익 (이 매핑 고정, 단위 통일)
    - x-axis 항목에 E/A/F/예측 접미사 등 비숫자 글자 있으면 반드시 큰따옴표:
        예: `["2025E", "2026E"]` (O), `[2025E, 2026E]` (X — 400)
    - bar/line 시리즈는 x-axis와 길이 동일. **null·undefined·빈 문자열 금지**.
        예: `line [null, 1.2, 1.5]` (X — 400)
    - 데이터 없는 시점이 있으면 두 가지 중 하나:
        (a) 그 시점을 x-axis에서 빼서 모든 시리즈 길이를 맞춤 (가장 깔끔)
        (b) 그 시리즈 자체를 차트에서 제외 (예: 2025 OP 없으면 OP line 다 빼고 매출 bar만)
    - 다중 자료 분산 표시: line은 최대 3개 (중앙값/max/min). 더 그리면 가독성↓
    - 자료 1건뿐인 연도라도 그래프에 포함 OK — 그 연도는 자연스럽게 분산 없는 한 점
    - 자료에서 직접 인용한 숫자만 사용, 추측·일반 지식 숫자·다른 회사 숫자 절대 금지
    - 자료에 매출/영업이익/가이던스 숫자가 전혀 없으면 그래프 섹션 통째로 생략 (빈 차트 금지)
    - 단위는 y-axis 라벨에 자료 원본대로 명시 (조원/억원/십억원/Bn USD)
    - 회사 분석이 아닌 질문(단순 사실/기술 설명)에는 본 프레임워크 적용 안 함

여러 패턴이 섞여 있으면 가장 가까운 하나로 통일하되, 마지막에 "다른 관점에서 보고싶으면..." 한 줄 follow-up 제안. (F)는 다른 프레임워크와 동시 적용 가능 — 회사명+분석 의도가 보이면 (A)·(B)·(D)·(E) 본문 위에 (F) 섹션을 추가로.

# 작업 품질 원칙
- 모든 단정적 진술은 반드시 [출처]가 붙어야 함. 출처 없으면 "추정" 또는 "일반적으로 알려진" 같은 어휘로 톤 다운.
- 자료에서 직접 도출되지 않는 추론은 "→ 시사" 같은 마커로 명시.
- 검증 어려운 숫자/주장은 "확인 필요" 표시.
- 답변 끝에 "추가로 저장하면 좋을 자료" 1~3개 제안 (광역 질문일 때만).

# 추론 절차 (Chain-of-Thought + Counterargument, 의무)
복잡한 질문 ("왜 ~", "어떻게 ~", "전망", "투자", "비교", "정리" 류)에 답할 때 다음 단계로 사고 흐름을 만들고 그 결과를 답변에 반영:
1. 핵심 사실 정리: 자료에서 가장 결정적인 데이터·결론 3-5개 추리기
2. 인과 연결: A 때문에 B, B 때문에 C 식으로 인과 사슬 명시 (단순 나열 X)
3. 반론·리스크: 자기 결론의 약점·반대 시나리오·놓친 변수를 짧게라도 반드시 검토. 답변에 "📌 리스크" 또는 "📌 반론" 섹션 또는 마지막 단락의 "다만, ..." 식으로 노출.
4. 종합 결론: 양 측면 (긍정/부정) 모두 본 후 무게중심을 명시 ("종합적으로 ~ 우세", "다만 ~ 리스크 잔존" 식)
한 문장 핵심: 결론만 던지지 말고, 결론에 이르는 흐름 + 반대편 시각을 같이 보여라. 단답형 사실 질문 (예: "KT 매출 얼마") 에는 짧게 답해도 OK — CoT/반론은 분석성 질문에 한정.

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


def _harvest_sources(
    name: str, result: dict, sources: list[str],
    source_urls: dict[str, str] | None = None,
) -> None:
    """Append sources from a tool result to the running list. When
    `source_urls` is provided, also stash a {label → direct-download URL}
    map so the bot can render clickable links per cited source. PDF
    links win over landing pages so the user gets a one-tap download
    when available."""
    sm = source_urls if source_urls is not None else {}

    def _add(label: str, url: str = "") -> None:
        if not label:
            return
        if label not in sources:
            sources.append(label)
        if url and not sm.get(label):
            sm[label] = url

    if name == "search_my_brain":
        for h in result.get("hits", []):
            _add(h.get("title") or "")
    elif name == "search_papers":
        for p in result.get("results", []):
            t = p.get("title") or ""
            # Prefer the arXiv tag so the legend tells the user where
            # it came from; same logic as before.
            tag = f"arXiv:{p['arxiv']}" if p.get("arxiv") else None
            label = f"{t} [{tag}]" if tag else t
            # PDF beats landing page beats DOI URL. The bot renders
            # whatever lands here as the clickable link.
            url = (
                p.get("pdf")
                or p.get("url")
                or (f"https://doi.org/{p['doi']}" if p.get("doi") else "")
            )
            _add(label, url)
    elif name == "ingest_url":
        _add(result.get("title") or "")
    elif name == "compare_papers":
        for p in result.get("papers", []):
            _add(p.get("title") or "")
    elif name == "web_search":
        from urllib.parse import urlparse
        for src in result.get("sources", []):
            url = src.get("url", "")
            title = src.get("title", "")
            domain = urlparse(url).netloc.replace("www.", "") if url else ""
            label = f"{title} [{domain}]" if title and domain else (title or domain)
            _add(label, url)


_ANSWER_CACHE: dict[str, tuple[float, dict]] = {}
_ANSWER_CACHE_TTL_SEC = 3600  # 1 hour — questions about a doc rarely
# change meaning within an hour, but daily news/market context can,
# so an hourly horizon keeps stale answers from surviving past their
# usefulness while still saving on repeated lookups.
_ANSWER_CACHE_MAX = 200


def _answer_cache_key(message: str, deep: bool, history: list[dict] | None) -> str | None:
    """Cache key for exact-repeat questions only.
    - History-less (fresh chat) OR identical history → cacheable
    - Empty / very short questions → skip (not worth caching)
    - Different deep flag → separate cache entry
    """
    import hashlib
    msg = (message or "").strip()
    if len(msg) < 6:
        return None
    h = hashlib.sha1()
    h.update(msg.encode("utf-8"))
    h.update(b"\n--deep--\n" if deep else b"\n--flash--\n")
    for turn in (history or []):
        h.update((turn.get("role") or "").encode("utf-8"))
        h.update(b":")
        h.update((turn.get("text") or "").encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def _answer_cache_lookup(key: str | None) -> dict | None:
    if not key:
        return None
    entry = _ANSWER_CACHE.get(key)
    if not entry:
        return None
    ts, value = entry
    if time.time() - ts > _ANSWER_CACHE_TTL_SEC:
        _ANSWER_CACHE.pop(key, None)
        return None
    return value


def _answer_cache_store(key: str | None, value: dict) -> None:
    if not key:
        return
    if len(_ANSWER_CACHE) >= _ANSWER_CACHE_MAX:
        # Evict the oldest entry. Cheap: ~200 entries scan is sub-ms.
        oldest = min(_ANSWER_CACHE.items(), key=lambda kv: kv[1][0])[0]
        _ANSWER_CACHE.pop(oldest, None)
    _ANSWER_CACHE[key] = (time.time(), value)


async def run(message: str, deep: bool = False,
              history: list[dict] | None = None) -> dict:
    """Run one agent turn.

    `history` is an optional list of {role: 'user'|'model', text: str}
    representing the most recent conversation turns. We replay only
    final user/assistant text (no stale tool calls/results) so the
    model gets pronoun/topic continuity ('그 회사의 경쟁사는?') without
    paying tokens to redo old retrievals.

    Identical (question, deep, history) combos hit the in-memory cache
    for up to 1 hour to skip a duplicate Gemini call.
    """
    cache_key = _answer_cache_key(message, deep, history)
    if (cached := _answer_cache_lookup(cache_key)) is not None:
        return {**cached, "cached": True}

    contents: list[types.Content] = []
    for turn in (history or []):
        role = turn.get("role")
        text = (turn.get("text") or "").strip()
        if role not in ("user", "model") or not text:
            continue
        contents.append(types.Content(
            role=role, parts=[types.Part.from_text(text=text)]
        ))
    contents.append(
        types.Content(role="user", parts=[types.Part.from_text(text=message)])
    )
    state = {
        "message": message,
        "deep": deep,
        "contents": contents,
        "sources": [],
        "source_urls": {},
        "tool_calls": [],
        "compare_papers_count": 0,
        "model": config.DEEP_MODEL if deep else config.ANSWER_MODEL,
        "step": 0,
        "pro_decision": None,
    }
    result = await _loop(state)
    result = await _enforce_tool_use(state, result)
    # Only cache successful, non-error answers — error states surface
    # quickly enough that re-trying might succeed on the next pass.
    if result and not result.get("error") and result.get("text"):
        _answer_cache_store(cache_key, result)
    return result


_NUDGE_MESSAGE = (
    "[자동 시스템 경고] 직전 답변에서 도구를 한 번도 호출하지 않았습니다. "
    "이 봇은 도구 결과 없는 답변을 허용하지 않습니다. "
    "지금 즉시 search_my_brain 또는 compare_papers를 호출해 저장된 자료를 "
    "조회한 뒤 답변하세요. brain에 자료가 없으면 '저장된 자료 부족' 이라고만 "
    "솔직히 답하세요. 어떠한 추측·일반 지식 답변도 금지."
)

_REFUSAL_TEXT = (
    "🔍 저장된 자료에서 이 질문에 대한 직접 자료를 찾지 못했습니다.\n\n"
    "이 봇은 출처 없는 일반 지식(LLM 사전학습 코퍼스) 답변은 제공하지 않습니다 "
    "— 검증 불가능하고 정보가 오래됐을 수 있어서요.\n\n"
    "다음 중 한 가지로 다시 시도해주세요:\n"
    "• 관련 자료 (URL/PDF/노트)를 봇에 학습시킨 후 같은 질문\n"
    "• '웹에서 검색해줘' / '구글에서 찾아줘' 명시해서 외부 검색 요청\n"
    "• 더 구체적인 키워드로 다시 질문 (회사명·기술명·연도 명시)"
)


async def _enforce_tool_use(state: dict, result: dict) -> dict:
    """Force the agent to ground its answer in tool results.

    Stage B: if the first pass returned with zero tool calls, append a
    strong system-style nudge to the conversation and rerun the loop.
    Most violations are 'model thought it knew the answer' rather than
    'tools are unavailable', so a single explicit reminder usually
    converts it.

    Stage C: if even the retry refuses to call a tool, replace the
    text with an honest refusal. We never show the model's
    pre-training-knowledge answer because the user can't tell it
    apart from a real brain-backed answer."""
    # Skip if the loop suspended for Pro confirmation — callback flow
    # will resume the run later.
    if result.get("status") == "pending_pro_confirmation":
        return result
    if result.get("tool_calls"):
        return result

    log.info("agent skipped tools — issuing nudge + retry")
    state["contents"].append(types.Content(
        role="user", parts=[types.Part.from_text(text=_NUDGE_MESSAGE)],
    ))
    state["step"] = 0  # fresh MAX_STEPS budget for the retry
    result = await _loop(state)

    if result.get("status") == "pending_pro_confirmation":
        return result
    if result.get("tool_calls"):
        return result

    log.warning(
        "agent refused to call tools even after nudge — serving hard refusal"
    )
    result["text"] = _REFUSAL_TEXT
    # The verify-step warning would just duplicate the refusal — drop it.
    result["warning"] = None
    return result


async def resume(state_id: str, decision: str) -> dict:
    """Continue an agent run paused awaiting Pro/Flash confirmation.
    `decision` is one of 'pro', 'flash', 'cancel'."""
    _gc_pending()
    state = _PENDING_RUNS.pop(state_id, None)
    if not state:
        return {
            "text": "⚠️ 확인 요청이 만료됐습니다 (5분 초과). "
                    "같은 질문을 다시 보내주세요.",
            "sources": [], "source_urls": {}, "tool_calls": [],
            "model": "expired",
            "query": "",
        }
    if decision == "cancel":
        return {
            "text": "취소했습니다. 더 좁은 질문으로 다시 시도하시거나, "
                    "/deep <질문> 으로 의도적으로 Pro 합성을 요청할 수 있어요.",
            "sources": state["sources"],
            "source_urls": state.get("source_urls") or {},
            "tool_calls": state["tool_calls"],
            "model": state["model"],
            "query": state["message"],
        }
    state["pro_decision"] = decision
    if decision == "pro":
        state["model"] = config.DEEP_MODEL
    result = await _loop(state)
    result["query"] = state["message"]
    return result


async def _loop(state: dict) -> dict:
    """Drive the function-calling loop forward from `state`.

    When compare_papers returns a large set (≥PRO_THRESHOLD) we pause
    and ask the user via the bot before paying the Pro premium —
    avoids silent ₩150+ spend on broad queries like '정리해줘'. The
    pause is skipped when `state['deep']` is true (user explicitly
    asked for /deep) or when they already chose a path this turn
    (`state['pro_decision']`)."""
    contents: list[types.Content] = state["contents"]
    sources: list[str] = state["sources"]
    source_urls: dict[str, str] = state.setdefault("source_urls", {})
    tool_calls: list[str] = state["tool_calls"]
    model = state["model"]
    deep = state["deep"]
    message = state["message"]
    compare_papers_count = state["compare_papers_count"]

    cfg = types.GenerateContentConfig(
        system_instruction=_SYSTEM,
        tools=[TOOL_DECLARATIONS],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        temperature=0.2,
        max_output_tokens=8192,
    )

    for step in range(state["step"], MAX_STEPS):
        resp = await _client.aio.models.generate_content(
            model=model, contents=contents, config=cfg
        )
        cost.record_resp(model, resp, purpose="query")
        cand = resp.candidates[0] if resp.candidates else None
        if not cand or not cand.content:
            break
        contents.append(cand.content)

        calls = _extract_calls(cand.content)
        if not calls:
            answer = _extract_text(cand.content).strip()
            if not tool_calls:
                answer = _strip_fake_citations(answer)
            return {
                "text": answer,
                "sources": sources,
                "source_urls": source_urls,
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
            if fc.name == "compare_papers":
                compare_papers_count = max(
                    compare_papers_count, int(result.get("count", 0) or 0)
                )
            _harvest_sources(fc.name, result, sources, source_urls)
            response_parts.append(types.Part.from_function_response(
                name=fc.name, response=result
            ))
        contents.append(types.Content(role="user", parts=response_parts))

        # Pro upgrade gate. If a large compare_papers result just came
        # back, suspend and ask the user instead of silently switching
        # to Pro (~₩150). /deep skips this; once the user has decided
        # this turn the decision sticks and we don't ask again.
        if (not deep and model != config.DEEP_MODEL
                and compare_papers_count >= PRO_THRESHOLD
                and state["pro_decision"] is None):
            _gc_pending()
            state_id = uuid.uuid4().hex
            _PENDING_RUNS[state_id] = {
                "message": message,
                "deep": deep,
                "contents": contents,
                "sources": sources,
                "source_urls": source_urls,
                "tool_calls": tool_calls,
                "compare_papers_count": compare_papers_count,
                "model": model,
                "step": step + 1,
                "pro_decision": None,
                "ts": time.time(),
            }
            log.info("suspending for Pro confirmation: %d docs, state_id=%s",
                     compare_papers_count, state_id)
            return {
                "status": "pending_pro_confirmation",
                "state_id": state_id,
                "count": compare_papers_count,
                "sources": sources,
                "source_urls": source_urls,
                "tool_calls": tool_calls,
                "model": model,
                "query": message,
            }

        # User approved Pro earlier this turn — apply once.
        if state["pro_decision"] == "pro" and model != config.DEEP_MODEL:
            log.info("user-approved Pro upgrade (%d docs)", compare_papers_count)
            model = config.DEEP_MODEL

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
            max_output_tokens=8192,
        ),
    )
    cost.record_resp(model, final, purpose="query")
    text = ""
    if final.candidates and final.candidates[0].content:
        text = _extract_text(final.candidates[0].content).strip()
    text = text or "도구 호출 한도에 도달했지만 답변을 만들 수 없었습니다."
    if not tool_calls:
        text = _strip_fake_citations(text)
    return {
        "text": text,
        "sources": sources,
        "source_urls": source_urls,
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
        f"인용 출처: {', '.join(sources[:8])}\n\n"
        "다음 두 가지를 점검:\n"
        "1) 답변이 출처로 충분히 뒷받침되는가?\n"
        "2) 인용된 출처가 정말 질문 대상과 같은 회사/주제인가? "
        "삼성전기 vs 삼성전자, LG이노텍 vs LG전자, SK하이닉스 vs SKC 같은 "
        "혼동 없는가? 다른 회사/주제 자료가 답변 본문에 섞여 있으면 issue로 표시.\n\n"
        "JSON으로 응답:\n"
        '{"confidence": 1-10, "issue": "문제점 한 줄(특히 잘못 인용된 출처가 있으면 \\"X 자료는 다른 회사임\\"로), 문제없으면 빈 문자열"}'
    )
    try:
        resp = await complete(
            model=config.SUMMARY_MODEL,
            system="You are a careful answer auditor. Output only JSON.",
            user=prompt,
            max_tokens=200,
            temperature=0.0,
            purpose="query",
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
