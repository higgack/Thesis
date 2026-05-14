"""
PaddleOCR worker — file-queue based OCR service.

Polls `/app/data/ocr_queue/*.json` for jobs written by the main bot
(src/ingest/ocr_client.py), runs Korean+English PaddleOCR on the
referenced page image, writes the result to
`/app/data/ocr_results/<job_id>.json`. Atomic rename on both sides
so a kill/restart at any point preserves either the original job or
the finished result, never a torn state.

Dormant by default: docker-compose puts this service behind
`profiles: ["ocr-local"]`, so `docker compose up -d` alone does not
start it. Enable with:
    docker compose --profile ocr-local up -d ocr-worker
and set OCR_BACKEND=local or =hybrid in `.env` to route bot OCR
through this worker.

Single-process for now; PaddleOCR uses ~1.5GB RAM and ~2 vCPU under
the cpus: '2' limit, so adding parallel workers is rarely worth it
unless ingest volume exceeds ~50 PDFs/day with chart-heavy content.
"""
import json
import logging
import os
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("ocr-worker")

QUEUE_DIR = Path(os.getenv("OCR_QUEUE_DIR", "/app/data/ocr_queue"))
RESULT_DIR = Path(os.getenv("OCR_RESULT_DIR", "/app/data/ocr_results"))
POLL_INTERVAL = float(os.getenv("OCR_POLL_INTERVAL", "0.5"))
HEARTBEAT_PATH = Path(os.getenv("OCR_HEARTBEAT", "/app/data/ocr_worker_heartbeat"))

QUEUE_DIR.mkdir(parents=True, exist_ok=True)
RESULT_DIR.mkdir(parents=True, exist_ok=True)

log.info("Loading PaddleOCR (Korean+English)...")
try:
    from paddleocr import PaddleOCR
except Exception:
    log.exception("paddleocr import failed — exiting")
    raise

# Single shared model instance: cold-start is ~30s, hot inference
# ~0.5-2s per page on 2 vCPU.
#
# Pinned to paddleocr 2.7.x for now (see requirements.txt). v3.x
# (current latest 3.5.0, https://github.com/PaddlePaddle/PaddleOCR)
# is a major rewrite — the constructor + ocr() return shape changed.
# Upgrade is a follow-on task: bump paddleocr/paddlepaddle pins, swap
# this constructor for the v3 API (`from paddleocr import PPOCR;
# PPOCR(lang='korean')`), and adjust _ocr_image() to the new
# result schema (3.x returns OCRResult objects, not nested lists).
_ocr = PaddleOCR(use_angle_cls=True, lang="korean", show_log=False)
log.info("Model loaded. Polling %s for jobs (interval=%.1fs)",
         QUEUE_DIR, POLL_INTERVAL)


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write tmp file + rename so a kill mid-write never leaves a
    half-written result that the bot would mis-parse."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.rename(path)


def _ocr_image(img_path: Path) -> tuple[str, float, int]:
    """Returns (joined_text, avg_confidence, line_count)."""
    result = _ocr.ocr(str(img_path))
    lines: list[str] = []
    confs: list[float] = []
    # PaddleOCR result shape: [[bbox, (text, confidence)], ...] per image.
    # ocr.ocr returns [page_results] even for a single image.
    page = (result[0] if result else None) or []
    for region in page:
        try:
            if isinstance(region, (list, tuple)) and len(region) >= 2:
                _, txt_conf = region[0], region[1]
                if isinstance(txt_conf, (list, tuple)) and len(txt_conf) >= 2:
                    text = str(txt_conf[0])
                    conf = float(txt_conf[1])
                    if text.strip():
                        lines.append(text)
                        confs.append(conf)
        except Exception:
            log.debug("malformed region in OCR output: %r", region)
    joined = "\n".join(lines)
    avg = (sum(confs) / len(confs)) if confs else 0.0
    return joined, avg, len(lines)


def _process(job_path: Path) -> None:
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("malformed job %s: %s — discarding", job_path.name, e)
        job_path.unlink(missing_ok=True)
        return

    job_id = job.get("job_id") or ""
    img_path = Path(job.get("img_path") or "")
    if not job_id or not img_path.exists():
        log.warning("invalid job (missing job_id/img): %r", job)
        job_path.unlink(missing_ok=True)
        return

    t0 = time.time()
    try:
        text, conf, n_lines = _ocr_image(img_path)
        result = {
            "job_id": job_id,
            "text": text,
            "confidence": conf,
            "lines": n_lines,
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
        log.info("job %s: %d lines, conf=%.2f, %dms",
                 job_id[:8], n_lines, conf, result["elapsed_ms"])
    except Exception as e:
        log.exception("OCR failed for job %s", job_id[:8])
        result = {
            "job_id": job_id,
            "text": "",
            "confidence": 0.0,
            "lines": 0,
            "error": f"{type(e).__name__}: {e}",
        }

    _atomic_write_json(RESULT_DIR / f"{job_id}.json", result)
    # Always clean up job + image regardless of success — the bot
    # has the result file now (or the timeout will fall back).
    job_path.unlink(missing_ok=True)
    img_path.unlink(missing_ok=True)


def _touch_heartbeat() -> None:
    """Write current epoch to heartbeat file. Bot can stat-check this
    before submitting jobs to know the worker is alive (freshness <
    e.g. 30s)."""
    try:
        HEARTBEAT_PATH.write_text(str(int(time.time())))
    except Exception:
        pass


def main() -> None:
    last_heartbeat = 0.0
    while True:
        try:
            now = time.time()
            if now - last_heartbeat >= 5.0:
                _touch_heartbeat()
                last_heartbeat = now

            jobs = sorted(
                p for p in QUEUE_DIR.glob("*.json")
                if not p.name.endswith(".tmp")
            )
            for j in jobs:
                _process(j)
            if not jobs:
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            log.info("shutting down")
            return
        except Exception:
            log.exception("worker loop error — continuing")
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
