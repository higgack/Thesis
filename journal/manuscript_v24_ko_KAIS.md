# 반도체 하이브리드 본딩 특허에 나타난 공정단계 간 기술융합: 경계를 넘는 특허의 특성과 출원청별 차이

**이형규** (한양대학교 기술경영학과)

**Cross-stage technological convergence in semiconductor hybrid bonding patents: Characteristics of boundary-spanning patents and differences across filing offices**

**HyungKyu Lee** (Department of Management of Technology, Hanyang University)

**요 약** 반도체 제조는 웨이퍼 위에 소자를 만드는 전공정과 그 웨이퍼를 잘라 조립하는 후공정으로 오랫동안 나뉘어 왔다. 하이브리드 본딩은 후공정의 접합 기술이지만 표면 평탄화와 표면 처리 같은 전공정의 제조공정 기술 없이는 성능이 나오지 않는다. 본 연구는 두 영역의 기술이 하나의 특허출원 안에 함께 담기는지, 어떤 출원이 그러한지를 유럽특허청 PATSTAT의 하이브리드 본딩 관련 특허출원 928건으로 분석하였다. 출원마다 부여된 CPC 기호를 기준으로 접합 기술 분류(H01L24)만 있으면 조립 단독형, 제조공정 기술 분류(H01L21)만 있으면 공정 단독형, 둘이 함께 있으면 통합형으로 구분하고, 두 클래스의 공동분류를 제조공정 영역과 접합 영역 사이의 경계 넘기를 재는 대리측정치로 사용하였다. 기준 정의에서는 통합형이 45.8%로 가장 많았고, 조립 단계 공정을 제외한 보수적 정의에서도 37.2%였다. 음이항 회귀와 다항 로짓 분석 결과, 두 클래스 안에서 세부 기술을 더 다양하게 포함한 출원일수록 통합형일 가능성이 높았으며(조립 단독형 대비 상대위험비 1.40), 최근 출원일수록 공정 단독형의 상대적 가능성은 낮았고(2014년 이전 43.6%에서 2020–2024년 10.4%), 중국 출원청 출원은 미국 출원청 출원보다 클래스 내 CPC 범위가 좁았다. 중국 출원청의 공정 단독형 편중은 최근 출원에 민감한 제한적 결과였다. 이 결과는 기술융합이 산업 사이만이 아니라 한 산업의 공정 단계 사이에서도 일어난다는 견해와 정합적인 특허 수준의 증거를 제공한다.

**Abstract** Semiconductor manufacturing has long been divided into front-end wafer fabrication and back-end assembly. Hybrid bonding belongs to the back end, yet it depends on front-end fabrication capabilities such as surface planarisation and surface treatment. Using 928 hybrid-bonding-related patent applications from EPO PATSTAT, this study asks whether the two bodies of technology are combined within single applications and which applications do so. Each application is classified by its CPC symbols as assembly-only (H01L24 only), process-only (H01L21 only) or integrated (both), and co-classification in the two classes is used as a patent-based proxy for boundary spanning between the fabrication-process and bonding domains. Integrated applications form the largest group at 45.8% (37.2% under a narrower definition that excludes assembly-stage processes). Negative binomial and multinomial logit estimates show that applications spanning more diverse technical elements within the two classes are more likely to be integrated (relative-risk ratio 1.40 against assembly-only), that process-only classification is relatively less likely for recent filings (43.6% before 2015 to 10.4% in 2020–2024), and that applications filed at the Chinese office are narrower in within-class CPC breadth than those filed at the US office, whereas their tilt towards process-only classification is sensitive to the most recent filings. The findings provide patent-level evidence consistent with technological convergence across the process stages of a single industry.

**Keywords :** Technological Convergence, Industry Architecture, Hybrid Bonding, Advanced Packaging, Within-class CPC Breadth, Multinomial Logit

## 1. 서론

반도체 산업의 분업 구조는 반세기 넘게 이어져 왔다. 트랜지스터를 웨이퍼 위에 만드는 일은 전공정(front end)의 몫이고, 완성된 웨이퍼를 잘라 조립하고 포장하는 일은 후공정(back end)의 몫이었다. 전공정은 종합반도체기업(IDM)과 파운드리가 운영하는 대규모 팹에 집중되었고[1], 패키징과 테스트는 조립·테스트 전문업체(OSAT)로 외주화되었다[2,3]. 두 단계 사이의 인터페이스가 비교적 안정적으로 관리되었기에 기업 간 전문화가 가능했다. 그러나 미세화가 한계에 부딪히면서 산업은 다이를 나누고 쌓고 연결하는 방식에서 새 성능 향상의 여지를 찾기 시작했고[4,5], 그 중심에 있는 하이브리드 본딩은 이 전제를 흔든다. 솔더 범프 없이 구리 패드와 주변 산화막을 직접 맞붙이는 이 기술은 10 µm 이하의 연결 간격을 열어 주지만[6,7], 그 성패는 나노미터 수준의 표면 평탄화, 표면 활성화, 정밀 정렬처럼 전통적으로 전공정에 속해 온 제조공정 역량에 달려 있다. 기술적 상호의존이 커지자 선도 파운드리가 첨단 패키징 역량을 내부에 두려는 움직임도 나타났다[7,8].

이러한 경계의 흐려짐을 개별 특허 수준에서 관찰할 수 있는가. 기술융합 문헌은 서로 구분되던 기술 영역이 겹치고 결합하는 과정을 오랫동안 다루어 왔지만[9-12], 그 실증 연구는 정보통신과 가전[13], 식품과 제약[14], 나노기술과 바이오기술[15]처럼 서로 다른 산업이나 기술 분야가 겹치는 경우에 집중되어 왔다. 한 산업 안에서 가치사슬의 단계를 가로지르는 융합은 좀처럼 다루어지지 않았으며, 가치사슬 상·하류 기업 간의 특허 인용을 분석한 Karvonen과 Kässi[40]도 하나의 기술이 공정 단계의 경계를 넘는지를 특허 단위에서 검정하지는 않았다. 산업 아키텍처, 곧 누가 무엇을 만들고 누가 가치를 가져가는가를 정하는 틀[16]에 압력을 가하는 융합이 있다면 바로 이런 종류의 융합이다.

본 연구는 유럽특허청(EPO) PATSTAT에서 추출한 하이브리드 본딩 관련 특허출원 928건과 각 출원에 부여된 협력적 특허분류(CPC) 기호를 이용한다. 접합·인터커넥트 기술(H01L24)과 반도체 제조공정 기술(H01L21)의 기호가 한 출원에 함께 부여되어 있으면 통합형, 접합 기호만 있으면 조립 단독형, 제조공정 기호만 있으면 공정 단독형으로 나눈다. 공동분류로 융합을 포착하는 방법 자체는 표준적이지만[11,17], 본 연구는 이를 두 산업 사이가 아니라 한 산업을 가로지르는 경계에 적용하고, 분야 수준의 지수로 집계하는 대신 개별 출원의 유형으로 재구성한다. 연구 질문은 셋이다. 하이브리드 본딩 특허에서 제조공정과 접합 기술의 공동분류는 얼마나 일반적이며 시간에 따라 어떻게 변했는가, 어떤 특허 특성이 공동분류 유형과 관련되는가, 그리고 이러한 유형과 기술범위는 출원청별로 어떻게 다른가이다.

## 2. 이론적 배경

### 2.1 기술융합과 특허 기반 측정

