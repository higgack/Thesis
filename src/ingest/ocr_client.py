"""
OCR backend router.

Selects which OCR engine processes a rendered page based on the
`OCR_BACKEND` env (default `gemini`). Dormant by default — without
the ocr-worker container running, only the `gemini` path is reachable.

Backends:
  gemini  Use Gemini Flash-Lite Vision (current default behaviour).
          Per-page cost ~₩1, very high quality on Korean + English
          + tables + charts. No local CPU work, fully network-bound.

  local   Use the ocr-worker container's PaddleOCR. Zero per-page
          cost. Quality drops ~10-20% on chart-heavy pages and
          rotated scans. CPU-bound on the worker side (capped to
          2 vCPU via docker-compose so the bot stays responsive).

  hybrid  Submit to PaddleOCR first. If the result confidence
          ≥ OCR_HYBRID_CONF_MIN (default 0.8) AND text length ≥
          OCR_HYBRID_TEXT_MIN (default 50 chars), keep it.
          Otherwise fall back to Gemini Vision for that one page.
          Best quality at minimal cost — Gemini fires only on
          genuinely hard pages.

Worker integration uses a file queue:
  data/ocr_queue/<job_id>.json      bot writes job + path to png
  data/ocr_queue/<job_id>.png       bot writes rendered page bytes
  data/ocr_results/<job_id>.json    worker writes text + confidence
Both sides write to .tmp then atomic-rename to the final name so a
crash mid-write never leaves a torn file. If the worker isn't
running or times out (OCR_LOCAL_TIMEOUT_SEC, default 60), the
hybrid path falls back to Gemini and the local path returns "".

Heartbeat: the worker touches `data/ocr_worker_heartbeat` every 5s.
The bot doesn't currently gate on heartbeat freshness; the timeout
+ fallback covers the worker-down case, and the timeout is short
enough that a stuck local backend doesn't pin an ingest slot.
"""
import json
import logging
import os
import time
import uuid
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)


def _backend() -> str:
    """Read env each call so a flipped OCR_BACKEND env via
    `docker compose up -d --force-recreate bot` picks up immediately
    without restarting the importer too. Cheap, just env lookup."""
    return (os.getenv("OCR_BACKEND", "gemini").strip().lower() or "gemini")


_QUEUE_DIR = Path(os.getenv("OCR_QUEUE_DIR",
                            str(config.DATA_DIR / "ocr_queue")))
_RESULT_DIR = Path(os.getenv("OCR_RESULT_DIR",
                             str(config.DATA_DIR / "ocr_results")))
_LOCAL_TIMEOUT_SEC = float(os.getenv("OCR_LOCAL_TIMEOUT_SEC", "60"))
_HYBRID_CONF_THRESHOLD = float(os.getenv("OCR_HYBRID_CONF_MIN", "0.8"))
_HYBRID_TEXT_MIN_CHARS = int(os.getenv("OCR_HYBRID_TEXT_MIN", "50"))


