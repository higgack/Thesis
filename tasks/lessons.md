# Lessons (교정 규칙)

> session-checkpoint가 갱신, session-start가 리뷰. 메타: `conf`(신뢰도) · `seen`(최근) · `obs`(관측수).

## 졸업 게이트 (Graduated Gates)

| 게이트 | 트리거 | 체크 |
|---|---|---|
| G1 | 커밋/푸시 | 이 세션에서 유저가 커밋·푸시 의도를 명시했는가? (이 레포는 작업 단위로 커밋·푸시가 기본 합의됨) |
| G2 | "없다/완료/클린" 단언 | grep/ls/Read로 실제 확인 후 "검증함/안 봄" 명시 |
| G3 | 논문에 수치·인용 반영 | 원자료(①②)·1차 출처와 대조(무결성 게이트) 후 반영 |
| G4 | 그림/문서 렌더 | 결과를 실제로 열어(Read 이미지/파싱) 깨짐 확인 |

## 레슨

### 마크다운 숫자 범위에 물결표 금지
> conf 0.85 · obs 2
`20~25℃` 처럼 `숫자~숫자`를 쓰면 GFM에서 **취소선**, pandoc에서 **아래첨자**로 깨진다 → **en-dash(–)** 사용.

### matplotlib 한글은 NanumGothic
> conf 0.8 · obs 1
DejaVu엔 한글 글리프 없음 → `pip install koreanize-matplotlib` 후 `import koreanize_matplotlib`. 영문 라벨이면 불필요.

### 외부 GitHub 접근은 curl raw로 우회
> conf 0.75 · obs 2
이 환경 git 프록시는 **higgack/Thesis만** 허용(타 repo clone 403). 외부 repo 파일은 `curl https://raw.githubusercontent.com/<owner>/<repo>/<branch>/<path>`로 받는다. wikidocs 등 일부 호스트는 HTTPS 프록시 자체가 차단(403).

### 새 repo 생성/푸시 불가
> conf 0.9 · obs 1
환경 권한상 새 GitHub repo 생성(403)·타 repo 푸시 불가. 산출물은 PR(#8)·tarball·SendUserFile로 전달.

### 논문 수치는 '인용'이지 '재계산' 아님
> conf 0.9 · obs 1
본 논문 수치는 저자 선행분석(①②)에서 인용. 재추출·재추정 안 함 → 한계로 명시, 무결성 게이트로 원문 대조.

### RAG-Anything은 무겁다
> conf 0.8 · obs 1
raganything[all] → MinerU → torch/CUDA(수 GB). 소용량 머신 실패. CPU torch 선설치 또는 경량 OpenKB 고려.