한 영역에서 개발된 기술이 다른 영역으로 옮겨 간다는 생각은 오래되었다. Rosenberg[9]는 19세기 공작기계 산업에서 특정 제품을 위해 개발된 가공 기술이 다른 제품에도 쓰이게 되는 과정을 기술적 수렴이라 불렀고, Kodama[10]는 분리되어 있던 기술 분야를 결합해 어느 한쪽도 홀로 갖지 못한 역량을 만드는 과정을 기술융합이라 개념화했다. Hacklin et al.[30]은 융합이 지식, 기술, 응용, 산업의 융합으로 단계적으로 진행된다는 단계 모형을 제시했고, 이 단계들이 되먹임하며 공진화적 순환을 이룬다고 보았다[13]. Curran과 Leker[11]는 이 단계들을 특허 지표로 관찰하는 방법을 제시했으며, 이후 이 문헌은 기술혁신경영의 독립된 연구 흐름으로 자리 잡았다[12,18]. 융합의 동인에 관한 실증으로는 융합이 일어나기 쉬운 기술 분야의 특성을 살핀 Caviggioli[41], 한국 정보통신기업의 협력 유형과 융합의 관계를 살핀 Hwang[42]이 있다.

이 문헌의 두 가지 특징이 본 연구의 출발점이다. 첫째, 초점이 서로 다른 산업이나 기술 분야의 중첩에 있다[14,15,19]. 둘째, 융합은 보통 두 분야 사이의 공동분류나 인용을 집계한 분야 수준의 지표로 측정된다[17,20-24]. 분류가 발명을 뒤늦게 따라가는 문제를 피하려고 특허 텍스트를 이용하는 연구도 있다[25,26]. 이런 지표는 두 분야가 융합하는지, 언제 융합하는지를 알려 주지만, 어떤 발명이 융합을 담고 있고 어떤 특성의 발명이 그러한지는 알려 주지 못한다. 개별 특허의 차이가 평균 속에 묻히기 때문이다. 본 연구는 공동분류를 신호로 쓴다는 점에서 첫째 흐름에 속하되, 분야 수준으로 집계하는 대신 개별 출원을 조립 단독형, 공정 단독형, 통합형의 세 공동분류 유형으로 나눈다. 다만 CPC 기호는 심사관이 발명의 기술 내용에 부여하는 것이므로, 공동분류는 두 영역의 기술이 한 발명에 함께 담겼다는 사실의 대리측정치이지 법적 청구범위의 측정치는 아니다.

### 2.2 반도체 공정단계 사이의 융합과 산업 아키텍처

산업과 산업 사이의 경계 말고도 한 산업 안에서 가치사슬의 단계를 가르는 경계가 있다. 반도체 산업은 이 경계가 수직 전문화 연구의 오랜 대상이었다. Langlois와 Steinmueller[43]는 산업 구조가 바뀌는 동안 경쟁우위가 기업과 국가 사이에서 어떻게 옮겨 다녔는지를 추적했고, Macher와 Mowery[2]는 설계와 제조, 제조와 조립이 분리되어 팹리스, 파운드리, 조립·테스트 전문업체가 생겨난 과정을, Brown과 Linden[3]은 이 구조가 여러 차례의 위기를 거치며 재협상되어 온 과정을 기술했다. Kapoor[1]에 따르면 전문화가 진행되는 가운데서도 여러 단계에 걸친 역량을 유지한 기업이 기술 전환에 다르게 대응했고 그 선택이 산업 아키텍처를 만들어 왔으며, Kapoor와 Adner[44]는 부품과 시스템이 서로 얽혀 있을 때 기업이 직접 만드는 범위보다 넓은 지식을 가질수록 유리하다는 점을 DRAM 산업에서 보였다.

두 문헌을 잇는 논리는 다음과 같다. 산업 아키텍처[16]는 공정 단계 사이의 인터페이스가 안정적일 때 전문화를 허용한다. 새로운 기술이 한 단계의 성능을 다른 단계의 지식에 의존하게 만들면 인터페이스가 불안정해지고, 기업은 경계 반대편의 지식을 획득하거나 통합해야 한다. 이때 지식의 결합은 먼저 발명 수준에서 나타나며, 특허의 공동분류는 그 흔적이다. 하이브리드 본딩이 이런 기술에 해당한다. 수율이 평탄화, 표면 화학, 정렬처럼 전공정에 자리한 제조공정 역량에 달려 있으므로 제조와 패키징 사이의 인터페이스는 더 이상 안정적이지 않다. 여기서 말하는 융합은 수직통합과 다르다. 수직통합 문헌이 묻는 것은 조직의 경계이고, 융합 문헌이 묻는 것은 하나의 발명 안에서 결합되는 지식의 경계다. Hacklin et al.[30]의 단계 모형에서 기술의 융합은 산업의 융합보다 먼저 오며, 본 연구가 관찰하는 것도 그 앞선 단계다. 따라서 산업 아키텍처는 직접 검정하는 결과변수가 아니라 결과를 해석하는 상위의 이론적 맥락이다.

### 2.3 하이브리드 본딩 기술

하이브리드 본딩은 유전체 안에 구리 패드가 새겨진 두 평탄한 표면을 맞대어, 상온에서 유전체끼리 먼저 붙고 저온 어닐링 뒤 구리끼리 붙게 하여 영구적인 전기 연결을 만드는 기술이다. 연결 간격은 마이크로범프 플립칩으로는 어려운 10 µm 이하로 내려가며 연구 단계에서는 1 µm 이하에 이른다[6,7]. 웨이퍼-투-웨이퍼(W2W) 접합은 CMOS 이미지센서와 3D NAND에서 먼저 상용화되었고, 다이-투-웨이퍼(D2W) 접합은 칩렛, 고대역폭 메모리(HBM), 3D 로직에 필요하다. 두 형태 모두에서 수율을 좌우하는 화학적 기계연마(CMP), 표면 활성화, 파티클 제어, 정밀 정렬은 CPC 분류상 접합 클래스(H01L24)가 아니라 제조공정 클래스(H01L21)에 속한다. Fig. 1은 하이브리드 본딩을 기반 공정, 접합 형태, 소자 구조, 적용 제품의 네 층으로 정리한 것이며, 기반 공정은 주로 H01L21과, 접합 형태 및 인터커넥트 구성은 주로 H01L24와 대응한다. 산업 지형도 같은 방향을 가리킨다. 독립적 산업 분석은 TSMC, Adeia, YMTC, Intel, Samsung을 특허 활동을 주도하는 출원인으로 꼽는데[8], 원천기술을 가진 라이선싱 기업, 첨단 패키징을 직접 운영하는 파운드리, 메모리 제조사가 나란히 앞서 있다는 사실은 접합 기술이 더 이상 후공정 전문기업만의 영역이 아님을 보여준다.

![](figures/fig_framework_psp.png)

**Fig. 1.** Enabling processes, bonding configurations, device structures and applications of hybrid bonding (drawn by the author based on Lau[6,7]).

## 3. 연구가설 및 연구방법

### 3.1 연구가설

**3.1.1 기술적 다양성.** Kodama[10]의 융합도 Rosenberg[9]의 수렴도 둘 이상의 기술 영역에 걸치는 발명을 뜻한다. 경계 넘기가 융합의 한 형태라면, 두 클래스 안에서 더 다양한 세부 기술 요소를 포함하는 출원일수록 두 영역의 기호가 함께 부여될 가능성이 높아야 한다. 특허 범위 연구는 특허가 걸치는 분류의 수로 범위를 재고 범위가 경제적 가치를 가짐을 보였으며[27], 반도체에서는 특허 시장이 잘게 나뉘어 있을수록 기업이 넓은 포트폴리오를 쌓는다[28,29]. 다만 기술범위와 공동분류 유형은 같은 CPC 집합에서 만들어지고 통합형은 정의상 두 클래스의 기호를 하나씩은 가지므로, H1은 인과적 결정요인이 아니라 통합형 특허의 구성적 특징에 관한 가설이다.

