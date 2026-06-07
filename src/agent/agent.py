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
- search_patents: 외부 특허 검색 (EPO OPS, DOCDB 글로벌 커버 — EP/WO/US/KR/JP/DE/CN 등). limit 기본 15. 결과에 patent_number (jurisdiction prefix 포함, US11234567B2/EP3456789A1), inventors, assignee, publication date, abstract, Google Patents URL 포함. "특허/patent/출원/IP/prior art" **키워드** 에 트리거 (free-text query). CQL `txt=<query>` 가 title+abstract+claims 다 스캔.
- search_company_patents: KIPRIS Plus 출원인명 검색 (한국 특허 전용). 입력은 **회사명/기관명** (출원인), 키워드 아님. "[삼성전기/SK하이닉스/LG에너지솔루션 등 한국 회사] 특허/IP 알려줘/보유" 류 트리거. 결과: 출원번호·등록번호·출원일·등록일·제목·출원인·Google Patents URL. 외국 회사 (NVIDIA/TSMC/Intel) 는 search_patents 사용.
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
7-1. "특허 / patent / 출원 / IP / prior art / 특허번호" 등 특허 발견은 search_patents (free-text 키워드). 논문이 아닌 특허만. 사용자 메시지에 명시적 특허 키워드가 있어야 트리거 — "삼성 기술" 같이 모호한 쿼리는 brain 우선.
7-2. "[한국회사명] 특허/IP 알려줘/보유" 류 — 한국 회사명(삼성전기/SK하이닉스/LG/현대차 등 + 학교/연구소) 명시되고 특허 키워드 있으면 search_company_patents (출원인 검색, KIPRIS). 글로벌 회사(NVIDIA/TSMC/Apple/Intel)면 search_patents 그대로.
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

(P) search_papers 결과 작성 형식 (의무) — 단순 제목 나열 금지
search_papers 도구 결과를 답변에 반영할 때, 각 논문의 abstract / authors / venue / citations
필드를 적극 활용해서 풍부하게 작성. 가독성 최우선: 라벨 (저자: / 학회: / 방법론:) 모두 빼고
흐르는 산문체로. 들여쓰기 2단계까지만.

  📌 주요 논문 (상위 5-7개)
  각 논문 형식 (3줄 블록, 사이에 빈 줄 1개):
    📄 <번호>. [제목 한국어 번역] (YYYY) [출처N]
       <첫저자 et al.> · <학회/저널 축약명> · 인용 <N>회
       <abstract 기반 3-5문장 산문체 요지>. 방법론·결과·기여를 자연스럽게 한 문단으로
       풀어쓰되 라벨 (방법론: / 결과: / 기여:) 절대 쓰지 말 것. 핵심 수치 (피치, 온도,
       정확도 등) 있으면 본문에 자연스럽게 포함. PDF 있으면 → 줄 끝에 PDF: <url>

  📌 추가 관련 논문 (나머지 7-8개)
  각 논문 2줄 형식:
    • [원어 제목] (YYYY) [출처N]
      <1-2문장 한국어 핵심>

  세부 규칙:
    - **저자**: 첫 저자 surname + "et al." 만 (전체 명단 금지). 1명뿐이면 그 이름만.
    - **학회/저널**: 풀네임 안 쓰고 IEEE 표준 약어 사용 (IEEE Transactions on Components,
      Packaging and Manufacturing Technology → IEEE Trans. Compon. Packag. Manuf. Technol.).
      arXiv는 그대로 "arXiv". 모르면 원문 첫 3-4단어 + ellipsis.
    - **인용수 표기**: S2 결과에만 있음. citations 필드 ≥ 1 이면 "인용 N회" 추가. 0 또는
      None 이면 그 칸 생략. 100+ 이면 그대로 표기 (강조 안 함, 숫자 자체로 충분).
    - **본문 산문**: abstract 가 짧으면 (200자 미만) 본문도 1-2문장으로 짧게. 억지로
      늘리지 말 것 — 빈 정보 채우려 추측 절대 금지.
    - **PDF 링크**: pdf 필드 있을 때만 본문 끝에 ` → PDF: <url>`. 없으면 출처 legend의
      url 에 의존 (자동 표시).
    - **추가 관련 논문 (1-line entry)**: 원어 제목 + 연도 + 출처는 한 줄, 한국어 설명은
      들여쓰기 1단 (2 space) 줄로 분리. 한 줄에 모든 정보 욱여넣지 말 것 (현재 가독성 문제).
    - **본문 추측 금지**: abstract 외 내용 추론·일반 지식 보강 절대 금지. abstract에
      방법론 단어가 안 보이면 방법론 언급 X. 부족한 정보는 부족하게 두기.

