# Swallowed commands: GIL starvation and the _W_LOCK convoy

Moved VERBATIM out of `AGENTS.md` (`## 씹힘(swallowed commands)`) on 2026-10-18 — text as of commit bf28456 — so the always-loaded rules file stays short. `AGENTS.md` keeps the rules and links here; nothing below was reworded. The investigation spans 2026-09-10..18; the condensed rules are in AGENTS.md.

---


`data/loop_stalls.log` 10건(2026-09-10~16, 25~46초)을 읽고 나온 결론.
**10건 전부 루프 자체는 정상 위치**(`selector.poll`·SSL handshake·
`sock.send`·aiohttp 파싱)에 있었다. 루프가 막힌 게 아니라 **GIL을 못 받은
것**이다. 그래서 CPU는 90% 밑, heartbeat는 신선 → **워치독 둘 다 안 운다.**
「stale heartbeat = 루프에 동기 작업」 규칙(Auto-deploy 섹션)은 여전히
맞지만, **씹힘은 그 규칙으로 안 잡히는 별개 증상**이다. 스택 덤프부터 볼 것.

- **GIL을 붙잡고 있던 워커**(덤프에 RUNNING으로 찍힌 것): PyMuPDF 표 추출
  (`table.py`, `fz_new_display_list_from_page` — SWIG C는 GIL을 안 놓는다)
  · tiktoken `_core_bpe.encode` · sqlite `fetchall()` · KG의
  `[dict(r) for r in rows]` · `fts_upsert`의 `executemany(DELETE …)`.
  **I/O가 아니라 전부 CPU/GIL 작업이다** — `_widen_thread_pool`의
  "this work is I/O-bound and releases the GIL"이라는 근거는 이 덤프들이
  반증한다. 그래도 **풀을 24에서 줄이지 말 것**: 2026-08-28 사고(명령이
  ingest 뒤에 큐잉)가 그걸 넓힌 이유고, 줄이면 그 사고가 돌아온다.
- **`sys.setswitchinterval` 기본 5ms → 1ms** (`GIL_SWITCH_INTERVAL`로
  override 가능). 여기서 실측: GIL 점유 워커 12개 + 50ms마다 깨는 루프 →
  추가 지연 중앙값 36.9ms(5ms) → 19.7ms(2ms) → **9.1ms(1ms)**, 최대
  146 → 103 → **67ms**. **완치가 아니라 완화다** — MuPDF 호출 하나는
  여전히 통째로 GIL을 쥔다(그걸 막는 게 `loaders.py`의 `_PDF_EXTRACT_SEM`).
  되돌리려면 그 머신에서 같은 실측을 먼저 하고 판단할 것.
- **대시보드 `regenerate()`가 상습범이다.** 최근 3건 중 2건에서 RUNNING
  워커가 이것이었다: `title_url_map().fetchall()`(2026-09-16 40초 덤프의
  유일한 앱 스레드) · `kg.top_entities()` · `kg.date_histogram()`.
  `title_url_map`은 이제 `meta.documents_signature()`(행수 + 최신
  `ingested_at`)로 게이트한다 — **행수만 보면 안 된다**: 재인제스트는
  행수를 그대로 둔 채 URL만 바꾼다(스크래치 DB로 재현 확인).
- **하지만 ≥25초 스톨은 늘지 않고 줄고 있다** (2026-09-16 실측, 날짜별
  집계 22일치): 8월 말 ~3.3건/일 → 9/09~16 ~1.6건/일. 사용자는 같은
  기간에 씹힘이 **더 잦아졌다**고 한다. 두 사실이 같이 참이려면 **느끼는
  스톨이 로그에 남는 것보다 짧다**는 뜻이다. 그래서 감지 임계를 25→10초,
  폴링을 15→3초로 내렸다(`STALL_DUMP_AFTER_SEC`·`STALL_POLL_SEC`).
  근거는 이 파일이 아니라 코드 주석에 이미 있었다 — **텔레그램 응답은
  `HTTPXRequest`의 15초 connect timeout에서 포기**하므로 답을 잃는 구간은
  10~25초인데, 임계 25초 + 15초 폴링은 그 구간을 통째로 못 봤다.
  쿨다운 5분은 그대로 — 임계를 내려도 파일이 넘치지 않게 잡아주는 게
  그것이다(최악 288건/일 ≈ 0.9MB, 회전 한계 2MB).
  **누적 건수로 판단하지 말 것**: `grep -c`는 63이지만 그건 22일치 합이고,
  내가 처음 "10건"이라고 한 것은 `tail -c 40000`(파일 끝 40KB)만 본
  표본이었다. 항상 날짜별로 쪼개서 **하루 몇 건**인지로 볼 것.
