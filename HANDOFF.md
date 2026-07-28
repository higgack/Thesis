# 세션 핸드오프 — 2026-06-04

이 문서는 직전 작업 세션의 맥락을 다음 세션으로 넘기기 위한 요약이다.
**코드 변경은 전부 git에 push 완료** (브랜치 `claude/personal-rag-knowledge-base-sLSvV`).
이 문서는 "무엇을 왜 했는지"만 담는다 — 코드 자체는 repo가 이미 갖고 있다.

읽는 순서: `CLAUDE.md`(standing 규칙, 최우선) → 이 문서 → 필요 시 `git log`.

---

## 이번 세션에서 끝낸 일 (커밋순, 최신 위)

| 커밋 | 내용 | 한 줄 이유 |
|---|---|---|
| 751dd1c | **CPU 100% 24/7 버그 수정** | 대시보드 regenerate가 60초마다 카드 source마다 `meta.search_title` LIKE 풀스캔(N+1) → 워커 스레드 1코어 영구 점유. `title_url_map()` 1회 스캔 + dict 조회로 교체. py-spy로 범인 확정. |
| 05b404a | `(빈 결과:…)` 노이즈 억제 | URL 없고 80자 미만인 짧은 알림이 창고로 흘러들어오면 학습거리 0 → 빈 결과 메시지만 남던 것. 이제 조용히 skip. |
| b477f3b | 링크 "전체 학습" 병렬화 | 순차 ingest라 N개면 N×수분, 침묵 → "안 되는 듯". 4-wide gather + 시작 알림. |
| df45e89 | yt-dlp 알림 문구 수정 | 실패 원인이 nightly가 아니라 **GCP IP 차단**("not a bot")인 경우가 흔함 → 재배포 무의미. 문구를 IP차단 우선으로. |
| 7df53ba | insidertracking 제목필터 forward | "미국 레딧 게시물 분석" 글만 원본 forward, 나머지 drop. `_TITLE_FILTER_CHANNELS`. |
| 80fb34c | FORWARD_TARGET `-100<id>` resolve | 창고 비공개 전환 위해 username→chat_id로 바꿨더니 listener가 못 찾음. PeerChannel wrap 추가. |
| 8cf406f | **scripts/preflight.sh 추가** | push 전 자동 검증(AST/F821/help cap/handler). 만들다가 `cmd_youtube_restub_rescan`의 `_html` 미import latent 버그도 잡아 고침. |
| 3d7d4e0 | get_entities_text() | digest 본문 emoji(UTF-16 surrogate)로 라벨 offset 깨져 "3.", "MTB M"처럼 잘림. |
| b638f9c | 대시보드 월별 접기 | day-section을 month-section으로 감쌈. |
| 2c47bd1 | yt-dlp health 의미 재정의 | "자막 없는 영상"을 yt-dlp 실패로 카운트하던 것 → extract_info 성공이면 healthy. 가짜 100% 알림 원인. |
| e302097 | notify_acks 메시지 갱신 | 코드 알림 문구 바꿔도 ack store에 옛 문구 박제돼 계속 나가던 것. message 바뀌면 갱신. |
| (그 외) | getfeed/benineb9 URL-only 통합, 제목 prefix+og:title, placeholder 제목 복구, PDF 파일명 우선 등 | git log 참조 |

---

## 진행 중 / 미해결 (다음 세션이 알아야 할 것)

1. **VM 다운사이즈** — 사용자가 직접 진행함("내가 했어"). CPU 버그 수정 후 안전해져서 n2-standard-4 → n2-standard-2 가능. 이미 처리됨, 추가 작업 불필요.

2. **Gemini 결제** — 선불 크레딧 0 소진으로 429 발생했었음. 사용자가 ₩충전 + **자동충전 ₩10만** 설정함. 해결됨.

3. **GCP Gemini 청구 갭** — 전체 청구 ₩257K vs thesis 몫 ₩87.5K. 나머지 ₩127K는 thesis 밖 워크로드(stock/standardview 등) 또는 다른 SA. thesis 책임 아님. 추적은 NOAH(다른 채널) 담당.

4. **YouTube ingest** — GCP IP가 YouTube에 "not a bot" 차단당함(구조적). yt-dlp/Deno/nightly는 정상. 코드로 못 고침 — 며칠 단위로 자동 해제되거나, 중요 영상은 자막 수동 복붙. 알림 문구는 df45e89로 정확해짐.

---

## 비용/리소스 실측 스냅샷 (2026-06-04)

- Gemini: **₩87,571/월** (flash-lite 74% · embed 20% · flash 4% · pro 2%) · ingest가 95.6%
- bot RSS: 4.3GB / VM 16GB · CPU: **버그 수정 후 idle ~10%** (이전 100% 고정)
- Chroma 청크: **226,194** (BM25는 50k 게이트로 비활성)
- 디스크: 73% (BGE-M3 미사용 캐시 삭제로 회수)
- 스레드: 32개(정상, 누수 아님)

---

## 작업 규칙 리마인더 (CLAUDE.md에 상세, 여기 핵심만)

- **커밋/푸시는 트리거 단어**("푸시"/"커밋"/"배포" 등)가 사용자 최신 메시지에 있을 때만. 이전 허락 이월 안 됨.
- **push 전 `bash scripts/preflight.sh`** 돌릴 것 (이번 세션에 추가됨).
- `_HELP_TEXT` ≤ 4000자. 명령 추가/모델 변경 시 help + 가이드 상수 4종 같이 갱신.
- 자동 포워드 채널 모드: digest expand / PLAIN strip / PLAIN full / URL-only / **title-filter**(신규) / drop.
- 브랜치: `claude/personal-rag-knowledge-base-sLSvV`. PR #1 존재.

---

## 다음 세션 시작 방법

1. 이 repo(`~/Thesis`)에서 새 세션 시작 → CLAUDE.md 자동 로드
2. 이 문서(`HANDOFF.md`) 읽으라고 한 줄 지시
3. 끝. 대화 히스토리는 안 가져와도 됨 (코드는 git, 맥락은 이 문서).
