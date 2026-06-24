# 기술 네트워크 KG (초안) — 하이브리드 본딩

> 논문 §4.5(기술 네트워크) / §6 통합 논의의 **그림 초안**. 자료①②의 **검증된 사실만**으로
> 결정론적으로 구성(LLM 추출 아님 → 환각 없음). 산출물: `tech_network.png`(렌더),
> `tech_network.json`(트리플), `tech_network.dot`(GraphViz). 아래는 GitHub에서 바로 보이는 Mermaid.

```mermaid
graph LR
  HB["하이브리드 본딩"]:::core
  TSV["TSV"]:::proc; CMP["CMP"]:::proc; DIC["다이싱"]:::proc
  C21["H01L21(전공정)"]:::cpc; C24["H01L24(접합)"]:::cpc; C25["H01L25(적층)"]:::cpc; OPT["G02B(광학)"]:::cpc
  CHIP["칩렛"]:::struct; BSP["BSPDN"]:::struct; CPO["CPO"]:::struct
  HBM["HBM"]:::prod; AI["AI 가속기"]:::prod; CPU["CPU"]:::prod; PWR["SiC/GaN"]:::prod; MLED["MicroLED"]:::prod
  ADE["Adeia"]:::firm; TSMC["TSMC"]:::firm; YMTC["YMTC"]:::firm; SS["Samsung"]:::firm; INT["Intel"]:::firm
  US["미국"]:::juris; CN["중국"]:::juris; JP["일본"]:::juris; TW["대만"]:::juris; KR["한국"]:::juris

  HB -->|requires| TSV
  HB -->|requires| CMP
  HB -->|requires| DIC
  HB -->|realized_in| C24
  HB -->|reaches_into| C21
  HB -->|enables| CHIP
  HB -->|enables| BSP
  HB -->|enables| CPO
  HB -->|applies_to| HBM
  HB -->|applies_to| AI
  HB -->|applies_to| CPU
  HB -->|applies_to| PWR
  HB -->|applies_to| MLED
  BSP -->|extends_to| C21
  CPO -->|couples| OPT
  ADE -->|strategy| C24
  TSMC -->|SoIC·전공정내재화| C21
  TSMC -->|focuses| CMP
  YMTC -->|X-Stacking| C21
  SS -->|focuses| C25
  INT -->|focuses| OPT
  TSMC -->|files_at| TW
  YMTC -->|files_at| CN
  SS -->|files_at| KR
  ADE -->|files_at| US
  CN -.->|process-only↑ RRR 2.69| C21
  JP -.->|최광폭·process-only 거의 없음| C24

  classDef core fill:#e41a1c33,stroke:#e41a1c;
  classDef proc fill:#ff7f0033,stroke:#ff7f00;
  classDef cpc fill:#984ea333,stroke:#984ea3;
  classDef struct fill:#377eb833,stroke:#377eb8;
  classDef prod fill:#4daf4a33,stroke:#4daf4a;
  classDef firm fill:#a6562833,stroke:#a65628;
  classDef juris fill:#99999933,stroke:#999999;
```

**범례(노드 유형):** 🔴핵심공정 · 🟠동행공정(TSV/CMP/다이싱) · 🟣CPC · 🔵구조 · 🟢제품 · 🟤기업 · ⚪관할권

**읽는 법:** 하이브리드 본딩(후공정 H01L24)이 TSV·CMP·다이싱을 *요구*하고 전공정(H01L21)으로
*침투*하며(경계 흐려짐), 칩렛·BSPDN·CPO 구조를 거쳐 HBM·AI가속기 등 제품으로 확장된다.
기업·관할권 전략(Adeia=IP원천, TSMC=전공정내재화, YMTC=X-Stacking, 중국=process-only↑,
일본=최광폭)이 CPC 축에 매핑된다. → §4.8 기업사례·§6.3 교차표·표 6-2와 정합.

---

## kg-gen으로 의미 기반 KG도 뽑으려면 (LLM 키 필요)

위 그림은 *검증된 구조 KG*다. 추가로 **텍스트에서 자동 추출한 의미 KG**가 필요하면 키가 있을 때:

```bash
pip install kg-gen
export OPENAI_API_KEY=...        # LiteLLM 형식
python3 - <<'PY'
from kg_gen import KGGen
kg = KGGen(model="openai/gpt-4o-mini")
text = open("manuscript/sources/02_patent_analytics_termpaper_KO.txt").read()
graph = kg.generate(input_data=text)
import json; json.dump(graph.__dict__, open("rag/kg_output/auto_kg.json","w"), ensure_ascii=False, indent=1)
PY
```
> 자동 추출 KG는 **환각 가능** → 엔티티·관계를 원문과 대조한 뒤에만 논문에 반영(무결성 게이트).
> 본 결정론 KG는 그 대조의 **기준선(ground truth)** 으로도 쓸 수 있다.
