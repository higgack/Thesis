FROM python:3.11-slim

# OS deps: libgl for OpenCV (paddleocr image preprocessing),
# libglib/libsm/libxext for image lib runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 libsm6 libxext6 \
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