> **H1.** 제조공정 및 접합 영역에서 더 다양한 세부 기술 요소를 포함하는 출원일수록 두 영역의 CPC 기호가 함께 부여될(통합형일) 가능성이 높다.

**3.1.2 시간 변화.** 융합의 단계 모형[23,30]에 따르면 기술이 발전하면서 융합의 형태도 바뀐다. 하이브리드 본딩의 초기 발명은 표면 준비, 저온 어닐링, 평탄화처럼 직접 접합을 가능하게 한 공정 단계에 관한 것이어서 공정 발명으로 분류되었고, 적용 범위가 웨이퍼 단위 접합에서 다이 단위 접합으로 넓어지면서 발명의 중심은 접합된 구조와 적층으로 옮겨 갔다[6]. 출원연도에는 기술 발전 외에 분류 체계와 심사 관행, 출원인 구성, 공개 시차도 반영되므로 연도 효과는 시간에 따른 변화로 해석한다.

> **H2.** 출원연도가 최근일수록 특허가 조립 단독형보다 공정 단독형으로 분류될 상대적 가능성은 낮다.

**3.1.3 출원청.** 어디에 출원하는가에는 누가 무엇을 위해 발명하는가가 드러난다. 중국의 특허 급증은 제도 변화와 외국인직접투자[31], 건수를 보상하는 보조금 정책[32]에 힘입었고, 반도체 가치사슬에서 중국의 위치는 지정학적 압력 속에서 빠르게 바뀌어 왔다[33]. 추격 문헌은 후발자가 선발자의 경로를 단계별로 따라가거나 일부 단계를 건너뛴다고 본다[34]. 후발국의 특허 활동이 생산 역량의 국산화, 공정별 병목의 해소, 특정 제조 단계의 권리 확보에 집중된다면 하나의 출원이 포괄하는 기술 요소는 시스템 전체의 통합보다 개별 공정 문제의 해결에 가까워지고, 두 클래스 안의 CPC 범위는 좁아지며 공정 단독형의 비중은 높아질 것이다. 이는 중국 특허 선행연구의 직접적 결론이 아니라 단계별 추격의 논리를 본 연구의 특허 구성에 적용해 끌어낸 예상이며, 범위와 유형은 서로 다른 모형으로 검정되므로 가설을 둘로 나눈다. 일본 출원청은 표본이 26건에 그치므로 가설로 세우지 않는다.

> **H3a.** 중국 출원청에 출원된 특허는 미국 출원청에 출원된 특허보다 기술범위가 좁다.

> **H3b.** 중국 출원청에 출원된 특허는 미국 출원청에 출원된 특허보다 조립 단독형 대비 공정 단독형으로 분류될 가능성이 높다.

### 3.2 데이터 및 변수

자료는 유럽특허청의 PATSTAT Global 2025년판에서 가져왔다[35]. CPC 분류 H01L21 또는 H01L24 계열 기호를 하나 이상 가진 출원을 후보로 한정한 뒤, 영문 제목을 소문자로 바꾸어 지정 키워드(*hybrid bonding*, *direct bonding*, *Cu–Cu*, *copper to copper*, *metal-oxide bond*) 가운데 하나 이상이 들어 있는 출원만 남겼다(Table 1). 출원–CPC 쌍 5,277건을 출원 식별자별로 집계하면 1968–2024년에 출원된 928건이 되며, 분석단위는 개별 특허출원이다. 추출 단계에서 두 클래스 밖의 기호는 보존되지 않았으므로 모든 기호는 H01L21 계열(202개)과 H01L24 계열(60개)의 서브그룹 기호 262개로 이루어진다. 이 광의의 표본은 현대의 하이브리드 본딩뿐 아니라 그 기술적 기반이 된 초기 직접 접합 전구 기술을 함께 포착하도록 설계되었다. 세 가지 특성을 밝혀 둔다. 제목에 *hybrid bonding*이 들어 있는 핵심 표본은 467건이고, 직접 접합의 무관한 용법(DBC 기판, 구리 합금, 전력 모듈 등)으로 판정한 출원은 68건이며, 공식 패밀리 식별자가 없어 같은 제목의 출원을 근사 중복군으로 보면 928건은 473개의 고유 제목으로 이루어진다. 이들은 4.4절의 민감도 분석에 쓴다.

**Table 1.** Search strategy for hybrid-bonding-related applications

| Step | Condition | Description |
|---|---|---|
| CPC | H01L21 | Processes and apparatus for manufacturing semiconductor devices, including wafer bonding (21/18, 21/20), isolation, interconnect and TSV (21/76), substrate and insulating-layer treatment (21/02), CMP (21/30–21/32); also assembly-stage processes (21/48, 21/50–21/58, 21/60, 21/78) and equipment (21/67–21/68) |
| CPC | H01L24 | Connector structures, bump, layer and wire connections, and related methods and apparatus for connecting or disconnecting semiconductor or solid-state bodies |
| Keyword | Title | At least one of *hybrid bonding*, *direct bonding*, *Cu–Cu*, *copper to copper*, *metal-oxide bond* |

본 연구는 H01L21을 전공정 그 자체로, H01L24를 후공정 그 자체로 보지 않는다. H01L21에는 웨이퍼 제조공정 외에 패키지 부품 제조, 실장·봉지, 리드 부착, 다이싱 같은 조립 단계의 제조공정과 공정 장치가 함께 들어 있고, 표본의 H01L21 계열 기호 1,636건 가운데 웨이퍼 제조공정이 905건(55.3%), 공정 장치가 328건(20.0%), 조립 단계 공정이 403건(24.6%)이다. 그럼에도 두 클래스의 공동분류를 대리측정치로 쓸 수 있는 것은, 하이브리드 본딩의 성능을 좌우하는 웨이퍼 준비, 표면 처리, 평탄화 및 관련 제조공정이 H01L21에 다수 포함되고 접합·인터커넥트 기술은 H01L24에 모여 있기 때문이다. 조립 단계 공정을 제조공정으로 세지 않는 좁은 정의로 다시 추정한 결과는 4.4절에 보고한다.

Table 2는 변수를 정의한다. 기술범위(breadth)는 출원별로 부여된 H01L21·H01L24 계열의 서로 다른 CPC 서브그룹 수로, 분류 기호의 수로 범위를 재는 Lerner[27]의 방식을 두 클래스 안에 적용한 것이다. 통합형은 정의상 두 클래스의 기호를 하나씩은 가져 최소 breadth가 2인 반면 단독형의 최소값은 1이므로, 보유 클래스마다 최소 1개 기호를 차감한 유형 최소치 조정 CPC 범위(breadth_adj = breadth − I[H01L21 보유] − I[H01L24 보유])를 탐색적 강건성 지표로 따로 두었다. 공동분류 유형(tech_cat)은 조립 단독형, 통합형, 공정 단독형의 세 범주이고, 제조공정 분류 여부(has_process)는 H01L21 기호를 하나라도 가지면 1이다. 출원청(ccode)은 출원이 제출된 특허청 또는 출원 경로로 8개 수준이며 미국이 기준이고, 출원연도(yc)는 표본의 첫 출원(1968년)부터 지난 햇수다. 자료에 출원인 정보가 없으므로 출원청 계수는 관할권의 인과효과가 아니라 해당 출원청에 제출된 발명 포트폴리오의 조건부 구성 차이로 해석한다.

**Table 2.** Variable definitions

