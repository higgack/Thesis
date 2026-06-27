FROM python:3.11-slim

# Deno: yt-dlp's EJS layer needs an external JS runtime (>=2.3.0) to
# solve YouTube's player JS challenge. Copied from the official bin
# image so we don't drag curl/unzip into the runtime layer. Pinned for
# reproducibility — bump the tag if a future Deno breaks the EJS API.
COPY --from=denoland/deno:bin-2.8.1 /deno /usr/local/bin/deno

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Deno cache on the bind-mounted data dir so compiled EJS scripts
# persist across rebuilds (same persistence pattern as HF_HOME).
ENV DENO_DIR=/app/data/deno_cache

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libxml2-dev libxslt1-dev git \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
# Install torch CPU-only first so sentence-transformers' transitive
# dependency doesn't drag in 1-2GB of nvidia-cuda-* wheels we can't
# use on this CPU-only VM. The CPU index has the same torch API.
# Generous --retries/--timeout: this layer cache-busts on every code push
# (COPY src below), so the torch download re-runs each deploy and a single
# transient pytorch.org hiccup must not fail the whole build.
RUN pip install --upgrade pip && \
    pip install --retries 10 --timeout 180 \
        --index-url https://download.pytorch.org/whl/cpu torch && \
    pip install .

COPY src ./src

# Best-effort refresh to the latest yt-dlp *nightly* on every deploy.
# YouTube breaks the extractor every few months and the stable channel
# lags by weeks; nightly carries the fix first. This RUN sits AFTER
# `COPY src` so its layer cache busts on every code push — each
# auto-deploy then self-heals YouTube without a manual pin bump.
# `|| true` keeps a transient PyPI hiccup or a yanked nightly from
# failing the whole deploy: we just keep the [default] floor pinned in
# pyproject.toml until the next build succeeds.
RUN pip install -U --pre "yt-dlp[default]" || true

CMD ["python", "-m", "src.bot"]
