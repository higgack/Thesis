# OpenKB — 문서→위키형 지식베이스(경량, 벡터리스) (optional)

[OpenKB](https://github.com/VectifyAI/OpenKB) (Apache-2.0)은 원문서(PDF/Word/MD/PPT/HTML/
Excel/URL 등)를 LLM으로 **한 번 컴파일**해 요약·개념페이지·엔티티페이지·상호링크를 가진
**위키형 지식베이스**로 만드는 도구다. 핵심 특징은 **벡터DB가 없다**는 점(PageIndex 트리
검색) — markitdown 변환, LiteLLM 멀티프로바이더, CLI(watchdog).

## 왜 우리 프로젝트에 (경량 대안)

우리 `rag/`(RAG-Anything)는 MinerU→PyTorch→CUDA로 **설치가 무겁고**(telegram-bot에서
디스크 부족으로 실패한 전례), OpenKB는 **벡터DB·GPU가 불필요**해 작은 머신에서도 돌릴 수
있는 **경량 문헌 KB 대안**이다. 또한 "쿼리마다 재색인" 대신 **영속적 위키**로 지식이 쌓여,
참고문헌을 반복 참조하는 논문 작업에 맞고 자동 생성 개념/엔티티 페이지는 **문헌 리뷰 뼈대**로
쓸 수 있다.

> 트레이드오프: 표·수식 등 **멀티모달 정밀 파싱**은 RAG-Anything이 우위. 멀티모달이 중요하면
> `rag/`, 경량·영속 위키가 중요하면 OpenKB를 택한다(둘 병행도 가능).

## 설치·사용 (vendor 아님 — pip 의존성)

```bash
pip install openkb          # 정확한 패키지명/명령은 업스트림 README 확인
export OPENAI_API_KEY=...   # LiteLLM 형식 키
# 참고 PDF를 한 폴더에 모아 컴파일 → 위키 생성 → 질의/채팅
openkb build rag/sources/   # (명령 형식은 버전에 따라 다를 수 있음)
openkb chat "하이브리드 본딩 관련 핵심 선행연구는?"
```

생성된 KB 디렉터리는 `rag/openkb/`(git-ignored)에 두기를 권장한다.

## 비고
- 라이선스 Apache-2.0, 2.6k★.
- 우리 `rag/sources/`의 PDF를 그대로 입력으로 쓸 수 있어 도입 마찰이 작다.
- 어느 도구든 산출 요약/주장은 논문 반영 전 **원문 대조(무결성 게이트)** 필수.
