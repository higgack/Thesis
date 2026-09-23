# 저널 투고용 논문 — 안내문 (한글)

> 대상 파일: `journal/manuscript_v1.md` (+ `manuscript_v1.docx`, `figures/`)
> 작성일: 2026-09-23 · 기반 자료: ① 계량분석 보고서(EN), ② 특허분석 학기 과제(KO), 학위논문 rev6/rev7
> 교수님 피드백 반영: **RQ1·RQ2는 배경(§3 Research context)으로 축소, RQ3·RQ4에 집중**, 기술융합을 이론적 배경으로, **가설 + 기존 이론에의 기여** 명시.

---

## 1. 무엇을 만들었나

| 항목 | 내용 |
|---|---|
| 제목 | *When the back end reaches into the front end: Technological convergence and boundary-spanning patents in semiconductor hybrid bonding* |
| 언어 | **영어** (아래 §4 "가정" 참조) |
| 구조 | Elsevier(Technovation/TFSC) 표준: Highlights(5) → Abstract(218단어) → Keywords(6) → 1 Introduction → 2 Theoretical background & hypotheses (2.1 융합 이론 · 2.2 반도체 가치사슬 내 융합 · 2.3 특허 측정 · 2.4 가설 H1–H3b) → 3 Research context (구 RQ1 거시분석 = 배경) → 4 Data & methods → 5 Results (RQ3·RQ4 = 본론) → 6 Discussion (6.1 이론적 기여 · 6.2 실무·정책 · 6.3 한계) → 7 Conclusion → CRediT·이해상충·Funding·Data availability·**AI 사용 고지**(Elsevier 필수) → Appendix A(CPC·검색전략)·B(범위 모형 전체표) → Figure captions → References |
| 분량 | 본문 약 7,500단어(표·캡션 포함; 참고문헌 제외) — Technovation 통상 범위(8–10k) 내 |
| 표 | Table 1(생태계 지표), 2(변수), 3(음이항), 4(이항 로짓), 5(다항 로짓), 6(가설 검정 요약), A1, B1 |
| 그림 | Fig.1 HB vs HBM 출원추이(②) · Fig.2 Top-10 출원인(②) · Fig.3 기술네트워크(영문판) · Fig.4 연도별 출원(①) · Fig.5 예측 breadth(①) · Fig.6 ROC(①) · Fig.7 process-only 예측확률(①) · Fig.A1 검색전략(②) |
| 참고문헌 | **61편**, 전부 서지 검증(`journal/refs_ledger.md`에 검증 기록). 신규 검증 46편 + rev6/rev7에서 기존 검증분 + 표준 교과서 2편 |

### 핵심 재구성 — 학위논문과 어떻게 다른가

| 학위논문(rev7) | 저널 논문(v1) |
|---|---|
| RQ1(거시 생태계)이 한 장(제4장) | §3 Research context 1개 절로 압축, **Table 1 한 장**으로 지표 요약 |
| RQ2(breadth 결정요인)이 §5.1 | §5.1 + Appendix B로 이동, "범위 = 설명변수" 역할로 재정의 |
| RQ3·RQ4가 §5.2–5.3, §6.1 | **논문 전체의 본론**. 가설 4개로 구조화, Table 6에 검정 요약 |
| 융합 이론 = §2.3 한 절 | §2 전체(2.1–2.4)를 융합 이론 + 반도체 산업구조 이론으로 재구축 |
| "거시–미시 수렴" 서사 | **"산업 내(intra-industry)·공정단계 간(cross-stage) 융합"**이라는 이론적 개념으로 승격 → 기존 융합 문헌(산업 간 융합)과의 차별점 = 기여 1 |
| 시간 추세: "전공정 지향 감소" | **"재구성(recomposition)"** 개념: process-only는 감소, integrated는 유지(RRR 0.993 n.s.) → 융합이 '더 많이'가 아니라 '형태를 바꾸며' 진행 = 기여 3. 교수님의 "전에는 따로 놀았다, 지금은…"에 대한 정직한 답 |

### 가설과 결과 (Table 6)

| 가설 | 예측 | 근거 이론 | 결과 |
|---|---|---|---|
| H1 | 기술범위↑ → 경계 넘음(전공정 청구)↑ | Kodama 융합, Lerner 특허범위, Ziedonis 포트폴리오 | OR 1.267***, RRR integrated 1.403*** / process-only 0.905** → **지지** |
| H2 | 성숙할수록 process-only↓, integrated는 유지 | Hacklin 단계모형, W2W→D2W 산업궤적 | RRR process-only 0.913***/년, integrated 0.993 n.s. → **지지** |
| H3a | 중국 출원청 = 전공정·process-only 지향 | Lee & Lim 추격이론, Hu & Jefferson·Dang & Motohashi 중국 특허 | OR 1.650**, RRR 2.687***, breadth IRR 0.785*** → **지지** |
| H3b | 일본 출원청 = 넓고 process-only 거의 없음 | Langlois & Steinmueller 장비·소재 강점, Sakakibara & Branstetter 다항청구 | IRR 1.270*, RRR 0.086***, OR 0.483 n.s. → **대체로 지지** |