- **범인은 `meta.py` `_W_LOCK` 컨보이로 확정됐다** (2026-09-17, 임계를
  10초로 내린 첫날 27건 집계). 임계 25초 시절엔 하루 1~2건이던 것이
  **27건** — 지속시간이 10·11·11·11·12·12·14·17·23·24·25초에 몰려 있고
  25초 넘는 건 34s·45s 둘뿐이다. **그동안 못 본 구간에 문제가 다 있었다.**
  - **BLOCKED 88 vs RUNNING 43** — 2:1로 락 대기다. GIL 기아가 아니라
    **락 컨보이**가 주범이라는 뜻이고, 둘은 대응이 정반대다.
  - 앱 프레임 빈도: `_wconn` **90회**(= `with _W_LOCK`) · `fts_upsert` 43
    · `ocr_cache_put` 20 · `summary_cache_remember` 18 · `upsert_doc` 10
    · `chunk_embed_remember` 10 · `regenerate` 12. 라이브러리는
    telegram 60 · pymupdf 30 · chromadb 22.
  - **`ocr_cache_put` 이 새로 드러난 얼굴이다.** 페이지 한 장의 OCR
    결과를 한 행씩 INSERT 하는데, 그게 per-PDF `ThreadPoolExecutor
    (max_workers=7)` 안에서 돌아 PDF 3개면 최대 21개 스레드가 **전역**
    `_W_LOCK` 을 한 행 쓰자고 번갈아 잡는다. 한 문서분을 모아 한 번에
    쓰는 것이 자연스러운 모양이다.
  - **진짜 원인은 `chunk_fts` 의 `cid UNINDEXED` 였다** (2026-09-17 측정
    후 확정). FTS5 는 `UNINDEXED` 열에 어떤 인덱스도 만들지 않으므로
    `WHERE cid=?` 는 **전체 스캔**이다. 그런데 `fts_upsert` 가 청크마다
    그 DELETE 를 돌렸다 — 문서 하나(수백 청크)를 넣을 때마다 67만 행
    테이블을 수백 번, 그것도 전역 `_W_LOCK` 을 쥔 채로.
    - 라이브 확인: `cid` 단건 조회 200회가 **끝나지 않는다**(머신에서
      직접 재보다 중단). `chunk_fts` 673,664행 · `meta.db` 4.8GB.
    - 스크래치 DB 20만 행 실측: `DELETE WHERE cid=?` 건당 **60.4ms** →
      rowid 방식 **2.0ms (31배)**, 문서 단위 삭제 72ms → 3ms. 67만 행이면
      격차가 더 크고, 300청크 문서 하나가 락을 수십 초 쥔다 — 관측된
      10~45초 스톨과 일치한다.
    - **2026-08 의 "3일간 CPU 100%, `_wconn` 경합(heavy `fts_upsert`)"
      도 같은 원인이었다.** 그때는 원인을 못 찾고 CPU 워치독을 추가하는
      것으로 덮었다.
  - **고침: `chunk_fts_map(cid PRIMARY KEY, rid, doc_id)`** (2026-09-17).
    rowid 는 FTS5 의 진짜 키라 `DELETE ... WHERE rowid=?` 가 즉시 찾는다.
    `fts_upsert`·`fts_insert`·`fts_clear`·`fts_delete_doc(s)`·
    `fts_existing_cids` 가 전부 이 표를 거친다.
    **`chunk_fts` 에 쓰는 새 경로를 추가하면 매핑도 같이 써야 한다** —
    안 그러면 그 행은 영영 못 지운다.
  - **백필은 `init()` 안에서 한 번의 `INSERT ... SELECT` 로 돈다.
    라이브 실측 10.6초** (673,678행, 2026-09-17 11:38:13→24, `docker
    logs` 기준). `n_fts > n_map` 일 때만 돌아서 재부팅해도 다시 안 돈다.
    - **이 숫자를 두 번 틀리게 추정했다.** 처음엔 스크래치 DB(164MB)
      0.4초를 보고 "67만 행 약 1초"(10배 낙관), 다음엔 운영이 4.8GB인
      걸 알고 "4.8GB 스트리밍이라 몇 분, 부팅이 막혀 heartbeat 10분
      초과 → 워치독 강제 재생성 → 재시작 루프"(과한 비관). 후자를
      막겠다고 배치 백필 + 상태 테이블 + 백그라운드 잡 + 백필 중
      삭제 폴백까지 만들었다가, 라이브 로그를 보고 **전부 되돌렸다** —
      일어나지 않는 사고를 막는 코드였다.
    - 교훈은 이 섹션의 마지막 줄과 같다: **크기가 다른 환경의 측정값을
      선형으로 늘려 잡지 말 것.** 재는 게 10초면 재고 나서 설계할 것.
  - `ocr_cache_put`(덤프 20회)은 **범인이 아니라 피해자**다. 한 행 쓰자고
    줄을 서 있었을 뿐이라, 락이 짧아지면 같이 풀린다. 먼저 이것부터
    배치로 묶으려던 것은 잘못된 방향이었다.
  - **효과 확인됨** (2026-09-18, 배포 경계로 갈라 측정). `5d6daad` 는
    2026-09-17 11:38 UTC 배포:
    - 9/17 T00~T11 (수정 전, 12h): 30건 → **시간당 2.50건**
    - 9/17 T12~T23 (수정 후, 12h): 3건 · 9/18 T00~T15 (16h): 6건
      → 합계 28h 9건 = **시간당 0.32건**
    - **7.8배 감소(−87%)**, 하루 환산 60건 → 7.7건. T11 에 1건, T12 에
      0건, T13 에 1건으로 **배포 시각 직후부터 떨어진다** — 날짜별 편차가
      아니라 경계가 정확하다.
    - **날짜별이 아니라 시간대별로 쪼갤 것.** 하루 단위로는 9/17 이
      33건이라 수정 전후가 섞여 판단이 안 된다. `grep -o "===== <날짜>T[0-9][0-9]"`
      로 시간 단위까지 잘라야 배포 경계가 보인다.
    - **남은 9건의 정체 = PyMuPDF PDF 표 추출** (2026-09-18 집계).
      `_wconn`·`fts_upsert` 는 목록에서 **완전히 사라졌다** — 락 컨보이는
      끝났다. 대신 `loaders.py → load_pdf` 5회 · `_extract_pdf_tables`
      2회 · `cost.py → record_resp/record/_conn` 2회 · `regenerate` 2회 ·
      `render_wiki` 2회. 지속은 10·11·16·17 / 24·25·26·33·33초.
      (`_loop_stall_dumper` 9회와 `main` 7회는 감지기 자신과 루프 스택
      밑동이라 범인이 아니다 — 집계할 때 빼고 읽을 것.)
    - **예상된 잔여분이다.** 이 섹션이 이미 적어둔 대로 MuPDF 호출 하나는
      통째로 GIL 을 쥐고, `_PDF_EXTRACT_SEM` 은 동시 개수만 묶지 한 개짜리
      긴 호출을 짧게 만들지 못한다. 더 줄이려면 `PDF_EXTRACT_CONCURRENCY`
      를 2→1 로 내리는 것이 후보인데, **PDF ingest 속도와 맞바꾸는 일이라
      사용자와 합의하고 할 것**(`.env` 값이라 코드만 고치면 안 바뀐다).
  - 진단 과정의 교훈: 원인을 **두 번 잘못 짚었다**(캐시 오염 가설, 배포
    전 화면을 배포 후로 착각). **라이브 출력을 보기 전에 고치지 말 것.**
