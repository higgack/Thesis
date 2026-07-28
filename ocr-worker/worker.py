"""
PaddleOCR worker — file-queue based OCR service (paddleocr 3.x API).

Polls `/app/data/ocr_queue/*.json` for jobs written by the main bot
(src/ingest/ocr_client.py), runs Korean+English PaddleOCR on the
referenced page image, writes the result to
`/app/data/ocr_results/<job_id>.json`. Atomic rename on both sides
so a kill/restart at any point preserves either the original job or
the finished result, never a torn state.

Dormant by default: docker-compose puts this service behind
`profiles: ["ocr-local"]`. Activate with
    docker compose --profile ocr-local up -d ocr-worker
and set OCR_BACKEND=local or =hybrid in .env to route bot OCR
through this worker.

paddleocr 3.x notes (vs 2.x):
- Constructor: use_doc_orientation_classify / use_doc_unwarping /
  use_textline_orientation flags (NOT use_angle_cls / show_log).
- Inference: .predict(path_or_image) instead of .ocr(path).
- Results: list of OCRResult-like objects with rec_texts (list of
  strings) and rec_scores (list of floats). Access via subscript
  (res['rec_texts']) — defensive code below also tries attribute
  and .json fallbacks in case the result schema shifts in patch
  releases.
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

log.info("Loading PaddleOCR (Korean+English, paddleocr 3.x API)...")
try:
    from paddleocr import PaddleOCR
except Exception:
    log.exception("paddleocr import failed — exiting")
    raise

# Single shared model instance. paddleocr 3.x constructor uses the
# pipeline-flag style (PaddleX inheritance). Korean lang + textline
# orientation (replaces 2.x's use_angle_cls). Doc-orientation and
# unwarping are heavier preprocessing steps disabled for speed.
_ocr = PaddleOCR(
    use_doc_orientation_classify=False,
    use_doc_unwarping=False,
    use_textline_orientation=True,
    lang="korean",
)
log.info("Model loaded. Polling %s for jobs (interval=%.1fs)",
         QUEUE_DIR, POLL_INTERVAL)


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write tmp file + rename so a kill mid-write never leaves a
    half-written result that the bot would mis-parse."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.rename(path)


def _extract_recognised(res) -> tuple[list[str], list[float]]:
    """paddleocr 3.x result objects expose rec_texts + rec_scores in
    one of three patterns depending on the patch release. Try all of
    them; any one that succeeds wins. Returns (texts, scores) — both
    empty on total failure (caller treats as 'no text found')."""
    # Pattern 1: subscript (PaddleX standard dict-like result)
    try:
        return list(res["rec_texts"]), list(res["rec_scores"])
    except (KeyError, TypeError, IndexError):
        pass
    # Pattern 2: attribute access
    try:
        return list(res.rec_texts), list(res.rec_scores)
    except AttributeError:
        pass
    # Pattern 3: .json property / method
    try:
        j = res.json
        if callable(j):
            j = j()
        if isinstance(j, dict):
            inner = j.get("res") if "res" in j else j
            if isinstance(inner, dict):
                return (list(inner.get("rec_texts", []) or []),
                        list(inner.get("rec_scores", []) or []))
    except Exception:
        pass
    return [], []


def _ocr_image(img_path: Path) -> tuple[str, float, int]:
    """Returns (joined_text, avg_confidence, line_count). Empty
    result on any failure — caller surfaces blank text and the bot
    falls back to Gemini for hybrid mode."""
    result = _ocr.predict(str(img_path))
    all_texts: list[str] = []
    all_confs: list[float] = []
    for res in (result or []):
        texts, scores = _extract_recognised(res)
        for t, s in zip(texts, scores):
            t = str(t or "").strip()
            if t:
                all_texts.append(t)
                try:
                    all_confs.append(float(s))
                except (TypeError, ValueError):
                    all_confs.append(0.0)
    joined = "\n".join(all_texts)
    avg = (sum(all_confs) / len(all_confs)) if all_confs else 0.0
    return joined, avg, len(all_texts)


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
    job_path.unlink(missing_ok=True)
    img_path.unlink(missing_ok=True)


def _touch_heartbeat() -> None:
    """Write current epoch to heartbeat file."""
    try:
        HEARTBEAT_PATH.write_text(str(int(time.time())))
    except Exception:
        pass


_SWEEP_TTL_SEC = 3600


def _sweep_orphans() -> None:
    """Drop result/queue files older than 1h. When the bot's 60s wait
    deadline expires after the worker already claimed a job, the worker
    still writes the result file — and nothing ever consumes it, so they
    accumulate forever in hybrid mode under load."""
    cutoff = time.time() - _SWEEP_TTL_SEC
    for d in (RESULT_DIR, QUEUE_DIR):
        try:
            for p in d.glob("*"):
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink(missing_ok=True)
                        log.info("swept stale %s", p.name)
                except OSError:
                    pass
        except OSError:
            pass


def main() -> None:
    last_heartbeat = 0.0
    last_sweep = 0.0
    while True:
        try:
            now = time.time()
            if now - last_heartbeat >= 5.0:
                _touch_heartbeat()
                last_heartbeat = now
            if now - last_sweep >= 600.0:
                _sweep_orphans()
                last_sweep = now

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
