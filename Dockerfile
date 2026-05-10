FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libxml2-dev libxslt1-dev git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
# Install torch CPU-only first so sentence-transformers' transitive
# dependency doesn't drag in 1-2GB of nvidia-cuda-* wheels we can't
# use on this CPU-only VM. The CPU index has the same torch API.
RUN pip install --upgrade pip && \
    pip install --index-url https://download.pytorch.org/whl/cpu torch && \
    pip install .

COPY src ./src

CMD ["python", "-m", "src.bot"]