---

## 2. 이론적 배경으로 넣은 외국 문헌 (요청 2번)

### (a) 기술융합 이론 — 고전 → 최신
Rosenberg 1963(JEH) · Kodama 1992(HBR) · Gambardella & Torrisi 1998(RP) · Athreye & Keeble 2000(Technovation) · Fai & von Tunzelmann 2001(SCED) · Bröring et al. 2006(R&D Mgmt) · Hacklin et al. 2009(TFSC, 단계모형) · Curran et al. 2010 · Curran & Leker 2011(TFSC) · Hacklin et al. 2013(MIT SMR) · Geum et al. 2016(TFSC) · Sick et al. 2019(Technovation) · **Sick & Bröring 2022(TFSC 리뷰)** · Caviggioli 2016 · Hwang 2020

### (b) 특허 기반 융합 측정
Karvonen & Kässi 2013 · Preschitschek et al. 2013(Foresight, 의미분석 vs IPC) · Cho & Kim 2014 · Lee et al. 2015 · Jeong et al. 2015(Scientometrics) · Song et al. 2017 · Kwon et al. 2020 · Zhu & Motohashi 2022(GCN) · No & Park 2010

### (c) **반도체 산업구조·수직분업** (요청하신 "반도체 관련" 부분)
- Langlois & Steinmueller 1999 — 반도체 산업 경쟁우위의 진화 (Mowery & Nelson 편)
- **Macher & Mowery 2004** — 수직 전문화(팹리스·파운드리·OSAT 분리)
- Brown & Linden 2009 — *Chips and Change* (MIT Press)
- **Kapoor 2013 (Org. Sci.)** — 반도체 산업에서 "분업 바람 속 통합의 지속" ← 파운드리의 패키징 내재화를 설명하는 핵심 근거
- **Kapoor & Adner 2012 (Org. Sci.)** — "만드는 것 vs 아는 것": 지식 경계가 생산 경계보다 넓을 때의 이점 (DRAM)
- Adner & Kapoor 2010 (SMJ) — 반도체 리소그래피 생태계
- Brusoni, Prencipe & Pavitt 2001 (ASQ) — "firms know more than they make"
- Jacobides, Knudsen & Augier 2006 (RP) — 산업 아키텍처
- Ernst 2005 (IJIM) — 칩 설계 모듈성의 한계
- Khan, Hounshell & Fuchs 2018 (Nature Electronics) — 무어의 법칙 종말과 정책
- Arden et al. 2010 (ITRS More-than-Moore) · Iyer 2016 · Lau 2021, 2022 (패키징 공학)
- Hall & Ziedonis 2001 · Ziedonis 2004 — 반도체 특허 행태
- Lerner 1994 · Marco et al. 2019 — 특허 범위(scope)
- 관할권: Lee & Lim 2001(한국 추격) · Hu & Jefferson 2009 · Dang & Motohashi 2015 · Grimes & Du 2022(중국 반도체 GVC) · Sakakibara & Branstetter 2001(일본 1988 개혁)

**솔직한 보고:** "반도체 전/후공정 경계에 융합 측정을 적용한" 동료심사 논문은 검색으로 찾지 못했습니다. 산업 기사(SemiEngineering, SEMICON Japan)만 "경계 흐려짐"을 언급합니다. **이것이 곧 이 논문의 gap이고, 기여 1(산업 내 융합)의 근거입니다.** §1과 §2.2에 그렇게 서술했습니다.

---

## 3. 저널 양식 적합성 (요청 3번)

| 저널 | 출판사·분야 | 적합도 | 이유 / 조정 필요 |
|---|---|---|---|
| **Technovation** (1순위) | Elsevier, 기술혁신경영 | ★★★ | Caviggioli 2016·Sick et al. 2019 게재지. 융합+특허+산업구조 조합이 정확히 맞음. **현재 원고가 이 양식** (Highlights 필수, 참고문헌 name–year) |
| **TFSC** (2순위) | Elsevier, 기술예측 | ★★★ | 융합 문헌의 본산(Hacklin·Curran·Sick·Zhu). 양식 동일 → 그대로 투고 가능. 다만 "예측" 함의를 §6에 한 문단 추가 권장 |
| Research Policy | Elsevier, 혁신경제 | ★★☆ | 인과 식별 요구가 높음. 현재 결과는 associational → 응용인 FE·IV 추가 없이는 리스크 |
| IEEE Trans. Eng. Mgmt | IEEE, 공학경영 | ★★☆ | 형식 변환 필요(IEEE 번호 인용, 2단). 내용은 적합 |
| J. Eng. Tech. Mgmt / Scientometrics | Elsevier / Springer | ★★☆ | 측정방법론 강조 시 대안 |
| 국내: 기술혁신학회지·기술혁신연구 | KCI | ★★☆ | 한글 번역 필요 — 요청 시 학위논문 rev7 문체로 변환 가능 |