def _local_via_queue(img_bytes: bytes, page_idx: int) -> tuple[str, float]:
    """Submit one rendered page to the worker; wait for the result.

    Returns (text, confidence). On any failure (worker down, timeout,
    malformed result) returns ("", 0.0) — caller decides whether to
    fall back to Gemini.
    """
    try:
        _QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        _RESULT_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        log.exception("ocr_client: queue dir create failed")
        return "", 0.0

    job_id = uuid.uuid4().hex
    img_path = _QUEUE_DIR / f"{job_id}.png"
    job_path = _QUEUE_DIR / f"{job_id}.json"
    tmp_path = _QUEUE_DIR / f"{job_id}.json.tmp"

    try:
        img_path.write_bytes(img_bytes)
    except Exception:
        log.exception("ocr_client: image write failed")
        return "", 0.0

    payload = {
        "job_id": job_id,
        "img_path": str(img_path),
        "page_idx": page_idx,
    }
    try:
        tmp_path.write_text(json.dumps(payload), encoding="utf-8")
        tmp_path.rename(job_path)
    except Exception:
        log.exception("ocr_client: job write failed")
        img_path.unlink(missing_ok=True)
        return "", 0.0

    result_path = _RESULT_DIR / f"{job_id}.json"
    deadline = time.time() + _LOCAL_TIMEOUT_SEC
    while time.time() < deadline:
        if result_path.exists():
            try:
                data = json.loads(result_path.read_text(encoding="utf-8"))
                return (
                    str(data.get("text") or ""),
                    float(data.get("confidence") or 0.0),
                )
            except Exception:
                log.exception("ocr_client: result parse failed")
                return "", 0.0
            finally:
                result_path.unlink(missing_ok=True)
                img_path.unlink(missing_ok=True)
        time.sleep(0.3)

    # Timeout — clean up so the queue doesn't accumulate ghost jobs.
    log.warning("ocr_client: worker timeout for page %d (job %s)",
                page_idx, job_id[:8])
    img_path.unlink(missing_ok=True)
    job_path.unlink(missing_ok=True)
    return "", 0.0


def _gemini_ocr_sync(img_bytes: bytes) -> str:
    """Gemini Flash-Lite Vision OCR. Returns extracted text or '[OCR
    error: ...]' on failure (caller surfaces the marker; we don't
    re-raise so one bad page doesn't kill the whole batch). Identical
    behaviour to the pre-refactor inline call in loaders.py."""
    try:
        from google import genai
        from google.genai import types
        from ..store import cost as _cost
    except Exception:
        log.exception("ocr_client: genai import failed")
        return "[OCR error: ImportError]"

    prompt = (
        "이 페이지의 모든 텍스트를 그대로 추출하세요. "
        "단락/제목/표 구조를 유지하고, 설명이나 코멘트 없이 "
        "텍스트만 출력하세요."
    )
    try:
        client = genai.Client(api_key=config.GOOGLE_API_KEY)
        resp = client.models.generate_content(
            model=config.SUMMARY_MODEL,
            contents=[
                types.Part.from_bytes(data=img_bytes, mime_type="image/png"),
                types.Part.from_text(text=prompt),
            ],
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=2048,
            ),
        )
        _cost.record_resp(config.SUMMARY_MODEL, resp, purpose="ingest")
        return (resp.text or "").strip()
    except Exception as e:
        log.warning("ocr_client: gemini call failed: %s", e)
        return f"[OCR error: {type(e).__name__}: {e}]"


def ocr_one_page(img_bytes: bytes, page_idx: int = 0) -> str:
    """Public entry point. Routes one rendered page through whichever
    backend `OCR_BACKEND` selects. Returns extracted text (empty
    string on total failure). Caller owns caching and error surfacing.
    """
    b = _backend()

    if b == "local":
        text, conf = _local_via_queue(img_bytes, page_idx)
        if text:
            log.info("ocr local: page %d → %d chars, conf=%.2f",
                     page_idx, len(text), conf)
        return text

    if b == "hybrid":
        text, conf = _local_via_queue(img_bytes, page_idx)
        if (text
                and conf >= _HYBRID_CONF_THRESHOLD
                and len(text) >= _HYBRID_TEXT_MIN_CHARS):
            log.info("ocr hybrid: page %d kept local (%d chars, conf=%.2f)",
                     page_idx, len(text), conf)
            return text
        log.info(
            "ocr hybrid: page %d falling back to gemini "
            "(local %d chars, conf=%.2f)",
            page_idx, len(text or ""), conf,
        )
        return _gemini_ocr_sync(img_bytes)

    # Default / unknown → gemini. Default path stays bit-identical
    # to the pre-refactor inline call.
    if b != "gemini":
        log.warning("ocr_client: unknown OCR_BACKEND=%r, using gemini", b)
    return _gemini_ocr_sync(img_bytes)
