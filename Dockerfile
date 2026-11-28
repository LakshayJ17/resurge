# Resurge hosted web app: sign up, log in, type a company (or its website), get a verified competitor report.
# Build:  docker build -t resurge .
# Run:    docker run -d --env-file .env -p 127.0.0.1:8000:8000 -v resurge-data:/data resurge
# API keys come from the .env file at run time; they are never baked into the image.

# 1) The website: a Next.js static export (frontend/out). Node is only needed to build it, and its output is plain
#    files, so it is built on the build machine's own CPU even when the image is for another (x64 or Arm).
FROM --platform=$BUILDPLATFORM node:26-alpine AS website
WORKDIR /web
ENV NEXT_TELEMETRY_DISABLED=1
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend ./
RUN npm run build

# 2) The Python app, which serves the website and its API from one origin.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MALLOC_ARENA_MAX=2 \
    RESURGE_HOME=/data \
    RESURGE_KEEP_AWAKE=0 \
    RESURGE_FRONTEND_DIR=/app/website \
    FASTEMBED_CACHE_PATH=/opt/fastembed

WORKDIR /app
COPY pyproject.toml README.md .env.example ./
COPY config ./config
COPY src ./src
RUN pip install ".[web,rag]"

# Bake the small local embedding model (about 64 MB) into the image so the first chat question is fast.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='/opt/fastembed')" \
    && chmod -R a+rX /opt/fastembed

COPY --from=website /web/out /app/website

RUN useradd --create-home --uid 10001 resurge && mkdir -p /data && chown resurge:resurge /data
USER resurge
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

CMD ["resurge", "serve", "--host", "0.0.0.0", "--port", "8000"]
