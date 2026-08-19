# kg-gen — 텍스트→지식그래프 추출 (optional)

[kg-gen](https://github.com/stair-lab/kg-gen) (MIT, NeurIPS'25 · arXiv:2502.09956)은 평문
텍스트에서 **엔티티·관계를 추출해 지식그래프(KG)** 를 만드는 도구다. DSPy 구조화 출력,
LiteLLM 멀티모델, MCP 서버, 그래프 병합·클러스터링·시각화를 지원한다.

우리 프로젝트에서의 쓰임은 **검색(RAG)이 아니라 구조 추출**이다. `rag/`(RAG-Anything)와
경쟁이 아니라 **보완재**이며, 다음에 유용하다.

- 논문의 **'기술 클러스터·기술 네트워크' 절** 보조 — 특허 초록/청구항·선행연구 텍스트에서
  개념-관계 그래프를 뽑아 **의미 기반 기술 네트워크**를 구성(자료②가 향후과제로 제시한
  "텍스트마이닝 기반 특허 네트워크"와 부합).
- 여러 문헌을 KG로 합쳐 **개념 지도·연구 갭** 도출(문헌 종합 단계).

## 설치 (vendor 아님 — pip 의존성)

```bash
pip install kg-gen          # 정확한 패키지명/사용법은 업스트림 README 확인
# LLM 키 필요 (LiteLLM 형식): 예 OPENAI_API_KEY
```

## 최소 사용 예 (개념)

```python
from kg_gen import KGGen                      # API는 버전에 따라 다를 수 있음
kg = KGGen(model="openai/gpt-4o-mini")        # LiteLLM 모델 문자열
graph = kg.generate(input_data=open("rag/sources/01_..._EN.txt").read())
# graph: entities + relations  → 시각화/병합/클러스터링
```

> 우리 코퍼스는 이미 `rag/sources/*.txt`(추출 텍스트)로 있으므로, 이를 입력으로 바로 KG를
> 뽑을 수 있다. 산출 KG는 `rag/kg_output/`(git-ignored)에 저장 권장.

## 우리 워크플로 연계
1. `triage.py`로 PDF 선별 → 텍스트 확보
2. kg-gen으로 문헌·특허 KG 추출
3. KG를 논문의 기술 네트워크 절 근거/그림 초안으로 활용
4. 모든 추출 주장은 academic skills의 무결성 게이트로 검증(LLM 추출은 환각 가능 — 원문 대조 필수)

## 비고
- 라이선스 MIT. 논문 인용 가능(KGGen, arXiv:2502.09956).
- LLM 추출 KG는 **검증 대상**: 엔티티·관계를 원문과 대조한 뒤 논문에 반영할 것.