Elsevier 투고 시 별도 파일: (1) Highlights (본문에 포함됨, 분리 제출), (2) Title page (저자·소속·이메일 — **현재 placeholder**), (3) Graphical abstract(선택 — Fig.3 축소판 활용 가능), (4) Declaration of interest, (5) Supplementary Material S1.

---

## 4. 가정·주의사항 (반드시 읽어주세요)

1. **언어 = 영어.** "유명한 저널(산업공학·기술경영·경영학)"을 SSCI급 국제저널로 해석했습니다. 국내 KCI 저널이 목표라면 말씀 주세요 — 번역·문체 변환은 한 세션이면 됩니다.
2. **가설은 분석 이후에 세워졌습니다(사후 가설).** 자료 ①의 회귀는 가설 없이 수행됐고, 이번에 이론에서 가설을 도출해 "검정"으로 서술했습니다. 이론 기반 가설을 기존 데이터에 검정하는 것 자체는 통상적이지만, **결과를 보고 가설을 맞춘 것(HARKing)이 아닌지** 심사자가 물을 수 있습니다. 방어 방법: (a) H2를 보면 결과가 단순하지 않습니다(integrated n.s.) — 결과에 맞춰 만든 가설이라면 이렇게 쓰지 않았을 것; (b) 가능하면 **모형을 한 번 더 돌려** 추가 통제(출원인 유형, 청구항 수)를 넣은 확장 명세로 재검정 → §5.4 robustness에 추가. 이게 가장 확실합니다.
3. **모든 수치는 자료 ①②에서 인용, 재계산 아님.** Table 3–5·B1은 ①의 Table 2–4를 그대로 옮겼습니다(1:1 대조 완료). Table 1은 ②의 지표를 옮겼습니다. Fig.7의 "1/4 vs 1/7 vs 1/50"은 ①의 그림에서 읽은 근사값입니다.
4. **Supplementary Material S1**은 아직 없습니다. 자료 ②(학기 과제)의 출원인 분석 부분을 영문으로 정리해 S1로 만들어야 합니다(Table 1·Fig.1·2의 출처).
5. **저자 정보 placeholder**: 소속 대학, 이메일, Acknowledgements. 채워주세요.
6. **그림 해상도**: Fig.1·2·4–7·A1은 원본 보고서에서 추출한 PNG입니다. 투고 시 Elsevier 기준(300 dpi, TIFF/EPS)으로 **원자료에서 재출력**해야 합니다. Fig.3(네트워크)은 `manuscript/figures/`의 결정론 KG 영문판 — 저널용으로는 CII/SLI 수치 라벨을 줄인 간결판이 나을 수 있습니다.
7. **ASME JEP 2026 논문 제외**: 학위논문 참고문헌에 있던 "Manufacturing challenges of hybrid bonding…" (J. Electron. Packag. 148(1))은 **저자를 확인할 수 없어** 저널 논문에서 뺐습니다. 학위논문 rev7에도 저자 미상 ★확인 상태로 남아 있으니 확인 필요.
8. **AI 사용 고지**: Elsevier는 생성형 AI 사용 시 본문에 고지를 요구합니다. 문안을 넣어 두었습니다 — 저자가 사실에 맞게 수정하세요.
9. 단일 저자에 "we"를 썼습니다(편집자적 we, 경영학 저널 관행). "I"로 통일을 원하시면 일괄 변경 가능합니다.

---

## 5. 다음 단계 제안

1. **[저자]** 소속·이메일 입력, AI 고지 문안 확인, §6.2 한국 시사점 톤 점검
2. **[분석 재실행 권장]** 자료 ① 데이터로 확장 명세(출원인 유형 더미·청구항 수) 재추정 → HARKing 방어 + robustness 강화. Stata/R 코드가 있으면 바로 붙일 수 있습니다.
3. **[S1 작성]** 자료 ②의 출원인 지표를 영문 표 3–4개로 정리
4. **[그림 재출력]** 300 dpi
5. **[교수님 검토]** 특히 §2.4 가설 문구와 §6.1 "recomposition" 해석
6. 검토 후 → v2 (동일 파일 보존, `manuscript_v2.md`로 분기)