| Variable | Definition | Type | Caution |
|---|---|---|---|
| breadth | Number of distinct H01L21/H01L24 CPC subgroups assigned to the application (within-class CPC breadth) | Count, 1–21 | Not total CPC scope or legal claim scope |
| breadth_adj | breadth minus one symbol per class held (stand-alone types: breadth − 1; integrated: breadth − 2) | Count, 0–19 | Exploratory; built with type information |
| tech_cat | Co-classification type: assembly-only (H01L24 only) / integrated (both) / process-only (H01L21 only) | 3 categories | Combination of classifications, not firm strategy |
| has_process | 1 if at least one H01L21 symbol is assigned | Binary | Fabrication-process classification, not the front end itself |
| ccode | Filing office or route: US (reference), CN, KR, TW, JP, EP, WO (PCT), other | 8 categories | Not applicant nationality |
| yc | Filing year minus 1968; sample mean 49.7 (2017.7) | Integer, 0–56 | Not a direct measure of maturity |

### 3.3 분석방법

종속변수의 척도와 가설이 묻는 질문이 다르므로 세 모형을 쓴다(Table 3). 기술범위는 1–21의 정수로 측정되는 카운트 변수이고 분산이 평균보다 훨씬 커서(과산포 모수 α = 0.268, 표준오차 0.020) Poisson 대신 음이항 회귀를 주모형으로 쓰며, 계수의 지수값인 발생률비(IRR)를 출원당 기대 CPC 서브그룹 수의 비율로 해석한다[36]. 제조공정 분류 여부는 이항 로짓으로 추정하고 승산비(OR)로 보고하되, 통합형과 공정 단독형을 묶어 제조공정 분류의 전반적 보유 여부를 확인하는 보완분석이다. 공동분류 유형은 순서 없는 세 범주이므로 조립 단독형을 기준으로 하는 다항 로짓[37]으로 추정하고 상대위험비(RRR)로 보고한다. 모든 모형의 설명변수는 출원연도와 출원청 더미이고 두 로짓 모형에는 기술범위를 더한다. RRR과 OR은 비율 척도이므로, 다항 로짓에 대해서는 관심 변수만 바꾸고 나머지는 각 출원의 관측값에 둔 채 표본 전체에서 평균한 평균 예측확률을 함께 제시한다. 가설 판정에는 5% 유의수준을 쓰되 단일 p값만으로 정하지 않고, 기준모형과 대안 표본·정의, 동일 제목 군집 표준오차에서 계수의 방향, 효과 크기, 유의성이 일관되는지를 함께 본다. 모든 계수는 인과효과가 아니라 관찰된 변수를 통제한 조건부 연관이다.

**Table 3.** Role of the three regression models

| Question | Dependent variable | Scale | Main model | Reported | Hypothesis |
|---|---|---|---|---|---|
| How many CPC subgroups does the application span within the two classes? | breadth | Count, 1–21 | Negative binomial | IRR | H3a |
| Does the application carry a fabrication-process classification? | has_process | Binary | Binary logit | OR | Supplementary |
| Which co-classification type does the application belong to? | tech_cat | 3 unordered categories | Multinomial logit (base = assembly-only) | RRR | H1, H2, H3b |

## 4. 연구결과

### 4.1 기술통계

기술범위는 평균 5.69개(표준편차 4.07, 범위 1–21)이며 통합형 7.89개, 조립 단독형 4.16개, 공정 단독형 3.15개로 유형에 따라 다르다. 유형별로는 통합형 425건(45.8%), 조립 단독형 338건(36.4%), 공정 단독형 165건(17.8%)이고, 제조공정 기호를 가진 출원은 590건(63.6%)이다. 출원은 2010년 이전에는 드물다가 2010년대 후반에 급증하여 2022년경 정점에 이르며, 2023–2024년의 감소에는 출원 뒤 공개까지의 시차가 영향을 미쳤을 가능성이 크다. Table 4는 출원청별 유형 분포와 기술범위다. 통합형은 EPO, 대만, 일본, 미국에서 절반 안팎으로 가장 많지만 중국과 PCT 경로에서는 40% 수준이고, 공정 단독형의 비중은 중국(24.6%)과 한국(27.6%)에서 높으며, 기술범위 평균은 중국(4.83)과 PCT(5.04)가 가장 좁고 일본(6.88)이 가장 넓다. 다만 일본 출원의 절반은 2011년 이전 출원이고(중앙값 2011.5년) PCT 출원의 중앙값은 2022년이어서, 이 차이가 출원 시기와 범위를 통제한 뒤에도 남는지는 회귀분석으로 확인한다.

**Table 4.** Co-classification type and breadth by filing office (N = 928; row shares in parentheses; SD in parentheses for breadth)

| Office | N | Assembly-only | Integrated | Process-only | H01L21 (%) | Mean breadth (SD) | Median year |
|---|---:|---|---|---|---:|---|---:|
| US | 382 | 148 (38.7%) | 181 (47.4%) | 53 (13.9%) | 61.3 | 5.99 (4.02) | 2020 |
| CN | 175 | 63 (36.0%) | 69 (39.4%) | 43 (24.6%) | 64.0 | 4.83 (3.39) | 2021 |
| WO (PCT) | 113 | 43 (38.1%) | 46 (40.7%) | 24 (21.2%) | 61.9 | 5.04 (3.57) | 2022 |
| EP | 83 | 29 (34.9%) | 44 (53.0%) | 10 (12.0%) | 65.1 | 5.94 (3.75) | 2021 |
| TW | 75 | 24 (32.0%) | 39 (52.0%) | 12 (16.0%) | 68.0 | 6.27 (4.23) | 2021 |
| KR | 58 | 17 (29.3%) | 25 (43.1%) | 16 (27.6%) | 70.7 | 5.84 (4.64) | 2018 |
| JP | 26 | 10 (38.5%) | 13 (50.0%) | 3 (11.5%) | 61.5 | 6.88 (7.00) | 2011.5 |
| Other | 16 | 4 (25.0%) | 8 (50.0%) | 4 (25.0%) | 75.0 | 5.81 (6.23) | 2015.5 |
| Total | 928 | 338 (36.4%) | 425 (45.8%) | 165 (17.8%) | 63.6 | 5.69 (4.07) | 2021 |

### 4.2 기술범위

Table 5는 기술범위의 음이항 추정 결과다. 미국 출원청 기준으로 중국 출원청 출원은 기대 CPC 서브그룹 수가 약 21% 적고(IRR 0.785, p < 0.01, 95% CI 0.695–0.887), PCT 경로 출원은 약 17% 적다(IRR 0.826, p < 0.01). 한국, 대만, EPO는 미국과 다르지 않으며, 일본은 가장 넓지만 10% 수준에서만 유의하다(IRR 1.270, p = 0.07). 이 결과는 H3a를 지지한다. 출원연도의 IRR은 1.015이지만 이 추세는 2010년 이전의 드문 초기 출원에 좌우되어, 2010년 이후 표본만 보면 부호가 반대로 바뀐다(IRR 0.970, p < 0.01). 따라서 범위의 시간 추세에 대해서는 결론을 내리지 않는다. 보완분석인 이항 로짓에서는 세부 분류가 하나 늘 때마다 제조공정 기호를 가질 승산이 약 27% 높아지고(OR 1.267, p < 0.01), 출원연도의 승산비는 1보다 작으며(연간 OR 0.946, p < 0.01), 출원청 가운데는 중국만 미국과 유의하게 달라 제조공정 기호를 가질 승산이 65% 높다(OR 1.650, p < 0.05). 판별력을 나타내는 ROC 곡선 하면적은 0.707이다[38].

