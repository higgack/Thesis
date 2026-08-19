# Stage 1 — 통합 연구 브리프 (RESEARCH)

> 산출물 유형: deep-research Stage 1 (RQ Brief · 통합 분석틀 · 방법론 · 종합 · 선행연구)
> 언어: 한국어 (전문용어 영문 병기) · 프레임: **다층 통합형(multi-level)**
> ⚠ 참고문헌의 정확한 서지정보(권/호/쪽)는 **Stage 2.5 무결성 게이트**에서 100% 검증 예정. 아래 URL은 추적용.

---

## 1. 대주제 (Working Title)

**특허 데이터를 활용한 차세대 반도체 패키징 기술 생태계의 진화: 하이브리드 본딩(Hybrid Bonding)을 중심으로 한 거시–미시 다층 분석**

A Multi-level Analysis of the Evolution of the Next-Generation Semiconductor Packaging Technology Ecosystem Using Patent Data: Focusing on Hybrid Bonding

---

## 2. 통합 연구질문 (Research Questions)

**핵심 RQ.** 하이브리드 본딩 기술은 특허 데이터 상에서 어떻게 진화하며, 그 진화는 **(거시)** 생태계·기업전략 차원과 **(미시)** 특허 수준의 기술범위·전후공정 지향 차원에서 어떤 **정합적 패턴**으로 나타나는가?

- **RQ1 (거시·생태계):** 출원 동학, 기술집중도, 기술 클러스터·네트워크, 인용지표(CII/PFS/SLI), 인용수명으로 본 생태계 구조와 **기업·관할권별 전략 분화**는 어떠한가?
- **RQ2 (미시·기술범위):** 특허의 **기술범위(breadth, CPC 서브클래스 수)** 는 출원연도·관할권에 따라 어떻게 결정되는가? *(음이항회귀)*
- **RQ3 (미시·전략지향):** 후공정(H01L24) 특허가 **전공정(H01L21)으로 침투**(has_process / integrated / process-only)하는 정도와 그 결정요인은? *(이항·다항 로짓)*
- **RQ4 (통합·경계):** 거시 명제("부가가치가 전공정→후공정으로 이동")와 미시 증거("후공정 특허의 전공정 침투")는 **전/후공정 경계의 흐려짐(technological convergence)** 이라는 하나의 현상으로 수렴하는가?

---

## 3. 통합 분석틀 (Integrated Framework)

상위 이론틀은 **P–S–P(공정 Process → 구조 Structure → 제품 Product)** 로 두고, 그 위에서 두 층위를 결합한다.

```
                ┌─────────────────────────────────────────────┐
이론 렌즈        │  P–S–P 프레임  ×  기술융합(경계 흐려짐) 렌즈   │
                └─────────────────────────────────────────────┘
                                 │
        ┌────────────────────────┴────────────────────────┐
 거시(MACRO)                                          미시(MICRO)
 기술 스캐닝·서술                                       가설 검정
 ─ 출원동학/집중도                                      ─ breadth → 음이항(RQ2)
 ─ 클러스터·네트워크                                    ─ has_process → 이항로짓(RQ3)
 ─ CII/PFS/SLI·인용수명                                ─ 전략유형 → 다항로짓(RQ3)
 ─ 기업·관할권 전략(자료②)                              ─ 시간·관할권 효과(자료①)
        └────────────────────────┬────────────────────────┘
                          RQ4: 두 층위의 수렴 = 경계 흐려짐 검증
```

**연결 논리:** 자료②의 거시 명제(가치사슬 전→후공정 이동)를, 자료①의 미시 계량(후공정 특허의 H01L21 침투)으로 **검증**한다. 즉 두 기존 페이퍼는 *같은 명제의 거시 서술 ↔ 미시 검정*으로 한 몸이 된다.

---

## 4. 방법론·데이터 (Methodology)

- **데이터:** EPO **PATSTAT 2025**. 하이브리드 본딩 928개 출원 / 5,277 출원–CPC 레코드(1968–2024), CPC **H01L21**(전공정)·**H01L24**(후공정) 중심.
- **거시:** 출원추이, 출원국/출원인 순위, 기술집중도(HHI 등), CPC 동시분류 기반 클러스터·네트워크, 인용지표(CII·PFS·SLI), 인용수명(citation lifetime).
- **미시:**
  - 기술범위 = 카운트 → OLS 진단(Breusch–Pagan) → Poisson → **음이항**(과산포 검정으로 정당화; AIC 비교).
  - 전략지향 = 이산선택 → **이항 로짓**(has_process, AUC) + **다항 로짓**(assembly-only 기준; integrated/process-only RRR).
