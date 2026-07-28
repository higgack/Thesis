# Base image (2026-05): official paddlepaddle CPU image.
#   Why this over python:3.10-bullseye + pip-installed paddle:
#   - paddle 2.6.2 wheels show 'free(): invalid pointer' glibc abort
#     on ALL tested debian variants (slim/bullseye/bookworm), and
#     paddlepaddle/paddle:2.6.x tags were retired from Docker Hub
#     (only 3.x tags remain visible as of 2026-05).
#   - paddlepaddle/paddle:3.3.1 is the latest stable CPU image, ships
#     paddle pre-validated against its bundled OS + Python, so the
#     binary ABI issues we hit with pip install paddlepaddle go away.
#   - Image is ~5GB but the worker is dormant by default; pulling
#     once during the rare hybrid-mode activation is acceptable.
# Worker code was rewritten to the paddleocr 3.x API: PaddleOCR
# constructor uses use_doc_orientation_classify / use_doc_unwarping /
# use_textline_orientation flags (not use_angle_cls), and inference
# is via .predict() returning result objects with rec_texts /
# rec_scores.
FROM paddlepaddle/paddle:3.3.1

# paddleocr is NOT pre-installed in the paddle base image — only
# the core paddle framework. Add it on top via requirements.txt
# (version pinned there so a single grep finds the dependency).
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Pre-load the Korean OCR model so the first job doesn't pay the
# ~30s cold-start penalty. Best-effort — if it fails (no network
# at build time, model server hiccup), the worker downloads it
# lazily on first request.
RUN python -c "from paddleocr import PaddleOCR; PaddleOCR(use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=True, lang='korean')" \
    || echo "model preload deferred to first request"

COPY worker.py /app/worker.py

# Unbuffered stdout/stderr so `docker logs` shows progress live.
ENV PYTHONUNBUFFERED=1

CMD ["python", "/app/worker.py"]