**Table 5.** Negative binomial regression of within-class CPC breadth (N = 928; reference office = US; α = 0.268 (SE 0.020); log-likelihood −2,431.5; AIC 4,883.0; standard errors of log coefficients in parentheses; \* p < 0.10, \*\* p < 0.05, \*\*\* p < 0.01)

| | IRR | SE (log) | p |
|---|:---:|:---:|:---:|
| Filing year (yc) | 1.015\*\*\* | (0.003) | <0.001 |
| CN | 0.785\*\*\* | (0.062) | <0.001 |
| KR | 1.018 | (0.094) | 0.853 |
| TW | 1.021 | (0.083) | 0.801 |
| JP | 1.270\* | (0.133) | 0.073 |
| EP | 0.993 | (0.080) | 0.933 |
| WO | 0.826\*\*\* | (0.073) | 0.009 |
| Other | 1.057 | (0.171) | 0.745 |

### 4.3 공동분류 유형

Table 6은 조립 단독형을 기준으로 통합형과 공정 단독형의 상대위험비를 보고하는 다항 로짓이며, Fig. 2는 기술범위와 출원연도를 바꾸었을 때 세 유형의 평균 예측확률이다. 세부 분류가 하나 늘 때마다 조립 단독형 대비 통합형일 상대위험은 40% 높아지고(RRR 1.403, p < 0.01, 95% CI 1.321–1.489) 공정 단독형일 상대위험은 10% 낮아진다(RRR 0.905, p < 0.05). 기술범위가 3인 출원은 통합형으로 예측될 확률이 0.26이지만 8인 출원은 0.68이다. 다만 유형과 기술범위가 같은 CPC 정보에서 만들어지므로 이를 독립적인 인과요인으로 해석하지 않으며, H1은 통합형 특허가 선택된 CPC 영역 안에서 더 다양한 세부 기술을 포함한다는 구성적 특징으로서 지지된다. 공정 단독형의 상대위험은 해마다 약 8.7%씩 떨어지는 반면(RRR 0.913, p < 0.01, 95% CI 0.887–0.940) 통합형의 상대위험은 전 기간의 선형 추세로는 변하지 않는다(RRR 0.993, 비유의). 2010년 출원의 공정 단독형 예측확률은 0.27이지만 2022년 출원은 0.12다. H2는 지지된다.

**Table 6.** Multinomial logit of co-classification type (base = assembly-only; N = 928; reference office = US; log-likelihood −774.5; LR χ²(18) = 367.4, p < 0.001; McFadden pseudo-R² = 0.192; AIC 1,589.0; standard errors of log coefficients in parentheses; \* p < 0.10, \*\* p < 0.05, \*\*\* p < 0.01)

| | Integrated RRR | SE (log) | p | Process-only RRR | SE (log) | p |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| Filing year (yc) | 0.993 | (0.015) | 0.654 | 0.913\*\*\* | (0.015) | <0.001 |
| Breadth | 1.403\*\*\* | (0.031) | <0.001 | 0.905\*\* | (0.048) | 0.039 |
| CN | 1.205 | (0.233) | 0.423 | 2.687\*\*\* | (0.276) | <0.001 |
| KR | 1.166 | (0.381) | 0.687 | 1.906 | (0.419) | 0.124 |
| TW | 1.312 | (0.322) | 0.399 | 2.102\* | (0.407) | 0.068 |
| JP | 1.046 | (0.557) | 0.935 | 0.086\*\*\* | (0.816) | 0.003 |
| EP | 1.307 | (0.291) | 0.358 | 0.858 | (0.438) | 0.726 |
| WO | 1.126 | (0.268) | 0.659 | 1.997\*\* | (0.323) | 0.032 |
| Other | 2.326 | (0.681) | 0.215 | 1.901 | (0.779) | 0.409 |

![](figures/fig_pred_types_ko.png)

**Fig. 2.** Average predicted probabilities of the three types by breadth (left) and filing year (right) (multinomial logit; other covariates at observed values)

중국 출원청 출원은 미국 출원청 출원보다 공정 단독형일 상대위험이 2.7배 높다(RRR 2.687, p < 0.01, 95% CI 1.566–4.611). PCT 경로(RRR 1.997, p < 0.05)도 높고 대만은 10% 수준의 약한 증거만 있다. 반면 통합형 방정식에서는 어느 출원청 계수도 유의하지 않아, 범위와 시점을 통제하면 경계를 넘는 특허의 비중 자체는 출원청 사이에 다르지 않다. 공정 단독형으로 예측되는 평균 확률은 중국 출원청이 4건 중 1건(0.255), 미국 출원청이 7건 중 1건(0.143) 꼴이다. H3b는 기준모형에서 지지되지만 4.4절에서 보듯 최근 출원 구성에 민감하다. 시기별 분포(Table 7)를 보면 공정 단독형의 비중은 2014년 이전 43.6%에서 2015–2019년 14.8%, 2020–2024년 10.4%로 계속 줄었고, 통합형은 31.3%에서 62.2%로 크게 늘었다가 44.3%로 낮아졌으며, 조립 단독형은 25.1%와 23.0%에서 45.4%로 늘었다. 선형 연도 대신 기간 더미를 넣은 다항 로짓에서도 2014년 이전 대비 공정 단독형의 상대위험은 0.273과 0.101(모두 p < 0.01)로 낮아지고, 통합형은 1.707(p = 0.09)로 올랐다가 0.762(비유의)로 내려온다. 통합형이 늘었다가 줄어든 움직임은 가설로 예측한 것이 아니므로 5.1절에서 탐색적 해석으로 다룬다.

**Table 7.** Co-classification type by filing period (N = 928; row shares in parentheses; the first period, 1968–2014, is longer than the other two)

| Period | Assembly-only | Integrated | Process-only | N | H01L21 (%) |
|---|---|---|---|---:|---:|
| –2014 | 45 (25.1%) | 56 (31.3%) | 78 (43.6%) | 179 | 74.9 |
| 2015–2019 | 48 (23.0%) | 130 (62.2%) | 31 (14.8%) | 209 | 77.0 |
| 2020–2024 | 245 (45.4%) | 239 (44.3%) | 56 (10.4%) | 540 | 54.6 |

### 4.4 강건성 분석과 가설 판정

Table 8은 표본과 변수 정의를 바꾸어 다시 추정한 핵심 계수다. 같은 제목의 출원을 한 군집으로 묶은 표준오차에서도 네 계수는 모두 1% 수준에서 유지된다. 제목당 가장 이른 출원 1건만 남긴 표본(N = 473), 조립 단계 공정을 제조공정으로 세지 않는 좁은 정의(N = 908), 무관한 용법 68건을 뺀 표본(N = 860)에서도 핵심 계수는 유지되며, 좁은 정의에서 유형 분포는 조립 단독형 46.8%, 통합형 37.2%, 공정 단독형 16.0%로 바뀌어 통합형이 최다 유형이라는 기술통계는 정의에 따라 달라지지만 회귀 결과는 그대로다. 제목에 *hybrid bonding*이 들어 있는 핵심 표본(N = 467)에서는 범위 → 통합형(1.485)과 중국 → 공정 단독형(7.211), 중국 → 범위(IRR 0.786)가 1% 수준에서 유지되지만, 2015년 이전 출원이 30건뿐이어서 연도 계수는 유의하지 않다(1.029). 2023–2024년을 뺀 표본(N = 753)에서는 범위와 연도의 계수와 중국 출원청의 좁은 범위(IRR 0.808, p < 0.01)가 유지되지만, 중국 출원청의 공정 단독형 RRR은 1.651(p = 0.12)로 유의성을 잃는다. 2023–2024년 중국 출원청 출원 39건 중 18건이 공정 단독형인 반면 미국 출원청은 65건 중 3건에 그치기 때문이다. 보유 클래스마다 최소 1개 기호를 차감한 breadth_adj로 다시 추정하면 통합형의 RRR은 1.403에서 1.262로 줄지만 방향은 유지되며, 이 지표도 유형 정보를 이용하므로 H1의 결정적 증거로 쓰지 않는다. 공정 단독형을 제외한 표본(N = 763)에서 통합형 대 조립 단독형의 이항 로짓을 추정하면 기술범위 OR 1.463, 출원연도 0.987, 중국 출원청 1.177로 다항 로짓 통합형 방정식의 값(1.403, 0.993, 1.205)과 유사하여, 대안 제외에 따른 실질적 해석의 변화는 없다(정식 IIA 검정을 대체하지는 않는다).

