# Hyper-Extract — 텍스트→하이퍼그래프 등 구조 추출 (optional, 채택)

> 대상: [yifanfeng97/Hyper-Extract](https://github.com/yifanfeng97/Hyper-Extract) (Apache-2.0) —
> 비정형 텍스트를 **8종 지식구조**(리스트 ~ **하이퍼그래프**, 시공간 그래프, Pydantic 모델)로 추출하는
> LLM CLI. 10+ 추출 알고리즘, **80+ YAML 도메인 템플릿**, PDF/MD 입력, 시맨틱 검색, Obsidian 내보내기,
> MCP. LLM: OpenAI/Anthropic/로컬 vLLM.

## 왜 우리 프로젝트에 (kg-gen 상위호환)
우리가 먼저 채택한 [kg-gen](kg-gen.md)은 텍스트→**pairwise 지식그래프**다. Hyper-Extract는 한발 더 나아가
**하이퍼그래프**(다대다 관계)를 직접 추출한다. 핵심은 우리 논문의 **CPC 동시분류**가 **본질적으로
하이퍼그래프**라는 점이다 — 한 특허가 여러 CPC를 *동시에* 보유하므로, 이를 노드쌍(pairwise)으로 쪼개면
다중 공출현 정보가 손실된다. Hyper-Extract의 하이퍼엣지는 "한 특허 = {CPC들}의 동시 묶음"을 그대로 표현해
**§4.5 기술 네트워크/클러스터를 더 충실히** 모델링한다.

또한 **YAML 템플릿 + Pydantic 스키마**로 추출을 통제·재현할 수 있어, 자유형 LLM 추출보다 **무결성에 유리**
(원문 대조 기준선으로도 적합). PDF는 `rag/sources/`를 그대로 입력.

## 위치
- `rag/` 검색(RAG) · `kg-gen` 단순 KG · **Hyper-Extract = 구조(하이퍼그래프) 추출**. 셋 다 보완재.
- 우리 기본 권장: **네트워크/문헌종합 구조화는 Hyper-Extract**, 가벼운 1회 KG는 kg-gen.

## 사용 개요 (LLM 키 필요)
```bash
pip install hyper-extract        # 정확한 패키지명/명령은 업스트림 README 확인
export OPENAI_API_KEY=...         # 또는 ANTHROPIC / 로컬 vLLM 엔드포인트
# YAML 도메인 템플릿(특허/CPC용) 선택 → PDF/텍스트 입력 → 하이퍼그래프 추출
# 입력: rag/sources/*.txt  → 산출: rag/hyper_extract_output/ (git-ignored)
```

## 주의
- LLM 추출은 **환각 가능** → 추출 하이퍼엣지/엔티티를 원문(①②)과 **무결성 게이트로 대조** 후 논문 반영.
- 우리의 결정론 KG(`manuscript/figures/tech_network*`)는 이 자동추출의 **검증 기준선(ground truth)** 으로 사용.
- 라이선스 Apache-2.0.