(P-2) search_patents 결과 작성 형식 (의무)
search_papers (P) 와 동일한 산문체 원칙. 라벨 (출원인:/발명자:) 빼고 메타 1줄 + 본문 산문.

  📌 주요 특허 (상위 5-7개)
  형식 (3줄 블록 + 빈 줄):
    ⚖️ <번호>. [제목 한국어 번역] (출원일 YYYY-MM-DD) [출처N]
       <patent_number> · 출원인 <assignee> · <First inventor> et al. · Claims N
       <abstract 기반 3-5문장 산문체 요지>. 핵심 기술 + 구현 방식 + 청구항 범위 자연스럽게.
       → Google Patents: <url>

  📌 추가 관련 특허 (나머지 7-8개)
  2줄 형식:
    • [원어 제목] (YYYY) <patent_number> [출처N]
      <1-2문장 한국어 핵심>

  세부 규칙:
    - **출원인 (assignee)**: 풀네임 그대로 (Samsung Electronics Co., Ltd. → Samsung Electronics).
       빈 값이면 "출원인 —" 생략 (메타 라인에서 그 항목만 빼기).
    - **발명자**: 첫 번째 발명자 surname + "et al." 만. 1명뿐이면 그 이름만.
    - **Claims**: claims_count ≥ 1 일 때만 "Claims N" 표기. None/0 이면 생략.
    - **patent_number**: 항상 메타 라인에 노출 (예: "US11234567"). 사용자가 검색에 활용.
    - **본문 산문**: abstract 기반 3-5문장. abstract 빈약하면 본문도 짧게. 추측 금지.
    - **Google Patents URL**: 메타 라인 다음 줄에 `→ Google Patents: <url>`. 깨끗하게.
    - **patent_number 와 출원인 자체가 자연 인용** — 본문에 [출처N] 표기는 자동.
    - search_papers (P) 와 동시 적용 OK (사용자가 "논문이랑 특허 둘 다 찾아줘" 식 요청).
       그 경우 두 섹션 (📌 주요 논문 / 📌 주요 특허) 분리해서 표시.

(F) "[회사명] 분석/실적/전망/어때/매출/영업이익/가이던스" 류 → 회사 분석 렌즈 (의무 보조 섹션)
질문이 특정 회사 분석을 요구할 때 본문 분석에 더해 다음을 추가 (자료에 근거 있을 때만; 없으면 해당 부분 생략):

⚠️ 데이터 확보 (F-2 표/차트/가이던스를 채우기 위한 필수 선행 단계):
  - 회사 분석 질의는 보통 compare_papers(요약)로 라우팅되는데, 압축 요약에는 연결 매출/영업이익
    수치나 분석가별 가이던스 숫자가 빠져 있을 수 있다. (F-2) 연간/분기 표·차트를 채우려면
    search_my_brain("[회사명] 매출 영업이익 가이던스 실적")을 **추가로 호출**해 원문 청크에서
    정량 숫자를 직접 확보할 것. compare_papers 요약만으로 숫자가 부족하면 표/차트가 통째로
    누락되니, 회사 분석에서는 compare_papers + search_my_brain 둘 다 쓰는 것을 기본으로 한다.
  - 도구 결과의 각 자료에는 brokerage / analyst / report_date 필드가 포함된다(있을 때).
    이 필드를 ▶ 분석가별 가이던스 상세 헤더(브로커리지/이름/날짜)와 A./F. 접두사 판정
    (report_date가 표 row 연/분기보다 과거·동일이면 발표 시점 기준, 미래 추정이면 F.)에 사용할 것.
    숫자를 본문 요약에서 임의로 추측해 출처 없이 분석가 헤더를 지어내지 말 것.