**Table 8.** Key coefficients across specifications (multinomial logit RRR, base = assembly-only; reference office = US; last column negative binomial IRR; \* p < 0.10, \*\* p < 0.05, \*\*\* p < 0.01)

| Specification | N | Breadth → Integrated | Year → Process-only | CN → Process-only | CN → Breadth (IRR) |
|---|---:|---|---|---|---|
| Baseline (Tables 5–6) | 928 | 1.403\*\*\* | 0.913\*\*\* | 2.687\*\*\* | 0.785\*\*\* |
| Baseline, title-clustered SE | 928 | 1.403\*\*\* | 0.913\*\*\* | 2.687\*\*\* | 0.785\*\*\* |
| One filing per title | 473 | 1.295\*\*\* | 0.935\*\*\* | 2.721\*\*\* | 0.792\*\*\* |
| Narrow process definition | 908 | 1.352\*\*\* | 0.898\*\*\* | 3.299\*\*\* | — |
| Irrelevant uses excluded | 860 | 1.384\*\*\* | 0.820\*\*\* | 3.235\*\*\* | 0.785\*\*\* |
| Core sample (*hybrid bonding* in title) | 467 | 1.485\*\*\* | 1.029 | 7.211\*\*\* | 0.786\*\*\* |
| 2023–2024 excluded | 753 | 1.418\*\*\* | 0.909\*\*\* | 1.651 | 0.808\*\*\* |

Table 9는 가설 판정이다. H1은 구성적 연관으로서, H2와 H3a는 모든 재추정에서 지지되며, H3b는 기준모형에서 유의하지만 최근 2년을 제외하면 비유의하여 제한적 지지로 판정한다.

**Table 9.** Summary of hypothesis tests (\* p < 0.10, \*\* p < 0.05, \*\*\* p < 0.01; 95% CI from exp(log coefficient ± 1.96 × SE))

| Hypothesis | Prediction | Key evidence | Verdict |
|---|---|---|---|
| H1 | More diverse elements → integrated | RRR 1.403\*\*\* (95% CI 1.321–1.489); 1.295–1.485\*\*\* across re-estimations | Supported (compositional association) |
| H2 | Recent filings → less process-only | RRR 0.913\*\*\* (95% CI 0.887–0.940); period dummies 0.273\*\*\*/0.101\*\*\*; 0.820–0.935\*\*\* across re-estimations (not identified in the core sample) | Supported |
| H3a | CN → narrower breadth | IRR 0.785\*\*\* (95% CI 0.695–0.887); 0.785–0.808\*\*\* across re-estimations | Supported |
| H3b | CN → more process-only | RRR 2.687\*\*\* (95% CI 1.566–4.611); 2.721–7.211\*\*\* across re-estimations, but 1.651 (n.s.) when 2023–2024 are excluded | Limited support |

## 5. 결론

### 5.1 연구결과 요약 및 시사점

본 연구는 기술융합이 한 산업의 공정 단계 사이에서도 일어나는지를 개별 출원 수준에서 물었다. 하이브리드 본딩 관련 출원 928건을 CPC 기호에 따라 조립 단독형, 공정 단독형, 통합형으로 나누고, 어떤 특성의 출원이 두 영역의 분류를 함께 가지는지를 추정하였다. 결과는 견고성에 따라 세 층으로 나뉜다. 가장 견고한 결과는 제조공정과 접합 기술의 공동분류가 45.8%, 보수적 정의로도 37.2%로 예외적인 현상이 아니라는 점, 공정 단독형의 상대위험이 표본과 정의를 어떻게 바꾸어도 최근 출원일수록 낮았다는 점, 그리고 중국 출원청 출원이 미국 출원청 출원보다 두 클래스 안의 CPC 범위가 좁았고 이 차이가 모든 표본에서 유지되었다는 점이다. 보조 결과는 통합형과 높은 기술적 다양성의 연관(통합형의 구성적 특징)과 통합형 비중이 늘었다가 줄어든 시기별 움직임이며, 제한적 결과는 최근 출원 구성에 민감한 중국 출원청의 공정 단독형 편중이다.

이론적으로 이 결과는 세 가지를 시사한다. 첫째, 산업 사이의 일로 다루어져 온 기술융합[11,30]이 한 산업을 가로지르는 경계, 곧 제조공정 영역과 접합 영역 사이에도 적용되며 개별 출원에서 관찰된다. 본 연구는 산업 아키텍처의 변화를 직접 검정하지 않지만, 단계 사이의 인터페이스가 흔들릴 때 발명이 그 양쪽을 함께 담기 시작하는 것은 그 틀이 다시 짜이는 데 선행할 수 있는 기술적 상호의존의 확대이며[1,16], 본 연구는 이를 특허 공동분류로 포착한다. 둘째, 융합의 미시 메커니즘은 개별 발명의 기술적 다양성에 있다. 더 많은 세부 기술에 걸치는 출원이 제조공정과 접합 기호를 함께 가지며, 이는 범위가 가치를 지닌다는 Lerner[27]의 발견 및 Ziedonis[29]의 설명과 맞닿아 있다. 셋째, 융합은 축적이 아니라 재구성으로 진행되었다. 2014년 이전에서 2015–2019년으로 가면서 공정 단독형은 43.6%에서 14.8%로 줄고 통합형은 31.3%에서 62.2%로 늘었으며, 2020–2024년에는 통합형이 44.3%로 낮아지고 조립 단독형이 45.4%로 늘었다. 출원당 접합 계열 기호는 3.03개에서 4.73개로 늘었다가 3.91개로 줄었고 제조공정 계열 기호는 2.12개와 2.23개에서 1.46개로 줄었다. 초기의 공정 발명이 성숙기에는 접합 구조와 함께 분류되고, 이후 발명의 중심이 접합 구조와 적층 자체로 이동한 것으로 읽을 수 있다. 이는 Hacklin et al.[13]의 공진화 논리를 하나의 기술 안에서 관찰한 것에 해당하지만, 접합 코드를 부여하는 심사 관행의 변화와 최근 출원의 분류 미완결 가능성을 배제할 수 없으므로 탐색적 해석으로 제시한다. 출원청 차이는 통합형이 아니라 공정 단독형과 기술범위에서 나타났다. 중국 출원청에 진입한 발명 포트폴리오는 범위가 좁고 특정 제조공정에 집중된 출원이 상대적으로 많아 단계별 추격의 출원 양상[34]과 정합적이지만, 출원인과 패밀리 정보를 통제하지 못하므로 추격 전략의 직접 증거로 볼 수는 없다.

