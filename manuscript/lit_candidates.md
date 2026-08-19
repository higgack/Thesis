# §2 문헌 보강 후보 (paper-lookup 실검색 결과)

> 출처: WebSearch 기반 실검색(2026-06). **실재·추적가능 문헌만** 수록(환각 없음).
> ⚠ 정확한 서지(저자·연도·권/호/쪽)는 **Stage 2.5 무결성 게이트에서 1차 출처로 확정 후** 인용한다.
> 산업 보고서(KnowMade 등)는 **학술 인용과 구분**(산업 자료로 표기).

## A. 산업 보고서 — 우리 발견의 독립 확증 (§4 보강)
- **KnowMade/Yole, *Hybrid Bonding Patent Landscape* (2019, 2024)** — 하이브리드 본딩 특허 **5,800+건 / 1,600+ 패밀리**, 선도 출원인 **TSMC·Adeia·YMTC·Intel·Samsung**, 미·중·유럽 중심.
  - 의의: **자료②의 규모(~5,278 레코드)·선도기업 순위를 외부 산업분석이 독립 확증** → §4.2/§4.7 신뢰도↑. (산업 보고서로 인용)
  - https://www.knowmade.com/ (Hybrid Bonding Patent Landscape)

## B. 기술융합(§2.3) — 학술
- **Technology fusion: Identification and analysis of the drivers of technology convergence using patent data**, *Technological Forecasting & Social Change*(추정) — 융합 '동인' 분석. https://www.sciencedirect.com/science/article/abs/pii/S0166497216300293 ★서지확정
- **Identifying the technology convergence using patent text information: a GCN-based approach**, *TFSC*(2022) — 텍스트+그래프합성곱 융합 식별. https://www.sciencedirect.com/science/article/abs/pii/S0040162522000099 ★서지확정
- **The effect of collaborative innovation on ICT-based technological convergence: a patent-based analysis**, *PLOS ONE*(2020) — 공동분류 기반 융합 측정. https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0228616 ★서지확정
- **Entropy and Gravity Concepts as New Methodological Indexes to Investigate Technological Convergence: Patent Network-Based Approach**, *PLOS ONE* — 네트워크 기반 융합 지표. https://www.ncbi.nlm.nih.gov/pmc/articles/PMC4051643/ ★서지확정

## C. 범용기술(GPT, §2.2/§6.2) — 학술
- **Mapping general purpose technologies with patent data**, *Research Policy*(2020) — **GPT 3차원 지표(개선성·범용성·보완성)** = 우리가 §6.2에서 "generality 미산출"이라 한계로 남긴 부분의 **방법론 출처**. https://www.sciencedirect.com/science/article/abs/pii/S0048733320300925 ★서지확정
  - 데이터셋: Harvard Dataverse "GPT Indicators" (doi:10.7910/DVN/PQGHKA)
- **Hall & Trajtenberg, Uncovering GPTs with Patent Data**, NBER WP 10901 (2004) — *이미 본문 인용 중*(재확인).

## 반영 계획
- **B·C**는 §2.3·§2.2/§6.2(향후과제 generality 산출)와 §6.6(선행연구 비교)에 1~2편씩 보강 → 서지 확정 후.
- **A(KnowMade)**는 §4.2/§4.7에 "외부 산업분석과의 정합" 한 문장으로 추가(산업 자료 표기) → 우리 수치 신뢰도 강화.
- 모든 추가는 **재현 가능한 실재 출처**만, 무결성 게이트 통과 후 본문 반영(다음 작업 시 rev6 후보).

## 재사용 도구
네트워크 되는 환경(학생 크레딧 등)에선 `python rag/paper_lookup.py "<질의>"`로 arXiv·Semantic Scholar 직접 검색 가능
(이 워크스페이스는 프록시 차단으로 WebSearch 사용).