- **통합:** RQ4에서 거시 추세(전공정 비중↓, 범위↑)와 미시 계수(시간·관할권)의 부호·방향 일치를 교차 해석.

---

## 5. 두 자료 합본 매핑 (Source → Thesis)

| 논문 장(예정) | 주 출처 | 보강 |
|---|---|---|
| 서론·연구배경 | ② | GPT·기술융합 이론 |
| 이론배경·선행연구 | 신규(문헌보강) | §7 |
| 분석틀(P–S–P) | ② | — |
| 데이터·변수 | ①② | — |
| 거시 결과(생태계·전략) | ② | 네트워크 보강 |
| 미시 결과(계량) | ① | — |
| 통합 논의(경계 흐려짐) | 신규(①+②) | 융합 문헌 |
| 결론·시사점·한계 | ①② | 한국 메모리 산업 시사점 |

---

## 6. 기여 (Contribution)

1. **이론** — P–S–P 프레임에 **기술융합(경계 흐려짐)** 렌즈를 결합한 다층 분석틀 제시.
2. **방법** — 동일 특허 데이터에 **기술 스캐닝(서술·네트워크)** 과 **가설 검정(카운트·이산선택)** 을 결합(혼합방법).
3. **실증** — 하이브리드 본딩을 **GPT 후보**로서 생태계·특허 수준에서 동시 규명.
4. **전략·정책** — 관할권·기업 전략 유형화 → 한국(메모리·파운드리) 시사점.

---

## 7. 선행연구 종합 (Literature — 검증 대기 시드)

**(A) 특허 카운트 계량의 토대**
- Hausman, Hall & Griliches (1984), *Econometrica* 52(4):909–938 — 특허 카운트(Poisson/음이항) 계량의 기초. https://www.nber.org/papers/t0017
- Cameron & Trivedi (2013), *Regression Analysis of Count Data* (2e), Cambridge UP — 과산포·음이항 표준 레퍼런스(자료① 기인용).

**(B) GPT(범용기술) 식별**
- Hall & Trajtenberg (2004), NBER WP 10901, *Uncovering GPTs with Patent Data* — generality·피인용·클래스 성장으로 GPT 식별. https://www.nber.org/papers/w10901

**(C) 특허 인용지표(CHI/Narin 계열)**
- Narin, Noma & Perry (1987), *Research Policy* — 기업 기술력 지표로서의 특허 인용(CII·Science Linkage·TCT 계열). https://www.sciencedirect.com/science/article/abs/pii/0172219083901394 *(서지 재확인 필요)*

**(D) 기술융합·경계 흐려짐**
- Curran & Leker — *Patent indicators for monitoring convergence*(NFF·ICT 예시) — 융합 모니터링 지표. https://www.academia.edu/19296679/
- Kim et al. (2019), *Technology Analysis & Strategic Management* 32(4) — *Anticipating technology-driven industry convergence: large-scale patent analysis*. https://www.tandfonline.com/doi/abs/10.1080/09537325.2019.1661374

**(E) 하이브리드 본딩·이종집적(기술 배경)**
- *State-of-the-Art and Outlooks of Chiplets Heterogeneous Integration and Hybrid Bonding*, *J. Microelectronics & Electronic Packaging* 18(4):145 (2021). https://meridian.allenpress.com/jmep/article/18/4/145/476339/
- *Manufacturing Challenges of Hybrid Bonding for Chiplets Heterogeneous Integration*, ASME *J. Electronic Packaging* 148(1):010801. https://asmedigitalcollection.asme.org/electronicpackaging/article/148/1/010801/

**(F) 기술범위·다양성 측정**
- 기술범위/다양성은 IPC/CPC 분산(예: Shannon Diversity Index) 또는 서브클래스 수로 측정 — 본 연구는 후자(자료①의 breadth) 채택.

> 다음 단계에서 (C)(D)(F)를 중심으로 1차 출처 확보·서지 확정 예정.

---

## 8. 미해결·결정 필요 항목

- 한국 산업 시사점의 비중(메모리 3사 vs 파운드리) — 결론 방향성.
- 네트워크 분석의 깊이(중심성·brokerage까지 vs 시각화 위주).
- 인과 식별 한계 명시(자료①의 연관성 한계 계승: IV·기업 고정효과 부재).
