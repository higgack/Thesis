# Base image choice (2026-05): python:3.10-bullseye (Debian 11).
#   Why bullseye and not the default (bookworm) or slim:
#   - python:3.11-slim → ExitCode=139 SIGSEGV in `import paddle`.
#     Slim base's minimal libstdc++ ABI doesn't match the paddle
#     2.6.2 binary wheel.
#   - python:3.10 (bookworm full) → "free(): invalid pointer" glibc
#     abort. Bookworm ships glibc 2.36, paddle 2.6.x was built
#     against glibc 2.31 — pthread/malloc ABI changed in 2.34+.
#   - python:3.10-bullseye ships glibc 2.31 which is exactly what
#     paddle 2.6.x wheels target. No ABI mismatch.
# AVX-512 capable Xeon (Cascade Lake) confirmed, so this is not a
# CPU SIMD issue.
FROM python:3.10-bullseye

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