실무적 함의는 자료가 출원인 유형을 구분하지 않으므로 가능성의 수준에서 제시한다. 상당수 출원이 제조공정과 접합 기술을 함께 포함한다는 결과는, 하이브리드 본딩에서는 접합 장비의 운용만으로는 충분하지 않고 표면 평탄화, 오염 제어, 표면 화학, 정렬까지 아우르는 인접 공정 지식이 중요해질 수 있음을 시사하며, 파운드리와 종합반도체기업의 패키징 내재화를 직접 입증하지는 않지만 두 공정 영역을 잇는 지식 범위가 특허 활동에서 중요하다는 점에서 Kapoor와 Adner[44]의 지식 경계 논리와 정합적이다. 정책 면에서 중국 출원청 표본의 좁은 클래스 내 CPC 범위는 견고하게 관찰되었지만, 그 원인이 보조금 정책[32]인지 출원인 구성이나 출원 선택인지는 본 자료로 가릴 수 없다. 차세대 HBM을 하이브리드 본딩에 기대고 있는 한국 메모리 산업에 이 결과가 던지는 시사점은, 패키징 경쟁력이 패키징 노하우 하나에 달려 있지 않고 평탄화, 표면 화학, 정렬 계측 같은 제조공정 역량과 이를 하나의 특허출원에서 통합적으로 보호하는 능력이 함께 필요할 수 있다는 점이다.

본 연구의 기여는 새로운 융합지수를 제안한 데 있지 않다. 공동분류를 개별 출원의 유형으로 재구성하여 특허 수준의 이질성과 관련 요인을 추정할 수 있게 하고, 이를 산업 내 공정 단계 간 융합이라는 새로운 사례에 적용한 데 있다. 이 설계는 두 개의 CPC 클래스와 하나의 분류 규칙, 표준 추정량만으로 이루어져 있어, 디스플레이의 첨단 리소그래피, 배터리의 셀-투-팩 통합처럼 하류 단계가 상류 역량에 의존하기 시작한 다른 공정기술에도 그대로 적용할 수 있다.

### 5.2 한계점과 향후 연구 방향

본 연구의 한계는 여섯 가지다. 첫째, 구성타당성이다. H01L21–H01L24 공동분류는 공정 단계 간 기술융합의 대리측정치이며, H01L21에는 조립 단계의 제조공정도 들어 있고 기술범위는 유형과 같은 CPC 정보에서 만들어진다. 둘째, 표본 선택이다. 제목 키워드 검색은 제목에 관련어가 없는 출원을 놓치고, *direct bonding*은 뜻이 넓어 무관한 용법을 따로 걸러야 했다. 셋째, 관측단위와 중복이다. 공식 패밀리 식별자가 없어 동일 제목으로 근사 조정했으므로 관측치의 독립성은 완전히 보장되지 않는다. 넷째, 누락변수다. 출원인과 기업 유형, 국적, 응용처, 두 클래스 밖의 CPC 기호가 자료에 없으며, PATSTAT의 출원인 정보와 정식 패밀리 식별자, 전체 CPC 기호를 결합하는 것이 다음 단계다. 다섯째, 시간과 공개 시차다. 연도 효과는 선형으로 가정했고 2023–2024년 출원은 불완전하게 관측되며, CPC는 주기적으로 개정되고 과거 출원도 소급 재분류될 수 있어 관찰된 시간 패턴의 일부는 분류 관행의 변화를 반영할 수 있다. 여섯째, 인과성이다. 다양성과 유형은 함께 결정될 가능성이 높고 적절한 도구변수가 없으므로 인과관계는 주장하지 않으며, 출원청 계수는 관할권의 효과가 아니고, 산업 아키텍처와 기업 전략은 직접 검정하지 않는다. 텍스트 기반 방법[25,26]은 분류보다 이른 탐지가 가능하지만 학습된 모형에 의존하므로, 경계가 제도적으로 명확한 본 연구에서는 재현가능성을 택했다. 향후 연구는 여러 공정기술을 같은 설계로 비교하여 여기서 관찰된 재구성의 움직임이 일반적인지 확인하고, 고차 공출현을 보존하는 표현이나 일반성 지수[39]로 하이브리드 본딩의 범용기술적 성격을 직접 검정할 수 있을 것이다.

## References

[1] R. Kapoor, "Persistence of integration in the face of specialization: How firms navigated the winds of disintegration and shaped the architecture of the semiconductor industry", Organization Science, Vol.24, No.4, pp.1195-1213, 2013.
DOI: https://doi.org/10.1287/orsc.1120.0802

[2] J. T. Macher and D. C. Mowery, "Vertical specialization and industry structure in high technology industries", in J. A. C. Baum and A. M. McGahan (Eds.), Business Strategy over the Industry Lifecycle (Advances in Strategic Management, Vol.21), Emerald, Bingley, pp.317-355, 2004.
DOI: https://doi.org/10.1016/S0742-3322(04)21011-7

[3] C. Brown and G. Linden, Chips and Change: How Crisis Reshapes the Semiconductor Industry, MIT Press, Cambridge, MA, 2009.

[4] W. Arden and M. Brillouët and P. Cogez and M. Graef and B. Huizing and R. Mahnkopf, "More-than-Moore" White Paper, White paper, International Technology Roadmap for Semiconductors (ITRS), 2010.

[5] H. N. Khan and D. A. Hounshell and E. R. H. Fuchs, "Science and research policy at the end of Moore's law", Nature Electronics, Vol.1, No.1, pp.14-21, 2018.
DOI: https://doi.org/10.1038/s41928-017-0005-9

[6] J. H. Lau, "State-of-the-art and outlooks of chiplets heterogeneous integration and hybrid bonding", Journal of Microelectronics and Electronic Packaging, Vol.18, No.4, pp.145-160, 2021.
DOI: https://doi.org/10.4071/imaps.1542066

[7] J. H. Lau, "Recent advances and trends in advanced packaging", IEEE Transactions on Components, Packaging and Manufacturing Technology, Vol.12, No.2, pp.228-252, 2022.
DOI: https://doi.org/10.1109/TCPMT.2022.3144461

[8] KnowMade, Hybrid Bonding Patent Landscape Analysis 2024, Industry report, KnowMade (Yole Group), Sophia Antipolis, 2024, https://www.knowmade.com/patent-analytics-services/patent-report/semiconductor-patent-landscape/semiconductor-advanced-packaging-patent-landscape/hybrid-bonding-patent-landscape-analysis-2024/

[9] N. Rosenberg, "Technological change in the machine tool industry, 1840–1910", Journal of Economic History, Vol.23, No.4, pp.414-443, 1963.
DOI: https://doi.org/10.1017/S0022050700109155

[10] F. Kodama, "Technology fusion and the new R&D", Harvard Business Review, Vol.70, No.4, pp.70-78, 1992.

[11] C. S. Curran and J. Leker, "Patent indicators for monitoring convergence – Examples from NFF and ICT", Technological Forecasting and Social Change, Vol.78, No.2, pp.256-273, 2011.
DOI: https://doi.org/10.1016/j.techfore.2010.06.021

[12] N. Sick and S. Bröring, "Exploring the research landscape of convergence from a TIM perspective: A review and research agenda", Technological Forecasting and Social Change, Vol.175, Article 121321, 2022.
DOI: https://doi.org/10.1016/j.techfore.2021.121321

[13] F. Hacklin and C. Marxt and F. Fahrni, "Coevolutionary cycles of convergence: An extrapolation from the ICT industry", Technological Forecasting and Social Change, Vol.76, No.6, pp.723-736, 2009.
DOI: https://doi.org/10.1016/j.techfore.2009.03.003

[14] S. Bröring and L. M. Cloutier and J. Leker, "The front end of innovation in an era of industry convergence: Evidence from nutraceuticals and functional foods", R&D Management, Vol.36, No.5, pp.487-498, 2006.
DOI: https://doi.org/10.1111/j.1467-9310.2006.00449.x

