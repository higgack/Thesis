"""체화 노트 (Study Notes) — 개인 학습 노트 + 되새김질(SRS) 엔진.

검색형 위키(`src/store/wiki.py`)와 분리된 별도 서브시스템. 별도 학습
TG 채널의 자료를 풍부한 노트로 재구성하고, spaced-repetition 복습
스케줄 + active-recall 질문으로 "체화"를 돕는다.

레이아웃 (docs/notes_mvp_design.md):
  store.py   노트 vault(md) + notes.db (atomic + WAL)
  srs.py     SM-2 lite 복습 스케줄 (순수 함수)
  synth.py   LLM 노트 합성 (파싱 결과 → 노트 + 복습질문)
  recall.py  복습 큐 / 자가평가 → 주기 갱신
  channel.py 오케스트레이션 (소스 로드 → 합성 → 저장)
"""
