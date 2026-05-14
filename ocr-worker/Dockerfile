# Base image choice (2026-05): python:3.10 full (NOT slim).
#   - paddlepaddle 2.6.x's libpaddle.so SIGSEGVs on python:3.11-slim
#     because the slim image ships a minimal libstdc++ ABI that
#     doesn't match paddle's binary. Confirmed via ExitCode=139 with
#     "import paddle" alone, on an AVX-512 capable Xeon (so not a
#     CPU instruction issue).
#   - python:3.10 (full debian-bookworm) ships the standard libstdc++
#     and glibc that paddle 2.6.x wheels were built against.
#   - python 3.10 is the most-tested CPython version for paddle 2.6
#     line (3.11 wheels exist but are flaky on minimal images).
FROM python:3.10

# OS deps:
#   libgomp1     — GNU OpenMP runtime required by paddlepaddle's
#                  libpaddle.so (without it: 'libgomp.so.1: cannot
#                  open shared object file').
#   libgl1       — OpenCV (paddleocr image preprocessing).
#   libglib2.0-0 — GLib runtime for image lib.
#   libsm6/libxext6 — X11 image lib runtime (cv2 imports).
# Most of these are already present in the python:3.10 full base,
# but we install explicitly to be deterministic across base image
# version bumps.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 libgl1 libglib2.0-0 libsm6 libxext6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download the Korean OCR model so the first job doesn't pay the
# ~30s cold-start penalty. Best-effort — if it fails (no network at
# build time) the worker will download lazily on first request.
RUN python -c "from paddleocr import PaddleOCR; PaddleOCR(use_angle_cls=True, lang='korean', show_log=False)" \
    || echo "model preload deferred to first request"

COPY worker.py /app/worker.py

# Unbuffered stdout/stderr so `docker logs` shows progress live.
ENV PYTHONUNBUFFERED=1

CMD ["python", "/app/worker.py"]