[15] H. J. No and Y. Park, "Trajectory patterns of technology fusion: Trend analysis and taxonomical grouping in nanobiotechnology", Technological Forecasting and Social Change, Vol.77, No.1, pp.63-75, 2010.
DOI: https://doi.org/10.1016/j.techfore.2009.06.006

[16] M. G. Jacobides and T. Knudsen and M. Augier, "Benefiting from innovation: Value creation, value appropriation and the role of industry architectures", Research Policy, Vol.35, No.8, pp.1200-1221, 2006.
DOI: https://doi.org/10.1016/j.respol.2006.09.005

[17] O. Kwon and Y. An and M. Kim and C. Lee, "Anticipating technology-driven industry convergence: Evidence from large-scale patent analysis", Technology Analysis & Strategic Management, Vol.32, No.4, pp.363-378, 2020.
DOI: https://doi.org/10.1080/09537325.2019.1661374

[18] Y. Geum and M. S. Kim and S. Lee, "How industrial convergence happens: A taxonomical approach based on empirical evidences", Technological Forecasting and Social Change, Vol.107, pp.112-120, 2016.
DOI: https://doi.org/10.1016/j.techfore.2016.03.020

[19] A. Gambardella and S. Torrisi, "Does technological convergence imply convergence in markets? Evidence from the electronics industry", Research Policy, Vol.27, No.5, pp.445-463, 1998.
DOI: https://doi.org/10.1016/S0048-7333(98)00062-6

[20] C. S. Curran and S. Bröring and J. Leker, "Anticipating converging industries using publicly available data", Technological Forecasting and Social Change, Vol.77, No.3, pp.385-395, 2010.
DOI: https://doi.org/10.1016/j.techfore.2009.10.002

[21] Y. Cho and M. Kim, "Entropy and gravity concepts as new methodological indexes to investigate technological convergence: Patent network-based approach", PLOS ONE, Vol.9, No.6, Article e98009, 2014.
DOI: https://doi.org/10.1371/journal.pone.0098009

[22] W. S. Lee and E. J. Han and S. Y. Sohn, "Predicting the pattern of technology convergence using big-data technology on large-scale triadic patents", Technological Forecasting and Social Change, Vol.100, pp.317-329, 2015.
DOI: https://doi.org/10.1016/j.techfore.2015.07.022

[23] S. Jeong and J. C. Kim and J. Y. Choi, "Technology convergence: What developmental stage are we in?", Scientometrics, Vol.104, No.3, pp.841-871, 2015.
DOI: https://doi.org/10.1007/s11192-015-1606-6

[24] C. H. Song and D. Elvers and J. Leker, "Anticipation of converging technology areas — A refined approach for the identification of attractive fields of innovation", Technological Forecasting and Social Change, Vol.116, pp.98-115, 2017.
DOI: https://doi.org/10.1016/j.techfore.2016.11.001

[25] N. Preschitschek and H. Niemann and J. Leker and M. G. Moehrle, "Anticipating industry convergence: Semantic analyses vs IPC co-classification analyses of patents", Foresight, Vol.15, No.6, pp.446-464, 2013.
DOI: https://doi.org/10.1108/FS-10-2012-0075

[26] C. Zhu and K. Motohashi, "Identifying the technology convergence using patent text information: A graph convolutional networks (GCN)-based approach", Technological Forecasting and Social Change, Vol.176, Article 121477, 2022.
DOI: https://doi.org/10.1016/j.techfore.2022.121477

[27] J. Lerner, "The importance of patent scope: An empirical analysis", RAND Journal of Economics, Vol.25, No.2, pp.319-333, 1994.
DOI: https://doi.org/10.2307/2555833

[28] B. H. Hall and R. H. Ziedonis, "The patent paradox revisited: An empirical study of patenting in the U.S. semiconductor industry, 1979–1995", RAND Journal of Economics, Vol.32, No.1, pp.101-128, 2001.
DOI: https://doi.org/10.2307/2696400

[29] R. H. Ziedonis, "Don't fence me in: Fragmented markets for technology and the patent acquisition strategies of firms", Management Science, Vol.50, No.6, pp.804-820, 2004.
DOI: https://doi.org/10.1287/mnsc.1040.0208

[30] F. Hacklin and C. Marxt and F. Fahrni, "An evolutionary perspective on convergence: Inducing a stage model of inter-industry innovation", International Journal of Technology Management, Vol.49, No.1–3, pp.220-249, 2010.
DOI: https://doi.org/10.1504/IJTM.2010.029419

[31] A. G. Hu and G. H. Jefferson, "A great wall of patents: What is behind China's recent patent explosion?", Journal of Development Economics, Vol.90, No.1, pp.57-68, 2009.
DOI: https://doi.org/10.1016/j.jdeveco.2008.11.004

[32] J. Dang and K. Motohashi, "Patent statistics: A good indicator for innovation in China? Patent subsidy program impacts on patent quality", China Economic Review, Vol.35, pp.137-155, 2015.
DOI: https://doi.org/10.1016/j.chieco.2015.03.012

[33] S. Grimes and D. Du, "China's emerging role in the global semiconductor value chain", Telecommunications Policy, Vol.46, No.2, Article 101959, 2022.
DOI: https://doi.org/10.1016/j.telpol.2020.101959

[34] K. Lee and C. Lim, "Technological regimes, catching-up and leapfrogging: Findings from the Korean industries", Research Policy, Vol.30, No.3, pp.459-483, 2001.
DOI: https://doi.org/10.1016/S0048-7333(00)00088-3

[35] European Patent Office, PATSTAT Global (2025 edition), Database, EPO, Vienna, 2025.

[36] A. C. Cameron and P. K. Trivedi, Regression Analysis of Count Data, 2nd ed., Cambridge University Press, Cambridge, 2013.

[37] D. McFadden, "Conditional logit analysis of qualitative choice behavior", in P. Zarembka (Ed.), Frontiers in Econometrics, Academic Press, New York, pp.105-142, 1974.

[38] D. W. Hosmer and S. Lemeshow and R. X. Sturdivant, Applied Logistic Regression, 3rd ed., Wiley, Hoboken, NJ, 2013.

[39] S. Petralia, "Mapping general purpose technologies with patent data", Research Policy, Vol.49, No.7, Article 104013, 2020.
DOI: https://doi.org/10.1016/j.respol.2020.104013

[40] M. Karvonen and T. Kässi, "Industry convergence analysis with patent citations in changing value systems", International Journal of Business and Systems Research, Vol.6, No.2, pp.150-175, 2012.
DOI: https://doi.org/10.1504/IJBSR.2012.046353

[41] F. Caviggioli, "Technology fusion: Identification and analysis of the drivers of technology convergence using patent data", Technovation, Vol.55–56, pp.22-32, 2016.
DOI: https://doi.org/10.1016/j.technovation.2016.04.003

[42] I. Hwang, "The effect of collaborative innovation on ICT-based technological convergence: A patent-based analysis", PLOS ONE, Vol.15, No.2, Article e0228616, 2020.
DOI: https://doi.org/10.1371/journal.pone.0228616

[43] R. N. Langlois and W. E. Steinmueller, "The evolution of competitive advantage in the worldwide semiconductor industry, 1947–1996", in D. C. Mowery and R. R. Nelson (Eds.), Sources of Industrial Leadership: Studies of Seven Industries, Cambridge University Press, Cambridge, pp.19-78, 1999.

[44] R. Kapoor and R. Adner, "What firms make vs. what they know: How firms' production and knowledge boundaries affect competitive advantage in the face of technological change", Organization Science, Vol.23, No.5, pp.1227-1248, 2012.
DOI: https://doi.org/10.1287/orsc.1110.0686
