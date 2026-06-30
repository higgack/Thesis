# 오프디스크 일일 백업 (GCS)

풀 VM 디스크는 **주1회 리전 스냅샷**(비용 최적화)으로만 보호되므로, 그 사이
7일 동안 잃을 수 있는 **작고 유니크한 KB 상태**(★/메모/알람 마크, Q&A·노트·
KG·위키·비용 이력)를 **매일 GCS 버킷으로** 따로 백업한다. 거의 0원(수~수십 MB).

- 포함: `data/`의 sqlite db(marks/qna/notes/kg/kg_ignored/cost/meta/pending),
  모든 상태 JSON(`*.json`), `data/wiki/` 마크다운.
- 제외: `data/chroma`(임베딩 — 큼 + 재임베딩으로 복구 가능), `data/files`(재취득 가능).
- 일관성: sqlite는 온라인 `.backup`+`serialize()`로 안전 사본(WAL 중에도 무손상).
- 봇 `daily_backup` 잡이 매일 ~04:00 KST 자동 실행(APScheduler, 신규 cron 없음).
  `BACKUP_BUCKET` 미설정 시 **아무 동작 안 함**(=배포는 안전, 셋업 후 활성).

## 1회 셋업 (VM에서, 복붙)

변수만 한 번 정해두면 나머지는 그대로 복붙:
```
BUCKET=gen-lang-client-0325676393-thesis-backup
SA=722358979517-compute@developer.gserviceaccount.com
```

**① 버킷 생성 (VM과 같은 리전 = 저렴):**
```
gcloud storage buckets create gs://$BUCKET --location=asia-northeast3 --uniform-bucket-level-access
```
예상: `Creating gs://...` 후 무에러. (이름이 전역 중복이면 다른 이름으로)

**② 봇 SA에 이 버킷 쓰기 권한:**
```
gcloud storage buckets add-iam-policy-binding gs://$BUCKET --member="serviceAccount:$SA" --role="roles/storage.objectAdmin"
```
예상: `Updated IAM policy ...`.

**③ 30일 지난 백업 자동삭제(비용 상한):**
```
printf '{"rule":[{"action":{"type":"Delete"},"condition":{"age":30}}]}' > /tmp/lc.json
gcloud storage buckets update gs://$BUCKET --lifecycle-file=/tmp/lc.json
```
예상: `Updating gs://...`.

**④ `.env`에 버킷 등록 + 봇 재생성**(env_file 재읽기엔 force-recreate 필요):
```
cd ~/Thesis
grep -q '^BACKUP_BUCKET=' .env && sed -i "s|^BACKUP_BUCKET=.*|BACKUP_BUCKET=$BUCKET|" .env || echo "BACKUP_BUCKET=$BUCKET" >> .env
docker compose up -d --force-recreate bot
```

**⑤ 즉시 1회 테스트(04시까지 안 기다리고):**
```
docker exec thesis-bot-1 python -c "from src.store import backup; print(backup.run_backup())"
```
성공: `{'status': 'ok', 'detail': 'gs://.../thesis/2026-06-30.tar.gz (NNNkB)'}`
- `skip` → `.env`의 `BACKUP_BUCKET` 비었음(④ 확인).
- `error: 업로드 실패 ...403` → ② 권한 반영 대기(1~2분) 후 재시도.

**⑥ 확인:**
```
gcloud storage ls gs://$BUCKET/thesis/
```
오늘 날짜 `.tar.gz`가 보이면 끝. 이후 매일 자동.

## 복원 (디스크 사고 시)
```
gcloud storage cp gs://$BUCKET/thesis/<날짜>.tar.gz /tmp/restore.tar.gz
mkdir -p /tmp/restore && tar -xzf /tmp/restore.tar.gz -C /tmp/restore
```
- `db/*.db` → `~/Thesis/data/`로 복사(봇 정지 상태에서)
- `json/*` → `~/Thesis/data/`
- `wiki/` → `~/Thesis/data/wiki/`
- `chroma`는 백업에 없음 → 복원 후 재임베딩(`/reembed` 등)으로 재생성.

## 비활성화
`.env`에서 `BACKUP_BUCKET=` 비우고 `docker compose up -d --force-recreate bot`.
잡은 남지만 매일 no-op.
