# §2 문헌 보강 후보 (paper-lookup 실검색 결과)

> 출처: WebSearch 기반 실검색(2026-06). **실재·추적가능 문헌만** 수록(환각 없음).
> ⚠ 정확한 서지(저자·연도·권/호/쪽)는 **Stage 2.5 무결성 게이트에서 1차 출처로 확정 후** 인용한다.
> 산업 보고서(KnowMade 등)는 **학술 인용과 구분**(산업 자료로 표기).

## A. 산업 보고서 — 우리 발견의 독립 확증 (§4 보강)
- **KnowMade/Yole, *Hybrid Bonding Patent Landscape* (2019, 2024)** — 하이브리드 본딩 특허 **5,800+건 / 1,600+ 패밀리**, 선도 출원인 **TSMC·Adeia·YMTC·Intel·Samsung**, 미·중·유럽 중심.
  - 의의: **자료②의 규모(~5,278 레코드)·선도기업 순위를 외부 산업분석이 독립 확증** → §4.2/§4.7 신뢰도↑. (산업 보고서로 인용)
  - https://www.knowmade.com/ (Hybrid Bonding Patent Landscape)

## B. 기술융합(§2.3) — 학술
- ✅**rev6 반영** — **Caviggioli, F. (2016).** Technology fusion: Identification and analysis of the drivers of technology convergence using patent data. *Technovation, 55–56*, 22–32. (저널명은 TFSC가 아니라 **Technovation** — 게이트에서 정정) → §2.3
- ✅**rev7 반영** — **Zhu, C., & Motohashi, K. (2022).** Identifying the technology convergence using patent text information: A graph convolutional networks (GCN)-based approach. *TFSC, 176*, 121477. doi:10.1016/j.techfore.2022.121477 → §2.3(측정 전통 2: 텍스트·표상학습), §6.6(결정론적 CPC 대비 트레이드오프)
- ✅**rev7 반영** — **Hwang, I. (2020).** The effect of collaborative innovation on ICT-based technological convergence: A patent-based analysis. *PLOS ONE, 15*(2), e0228616. doi:10.1371/journal.pone.0228616 → §2.3(측정 전통 3: 융합 동인), §6.6(조직 간 관계 vs 특허 내재 속성 — 범위 한계 명시)
- ✅**rev7 반영** — **Cho, Y., & Kim, M. (2014).** Entropy and gravity concepts as new methodological indexes to investigate technological convergence: Patent network-based approach. *PLOS ONE, 9*(6), e98009. doi:10.1371/journal.pone.0098009 → §2.3(측정 전통 1: 분류체계 기반 지표화), §6.6(집계지표 vs 특허단위 조작화)

## C. 범용기술(GPT, §2.2/§6.2) — 학술
- ✅**rev6 반영** — **Petralia, S. (2020).** Mapping general purpose technologies with patent data. *Research Policy, 49*(7), 104013. — **GPT 3차원 지표(개선성·범용성·보완성)** = 우리가 §6.2에서 "generality 미산출"이라 한계로 남긴 부분의 **방법론 출처**. https://www.sciencedirect.com/science/article/abs/pii/S0048733320300925 ★서지확정
  - 데이터셋: Harvard Dataverse "GPT Indicators" (doi:10.7910/DVN/PQGHKA)
- **Hall & Trajtenberg, Uncovering GPTs with Patent Data**, NBER WP 10901 (2004) — *이미 본문 인용 중*(재확인).

## 반영 계획
- ~~**B·C**는 §2.3·§2.2/§6.2와 §6.6에 1–2편씩 보강 → 서지 확정 후.~~ → **완료**: C(Petralia)는 rev6, B 잔여 3편은 **rev7**에 반영. Caviggioli는 rev6 반영.
- ~~**A(KnowMade)**는 §4.2/§4.7에 추가~~ → **완료(rev6)**.
- **B목록 전량 소진.** 추가 인용 보강은 새 후보 탐색부터 필요.
- 모든 추가는 **재현 가능한 실재 출처**만, 무결성 게이트 통과 후 본문 반영. rev7의 3건은 서지를 **독립 2회 교차확인**함(PLOS/NCBI/Crossref는 프록시 차단 → WebSearch 교차검증으로 대체).

## 재사용 도구
네트워크 되는 환경(학생 크레딧 등)에선 `python rag/paper_lookup.py "<질의>"`로 arXiv·Semantic Scholar 직접 검색 가능
(이 워크스페이스는 프록시 차단으로 WebSearch 사용).