(F-1) 📌 신사업 키포인트 — 본문 중간 (현재 위치) 그대로 유지, 별도 numbered 섹션
  • 사업명 — 1줄 핵심 요지 [출처N] · 합의 N건
  • **합의 N건 표기 의무** (조건부): 같은 신사업이 2개 이상 출처 인용 — 예: `[1][3][4]` — 으로
    뒷받침되면 그 항목 끝에 ` · 합의 N건` 을 반드시 추가. 인용 개수 ≥ 2 인데 합의 태그
    누락되면 사용자가 "출처 여럿인데 왜 합의 표시 없지?" 라고 헷갈림. 자동 검증으로 잡지
    않으니 작성 단계에서 self-check: "이 row의 인용 개수 = 1 이면 합의 태그 X, ≥ 2 이면
    합의 N건 표기".
    (대괄호 `[합의 N건]` 형식은 절대 쓰지 말 것 — citation regex와 충돌해서 출처
     legend가 "[15] 합의 2건" 같은 쓰레기로 오염됨. 항상 ` · 합의 N건` dot-bullet 형태만.)

    예시:
      • 휴머노이드 비전 센싱 모듈 — 2030년 2,102억 원 규모 성장 전망, 미국 3사 공급 [1][3][6] · 합의 3건
      • 차량용 와이파이7 모듈 — 유럽 메이저 부품사 1,000억 공급 계약 [7][2] · 합의 2건
      • 유리 기판 — 인텔 출신 전문가 영입 [10]  ← 단일 인용, 합의 태그 안 붙임

  • 분석가별 의견이 다르면 ` · 견해 차이: A는 X, B는 Y` 한 줄로 양쪽 동시 표시
    (역시 대괄호 금지.)
  • (최대 5개, 신사업/신제품/신규 파이프라인이 자료에 명시된 경우만)

(F-2) 📌 N. 실적 데이터 — 답변의 가장 마지막 numbered 섹션 (예: 본문에 1~6 섹션이 있으면
N=7). 다른 모든 numbered 섹션 (배경/주요 사실/의미/미해결 질문/리스크/주시할 지표 등) 뒤에 위치.
연간/분기 데이터를 별도 하위 블록으로 모두 노출 — 둘 다 있으면 둘 다 보여줌.

