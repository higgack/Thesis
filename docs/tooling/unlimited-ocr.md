# Unlimited-OCR — 장문 PDF one-shot OCR (optional, GPU 전제)

[Unlimited-OCR](https://huggingface.co/baidu/Unlimited-OCR) (Baidu, arXiv:2606.23050)은
긴 문서를 **페이지 단위로 쪼개지 않고 한 번에(one-shot, long-horizon)** 텍스트/마크다운으로
변환하는 OCR·문서파싱 모델이다. 핵심은 **R-SWA(Reference Sliding Window Attention)** — 원본
이미지는 항상 전체 참조(Global Reference)하되 모델이 생성한 텍스트는 최근 일부(예: 128토큰)만
기억(Local Generation)해, KV 캐시가 문서 길이에 비례(O(N))해 터지던 메모리 병목을 제거한다.
"OCR의 정답 근거는 이미지이지 방금 쓴 글자가 아니다"라는 작업 특성을 어텐션 구조에 반영한 묘수.

## 우리 프로젝트에서의 위치

`rag/`(RAG-Anything)의 **MinerU 파서**, 그리고 `rag/triage.py`가 **`OCR_NEEDED`** 로 분류한
페이지의 **OCR 백엔드**와 같은 자리다(대체/보완재). 강점은 **페이지 경계 문맥 보존**(두 페이지에
걸친 표·문장이 안 끊김 — 특허 표에 유리)과 **PyMuPDF로 PDF→이미지**(우리 스택과 동일).

> 철학적 연결: `triage.py`가 "싼 신호로 비싼 OCR을 회피"하듯, Unlimited-OCR은 "작업 성질을 알면
> 트레이드오프 한쪽을 거의 공짜로" 만든다. 둘을 합치면 **triage로 OCR 대상 페이지만 골라 →
> Unlimited-OCR로 장문 일괄 파싱**이라는 파이프라인이 자연스럽다.

## 도입 조건 / 제약 (중요)

1. **NVIDIA GPU 필수.** `torch.cuda`·CUDA 12.9·bfloat16·`.cuda()`. CPU/소용량 머신(이 워크스페이스의
   기본 환경, telegram-bot 등)에서는 **실행 불가** — GPU 박스에서만.
2. **검증 부재·환각 위험.** 공개 벤치마크 수치가 아직 없고, OCR 고질병인 "없는 내용 지어내기·임의
   영어 번역"을 이 구조가 막는지 미확인. **특허/CPC 코드·통계 수치가 정확해야 하는 논문엔 위험** →
   결과는 반드시 academic skills의 **무결성 게이트로 원문 대조**.
3. **현재 코퍼스엔 대체로 불필요.** 우리 원본(①②)은 native-text PDF라 `triage.py`가 **TEXT_ONLY** 로
   분류 → pymupdf 텍스트 추출로 충분하고 OCR 단계가 아예 안 걸린다.

## 언제 쓰나 (우선순위: 낮음~중간, 조건부)

- ❶ **스캔/이미지-only 특허**(구형 특허·도면 위주)를 다뤄야 하고 ❷ **GPU**가 있을 때 →
  `triage.py`의 `OCR_NEEDED` 버킷 엔진으로 **MinerU 대안**. 그 외에는 보류.

## 실행 개요 (GPU 환경에서)

```bash
# HuggingFace Transformers 경로 (torch 2.10 / CUDA 12.9)
pip install torch torchvision transformers Pillow einops addict easydict pymupdf psutil
```
```python
from transformers import AutoModel, AutoTokenizer
import torch
m = "baidu/Unlimited-OCR"
tok = AutoTokenizer.from_pretrained(m, trust_remote_code=True)
model = AutoModel.from_pretrained(m, trust_remote_code=True, use_safetensors=True,
                                  torch_dtype=torch.bfloat16).eval().cuda()
# 다중 페이지/PDF: base 설정(image_size=1024). PDF는 PyMuPDF로 페이지→이미지 후 infer_multi.
```
- 단일 이미지: `gundam`(crop) / `base` 설정. 다중·PDF: `base`.
- `no_repeat_ngram_size`·`ngram_window`로 **구절 무한 반복** 방지(OCR 흔한 실패).
- 고성능 서빙은 **SGLang**(`sglang.launch_server`, OpenAI 호환 API, 커스텀 logit processor로 반복 방지).
- 모델: HuggingFace / ModelScope. 논문: arXiv:2606.23050.

> 라이선스·정확도는 도입 전 업스트림에서 재확인할 것. 우리 워크플로에선 **triage 선별 + 무결성 대조**를
> 전제로만 사용한다.