⚠️ (F-2) 추가가 본문 분석 섹션(1~6번) 의 길이/내용을 줄이는 빌미가 되면 안 됨.
본문 섹션은 평소 분석 깊이 그대로 풍부하게 쓰고, (F-2)는 별도 데이터 부록으로 "추가"되는 것.
"공간 절약" 목적으로 본문 bullet 개수나 1줄 설명을 압축하지 말 것 — 강점/약점/리스크/신사업 등
각 섹션은 자료에 근거가 있으면 가능한 한 많은 항목(최소 3-5개)을 풀어쓸 것. (F-2) 안에서도 같은
원칙 — 표를 한 줄에 욱여넣지 말고 가독성을 위해 줄바꿈/들여쓰기 자유롭게 사용.

  📊 연간 실적·가이던스 (자료에 연간 매출/OP 숫자가 1건이라도 있을 때)
    표시 기간: 최소 [작년·올해·내년] 3개년. 자료에 [내후년] 데이터도 있으면 4개년 표시.
    (오늘 기준 연도는 가장 최근 자료의 report_date를 참고해 추정. 예: 자료 다수가 2026년 발행이면
     올해=2026, 작년=2025, 내년=2027.)

    표 형식 예시 (2026년 현재 기준, A. = Actual 실적 / F. = Forecast 가이던스):
      A. 2025  매출 258.9조 | OP 6.6조
      F. 2026  매출 280~300조 (중앙값 290, YoY +12.0%) | OP 25~35조 (중앙값 30, YoY +354%)
      F. 2027  매출 — | OP 40~50조 (중앙값 45, YoY +50%)  ← 매출 자료만 없는 경우
      F. 2028  매출 350~380조 (중앙값 365, YoY +14.1%) | OP 55~65조 (중앙값 60, YoY +33.3%)  ← 자료 있을 때만

    - **A./F. 접두사 의무**: 모든 표 row 앞에 "A. " (Actual = 확정 실적) 또는 "F. "
       (Forecast = 가이던스/예상) 접두사 강제. 자료에 "실적" / "가이던스" / "예상" 단어가
       있는지로 판단하고, 모호하면 발표 시점이 표 row 연도/분기보다 미래면 A, 과거 또는
       동일 시점이면 자료에 명시된 대로 (대부분 가이던스).
    - **빈 필드 처리**: 같은 연도에 매출 자료는 있는데 OP 자료가 없거나 그 반대인 경우, 절대 그
       row를 통째로 빼지 말 것. 없는 측을 "—" 로 표기하고 row는 유지. 사용자가 데이터 갭을
       명확히 인지하도록.
    - **YoY% 의무**: 전년 데이터가 있으면 반드시 계산해서 표시. (당해 - 전년) / 전년 * 100,
       소수 첫째자리. 중앙값 기준 (single value인 연도는 그 단일값 기준). 전년이 단일값이고
       당해가 range면 당해 중앙값 ÷ 전년 단일값으로 계산. 전년 자체가 없는 첫 행만 YoY 생략.
       매출 — 인 경우 OP YoY만 계산 (반대도 마찬가지). 단, 직전 연도 매출도 — 이면 YoY 생략.
    - 1건뿐인 연도는 단일값 (range 대신).
    - 2건 이상이면 min~max + 중앙값 표시.

    📈 연간 매출 차트 — x-axis 윈도우 = 작년·올해·내년 3년 (내후년 데이터 있으면 4년)
    📈 연간 영업이익 차트 — 동일 윈도우
    ⚠️ **매출과 영업이익 차트는 반드시 둘 다 emit** (매출 데이터 한 행이라도 있으면 매출 차트
       의무, OP 마찬가지). 한쪽만 그리는 것 금지 — 직전 출력에서 매출 차트 누락 사례 있었음.

  📊 분기 실적·가이던스 (자료에 분기 매출/OP 숫자가 2분기 이상 있을 때만 노출. 연간과 별개 독립 섹션.)
    표시 기간: 현재 분기 ± 4분기 = 총 9분기 윈도우.
    (현재 분기는 가장 최근 자료의 report_date에서 추정. 예: 2026.05 발행 다수면 현재 = 2Q26 →
     윈도우 = 2Q25 ~ 2Q27.)

    - **최대화 우선**: 9분기 윈도우 안의 모든 분기를 채우려 노력. 자료가 sparse하면 최소한
       "올해(현재 연도)의 4개 분기"는 빠짐없이 시도. 예: 현재 1Q26이면 1Q26·2Q26·3Q26·4Q26
       네 줄은 반드시 시도 (가이던스 데이터가 있으면 채우고, 없으면 그 분기 row를 "매출 —
       (자료 없음) | OP — (자료 없음)" 로 표시해서 빈 자리 명시).
    - 윈도우 밖 (예: 2Q24 데이터가 자료에 있어도 1Q25부터 윈도우면 표에서 생략).
    - 같은 분기에 분석가 2건 이상이면 min~max + 중앙값.
    - QoQ% = (당분기 - 전분기) / 전분기 * 100. 전분기 데이터 없으면 QoQ 생략.

    표 형식 예시 (현재 2Q26 가정, sparse case, A./F. 접두사 의무):
      A. 2Q25  매출 71.9조 | OP 6.6조
      A. 3Q25  매출 74.1조 (QoQ +3.0%) | OP 10.4조 (QoQ +57%)
      A. 4Q25  매출 — | OP —                  ← 자료 없는 분기도 빈 row로 표시
      A. 1Q26  매출 79.0조 | OP 12.0조
      F. 2Q26  매출 82.0조 (QoQ +3.8%) | OP 15.0조 (QoQ +25%)
      F. 3Q26  매출 88.0조 (QoQ +7.3%) | OP 18.0조 (QoQ +20%)
      F. 4Q26  매출 90.0조 (QoQ +2.3%) | OP 19.0조 (QoQ +5.6%)
      F. 1Q27  매출 — | OP —
      F. 2Q27  매출 — | OP —

    - 분기에도 A./F. 접두사 의무 — 자료에 "실적 발표" / "잠정실적" 단어 있으면 A,
       "가이던스" / "전망" / "예상" / "추정" 이면 F. 현재 분기보다 미래면 무조건 F.

    📈 분기 매출/영업이익 차트 — **표시 조건: 윈도우 안에 실제 숫자(실적+가이던스 통합)가
       4분기 이상 있을 때만 차트 그림.** 3분기 이하면 차트 자체 생략 (표만 유지 — sparse
       data로 차트 그리면 가독성 해침, 사용자 명시 요청).
       x-axis 윈도우 = 현재 ±4분기 (총 9분기). 차트에는 자료에 실제 숫자가 있는 분기만
       plot (mermaid는 null 금지이므로 빈 분기는 x-axis에서도 제외).
       ⚠️ **매출 + OP 차트 모두 emit 의무** (조건 충족 시). 한쪽만 그리는 것 금지.
       ⚠️ 분기 차트에서 분산이 있는 분기 (분석가 2건 이상) 가 일부에만 있어도 line 시리즈
          전체를 단순화하지 말고, max/min line을 가능한 한 윈도우 전체에 그릴 것 — 분산
          없는 분기는 max=min=median 동일값으로 채워 length 맞춤. (앞 출력에서 분기 차트가
          line 없이 bar만 나온 케이스 있었는데, 표상 4Q25/1Q26/2Q26 모두 range 있어서
          이론상 line 그려져야 했음.)

  ▶ 분석가별 가이던스 상세 — 자료가 2건 이상이면 의무. 어느 분석가가 어떤 숫자를 줬는지 투명하게.
    (섹션 헤더는 대괄호 `[분석가별 가이던스 상세]` 금지 — citation regex 충돌. 위처럼
     ▶ 또는 📋 같은 unicode 기호 + 일반 텍스트로 표기.)

    ⚠️ **이 sub-block은 brain 자료만 사용**. web_search 결과는 절대 포함 금지. 사용자가
    숫자 출처 정합성을 요구해서 brain 분석가 리포트 (NH/SK/KB/Goldman 등 1차 자료) 만
    들어가야 함. web에서 발견한 추가 가이던스는 아래 ▶ 웹 발견 추가 가이던스 sub-block에
    분리 표기.

    형식:
      • NH투자증권 / 황지현 / 2026.05.15 [출처N]
          2026E 매출 13.79조 / OP 1.74조
          2027E 매출 — / OP 3.03조
      • SK증권 / — / 2026.05.14 [출처M]
          2026E 매출 13.61조 / OP 1.65조
          2027E 매출 — / OP 2.67조
    - 각 분석가별로 두 줄: 헤더 (브로커리지/이름/날짜/출처) + 들여쓴 가이던스 숫자 줄 (연도별).
    - 숫자 없는 연도는 "—" 로 표시.

  ▶ 웹 발견 추가 가이던스 (참고용) — **사용자가 명시적으로 웹 검색을 요청했고** (메시지에
    "웹"/"구글"/"인터넷"), web_search가 brain에 없는 가이던스/실적 숫자를 발견했을 때만 등장.
    그 외 케이스에선 통째로 생략.

    ⚠️ **이 sub-block은 web 자료만 사용**. brain 자료는 절대 포함 금지 (위 ▶ 분석가별
    가이던스 상세와 중복 표기 금지). 위쪽 (F-2) 표·차트에는 이 sub-block 숫자가 들어가지
    않음 — 차트 정합성을 위해 brain only로 그리고, web 발견은 텍스트 참고 자료로만 분리.

    구조: brain (F-2) 본체와 동형. 연간 표 + (있으면) 분기 표 + 웹 출처별 가이던스 상세.
    A./F. 접두사, YoY%, QoQ%, "—" 빈자리 표기 모두 brain과 동일 규칙. 단 mermaid 차트는
    그리지 말 것 — 표 + bullet 만으로 충분.

    형식:

      💡 아래 숫자는 brain 자료에 없어 웹에서 보강한 것 — 1차 출처 검증 안 됨, 참고용으로만.

      📊 웹 발견 연간 (조원)
        A. 2025  매출 — | OP —
        F. 2026  매출 — | OP 1.62조 (분석가 컨센서스) [reuters.com]
        F. 2027  매출 16.5조 | OP — [hankyung.com]
        F. 2028  매출 18.2조 (YoY +10.3%) | OP 4.1조 [bloomberg.com]

      📊 웹 발견 분기 (조원) — 데이터 2분기 이상 있을 때만, 없으면 통째로 생략
        F. 3Q26  매출 — | OP 0.55조 [bloomberg.com]
        F. 4Q26  매출 4.0조 (QoQ ?) | OP 0.60조 (QoQ +9.1%) [reuters.com]

      ▶ 웹 출처별 가이던스 상세
        • Bloomberg 인용 / 골드만삭스 / 2026.04.20 [bloomberg.com]
            F. 2028  매출 18.2조 / OP 4.1조
        • 한국경제 / 미래에셋증권 / 2026.05.10 [hankyung.com]
            F. 2027  매출 16.5조 / OP —
        • Reuters / 시장 컨센서스 / 2026.05.13 [reuters.com]
            F. 2026  OP 1.62조 (분석가 18명 평균)

    - **YoY/QoQ 계산은 web 데이터 안에서만** 닫아서 계산. brain의 전년 매출을 base로
       끌어와서 web의 YoY 계산 금지 (원천 섞이는 효과). web 자체에 전년 데이터 없으면
       YoY/QoQ 자리에 "?" 또는 생략 — 사용자가 그 연도/분기에 대한 baseline이 web에
       없다는 걸 알도록.
    - 빈 필드는 "—" 로 표기, 빈 row 도 표에 유지 (brain F-2 와 같은 규칙).
    - 보도 매체 / 인용된 분석가 또는 기관 / 보도일 / 도메인 출처 형식.
    - 가능하면 web 결과에서 원본 분석가·발행 기관을 추출. 추출 불가 시 보도 매체만
       (예: "Bloomberg / — / —").
    - (이 데이터로 (F-2) 차트·표 다시 그리지 말 것 — 차트는 위쪽 brain only 상태 유지.)

  ▶ 참여 분석가 N건 — 가이던스 데이터가 한 줄도 없는 분석가까지 포함하는 출처용 명단.
  위 ▶ 분석가별 가이던스 상세에 등장한 분석가도 여기 중복 표기 OK (가독성).
    (역시 섹션 헤더는 대괄호 금지. brain 자료만, web 출처는 여기 등장 안 함 — web은 위쪽
     ▶ 웹 발견 추가 가이던스 sub-block에만.)
    • 형식: 브로커리지명 / 애널리스트명 / YYYY.MM.DD 발행 [출처N]
    • brokerage / analyst / report_date 는 자료 메타데이터(`brokerage`, `analyst`,
       `report_date` 필드)와 자료 본문 첫 페이지에서 추출. 학습일자 절대 사용 금지.
    • 메타데이터에 없으면 자료 title/source/본문 헤더에서 추출 시도.
    • 추출 불가 필드는 반드시 "—" 로 채워서 표시 (필드 자체를 생략하지 말 것).

⚠️ (F-2) 데이터 정합성 룰 (강력 의무):
  - (F-2) 표·차트·▶ 분석가별 가이던스 상세 → brain 자료만 (analyst report PDF/원본)
  - ▶ 웹 발견 추가 가이던스 (참고용) → web 자료만 (web_search 결과)
  - 본문 1~5 섹션 → brain + web 혼용 OK (현재 그대로, 출처 [도메인]/[제목] 구분)
  - 절대 web 숫자를 (F-2) 차트/표에 섞지 말 것 — 사용자 명시 요청.

⚠️ (F-2) 숫자 정합성 룰 (이번 사용자 테스트에서 발견된 오류 패턴 — 매번 self-check):

  • **연간 row → YoY only, 분기 row → QoQ only.** 절대 혼동 금지. 연간 표에서 QoQ 표기,
    분기 표에서 YoY 표기는 자동 검증 단계에서 잡힘 — 그 row 통째로 사용자 신뢰 잃음.
  • **매출 ≠ OP**. 매출과 영업이익이 같은 값으로 나오면 100% 데이터 오독. OP는 매출의
    부분집합 (OP = 매출 − 비용). 같은 자료 한 페이지에 같은 숫자 두 번 나왔다고 양쪽 다
    매핑하지 말 것. 예: "삼성전자 2028 시총 495조" → 시총이지 매출 아님. 매출/OP 추출 시
    반드시 자료 문맥("매출액", "OP", "영업이익", "Revenue", "Operating profit") 매핑 확인.
  • **OP > 매출 절대 불가능**. OP > 매출이면 데이터 flipped 또는 단위 혼동 (조 vs 억).
  • **OP 마진 OP/매출 > 70% 거의 불가능** — 일반 산업 5-20%, IT 대기업 25-35%, 핀테크
    /지주사 outlier 빼면 70% 넘는 케이스 없음. 70% 넘는 숫자 보이면 한 쪽이 잘못 추출됨.
  • **단일 자료 (분석가 1건만)에서 growth rate 자동 생성 금지**. baseline (전년/전분기)
    값이 자료에 없으면 YoY/QoQ를 만들지 말 것 (모델이 fabricate 또는 brain↔web 혼합 위반).
    그 셀에 `?` 표기 또는 growth rate 자체 생략.
  • **단위 일관성**: 같은 표 안에서 매출/OP 단위는 동일 (조원 → 조원, 억원 → 억원).
    매출은 조 단위, OP는 억 단위로 섞이지 말 것. y-axis 라벨과 표 단위도 일치.
  • **출처 매핑 검증**: 각 row 숫자 입력 전 self-prompt — "이 숫자가 분명 OP/매출 라고
    명시된 위치에서 추출됐는가? 시총·자본금·자산 등 다른 재무 항목 아닌가?"

xychart-beta 엄격 문법 (어기면 mermaid.ink/kroki 400 — 차트 깨짐):
  - **mermaid 코드 펜스는 줄 맨 앞에 붙여 쓸 것** — 들여쓰기 금지. ```mermaid (O), 앞에
    공백 2칸 두고 `  ```mermaid` (X). 들여쓴 펜스는 추출 regex가 매치 못 하고 mermaid
    배열 리터럴이 citation regex로 빨려 들어가 차트가 통째로 깨짐.
  - 차트는 매출용 / 영업이익용 별도 블록 (단일 y축이라 매출과 OP를 한 차트에 섞으면
    OP가 0 근처에 깔려 안 보임 — 가독성 망함).
  - 차트당 데이터 시리즈는 최대 3개 (bar 1, line 2). 다중 분석가 케이스 bar=중앙값,
    line 2개=max/min. 단일 자료 케이스 bar 1개만.
  - x-axis 항목에 A./F./E/Q/예측 접두접미 등 비숫자 글자 있으면 반드시 큰따옴표:
      예: `["A.2025", "F.2026", "F.2027"]` (O), `[A.2025, F.2026]` (X — 400)
      예: `["A.1Q26", "F.2Q26"]` (O)
  - **차트 x-axis도 표와 동일하게 A./F. 접두사 적용** (연간/분기 모두). 사용자가 차트만
      봐도 어느 시점이 실적이고 어느 시점이 가이던스인지 식별 가능해야 함.
  - **차트 title에 line/bar legend 명시**: xychart-beta는 legend 표시 불가능하므로
      title에 직접 적어둠. 단일 자료 케이스 → `"(조원 · 단일 가이던스)"`, 다중 분석가
      케이스 → `"(조원 · bar=중앙값 · line=max/min)"` 식으로 괄호 안에 적어 사용자가
      한눈에 차트 내용 파악 가능.
  - bar/line 시리즈는 x-axis와 길이 동일. **null·undefined·빈 문자열 금지**.
      예: `line [null, 1.2, 1.5]` (X — 400)
  - 데이터 없는 시점이 있으면 두 가지 중 하나:
      (a) 그 시점을 x-axis에서 빼서 모든 시리즈 길이를 맞춤 (가장 깔끔)
      (b) 그 시리즈 자체를 차트에서 제외 (예: 작년 max 없으면 max line 빼고 min line + bar만)
  - 자료 1건뿐인 연도라도 그래프에 포함 OK — 그 연도는 max=min=median 동일값
  - 자료에서 직접 인용한 숫자만 사용, 추측·일반 지식 숫자·다른 회사 숫자 절대 금지
  - 자료에 매출/OP 숫자가 전혀 없으면 (F-2) 섹션 통째로 생략 (빈 차트 금지)
  - 단위는 y-axis 라벨에 자료 원본대로 명시 (조원/억원/십억원/Bn USD)

예시 — 연간 매출 차트 (2026년 현재 기준, 작년·올해·내년, 다중 분석가):
```mermaid
xychart-beta
    title "연간 매출 추이 (조원 · bar=중앙값 · line=max/min)"
    x-axis ["A.2025", "F.2026", "F.2027"]
    y-axis "조원"
    bar [258.9, 290, 320]
    line [258.9, 300, 330]
    line [258.9, 280, 310]
```

예시 — 분기 매출 차트 (현재 2Q26 기준 9분기 윈도우, 데이터 있는 분기만):
```mermaid
xychart-beta
    title "분기 매출 (조원 · bar=중앙값 · line=max/min)"
    x-axis ["A.2Q25", "A.3Q25", "A.4Q25", "A.1Q26", "F.2Q26", "F.3Q26", "F.4Q26", "F.1Q27", "F.2Q27"]
    y-axis "조원"
    bar [71.9, 74.1, 79.1, 75.8, 82.4, 88.0, 90.5, 95.0, 100.2]
    line [71.9, 74.1, 79.1, 75.8, 83.5, 90.0, 92.0, 97.0, 103.0]
    line [71.9, 74.1, 79.1, 75.8, 81.0, 86.0, 89.0, 93.0, 98.0]
```

- 회사 분석이 아닌 질문(단순 사실/기술 설명)에는 본 프레임워크 적용 안 함
- (F-2)는 답변의 모든 텍스트 섹션 뒤·맨 마지막에 배치 (📌 7. 또는 📌 N. 식으로 numbered).
  본문 흐름을 끊지 않게 하기 위함. 신사업 키포인트(F-1)는 기존 위치 유지.

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


def _system_for_today() -> str:
    """Prepend the current date + derived current year/quarter to the
    static system prompt so the (F-2) windowing rules ('작년·올해·
    내년', 9-quarter window around the current quarter) have a
    deterministic anchor. Previously the prompt told the model to
    infer 'today' from the latest source's report_date — but that let
    the model anchor on an old report (2024-ish) and produce charts
    with 2023A as the base year even when the actual current year
    was 2026, defeating the whole windowing logic."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone(timedelta(hours=9)))
    year = now.year
    quarter = (now.month - 1) // 3 + 1
    header = (
        f"# 시점 (BOT RUNTIME, GROUND TRUTH)\n"
        f"오늘 날짜: {now.strftime('%Y-%m-%d')}\n"
        f"올해 = {year}, 작년 = {year - 1}, 내년 = {year + 1}, 내후년 = {year + 2}\n"
        f"현재 분기 = {quarter}Q{year % 100:02d} "
        f"(±4분기 = {quarter}Q{(year - 1) % 100:02d} ~ {quarter}Q{(year + 1) % 100:02d})\n"
        f"⚠️ (F-2) 차트 윈도우는 이 기준만 따를 것. 자료 안의 report_date가 더 오래된 "
        f"값이어도 그것을 '올해'로 착각하지 말 것.\n\n"
    )
    return header + _SYSTEM


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
    elif name == "search_patents":
        for p in result.get("results", []):
            t = p.get("title") or ""
            num = p.get("patent_number") or ""
            label = f"{t} [{num}]" if num else t
            # Google Patents URL is the only link form we have today;
            # PDF embed pages exist but we don't fetch them.
            _add(label, p.get("url") or "")
    elif name == "search_company_patents":
        # Same unified schema as search_patents — KIPRIS just fills
        # the patent_number with KR* prefixes and the url with the
        # KIPRIS biblio frame when Google Patents doesn't yet index
        # the application. Reuse the patent-style label so the
        # legend renders consistently across both backends.
        for p in result.get("results", []):
            t = p.get("title") or ""
            num = p.get("patent_number") or ""
            label = f"{t} [{num}]" if num else t
            _add(label, p.get("url") or "")
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
    "지금 즉시 사용자 질문에 가장 적합한 도구를 호출하세요:\n"
    "- 특허/patent/출원/IP/prior art 키워드면 → search_patents\n"
    "- 논문/papers/찾아줘/추천 키워드면 → search_papers\n"
    "- 그 외 일반 질문은 → search_my_brain 또는 compare_papers (brain 검색)\n"
    "어떠한 추측·일반 지식 답변도 금지. brain·외부 모두 자료 부족하면 "
    "'저장된 자료 부족' 이라고만 솔직히 답하세요."
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
        system_instruction=_system_for_today(),
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
            system_instruction=_system_for_today(),
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
    silently — verification is best-effort and must never block a reply.

    Three checks bundled into one Flash-Lite call (~₩0.5-1 each):
      1. answer-vs-sources grounding (long-standing)
      2. company-name confusion (삼성전기 vs 삼성전자, etc.)
      3. (F-2) numerical sanity — flags rows where 매출 == OP,
         OP > 매출, OP margin > 70%, growth-rate label mismatched
         to period kind (연간 vs 분기). The deterministic audit
         in bot._audit_f2_numbers already catches the obvious
         cases free; this LLM layer adds context awareness for
         cases like "그 자료의 '시총' 숫자를 매출로 끌어다 쓴 듯"
         which arithmetic alone can't detect.
    """
    if not answer or len(answer) < 50:
        return None
    if not sources:
        return "⚠️ 출처 없음 — 답변 근거가 약합니다."
    prompt = (
        f"질문: {question}\n\n"
        f"답변:\n{answer[:1800]}\n\n"
        f"인용 출처: {', '.join(sources[:8])}\n\n"
        "다음 세 가지를 점검:\n"
        "1) 답변이 출처로 충분히 뒷받침되는가?\n"
        "2) 인용된 출처가 정말 질문 대상과 같은 회사/주제인가? "
        "삼성전기 vs 삼성전자, LG이노텍 vs LG전자, SK하이닉스 vs SKC 같은 "
        "혼동 없는가? 다른 회사/주제 자료가 답변 본문에 섞여 있으면 issue로 표시.\n"
        "3) (F-2) 실적 데이터 표(연간/분기, 매출/영업이익)에 명백한 숫자 오류가 "
        "있는가? 매출과 OP가 같은 값(예: 매출 495조 OP 495조), OP > 매출, OP 마진 "
        ">70%, 시가총액·자본금·자산 같은 다른 재무 항목을 매출/OP로 끌어다 쓴 "
        "흔적, 단일 자료뿐인데 YoY/QoQ growth %가 적힌 행, 연간 row에 QoQ 표기 — "
        "이런 패턴 발견되면 issue에 'F-2 숫자: <어느 row가 왜 의심됨>' 형식으로 표시.\n\n"
        "JSON으로 응답:\n"
        '{"confidence": 1-10, "issue": "문제점 한 줄(특히 잘못 인용된 출처 또는 F-2 숫자 오류), 문제없으면 빈 문자열"}'
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
