# ─── Dockerfile for News Agent Pipeline (with Vector Architecture) ───
# Build:  docker build -t news-agent .
# Run:    docker run --env-file .env news-agent
# Shell:  docker run -it --env-file .env news-agent bash
#
# CHANGES FROM ORIGINAL:
#   - Added system deps for trafilatura (libxml2, libxslt, git)
#   - Added scripts/ directory to COPY
#   - Added storage/vector-store directory creation
#   - Added .dockerignore-friendly layer ordering
#   - Optional: install sentence-transformers+torch for local fallback
#     (commented out — on Railway free tier you do NOT want these)

FROM python:3.10-slim

# ── System deps ──
# gcc:        needed by some Python packages (boto3, numpy)
# libxml2-dev libxslt-dev: needed by trafilatura (article text extraction)
# git:        needed by some HF model fetches (rare, but safe to have)
# curl:       for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates && \
    update-ca-certificates \
    gcc \
    libxml2-dev \
    libxslt1-dev \
    libxml2 \
    libxslt1.1 \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ── Workdir ──
WORKDIR /app

# ── Install Python deps first (layer cache) ──
# requirements.txt should include:
#   requests, numpy, beautifulsoup4, trafilatura, boto3, python-dotenv
# It should NOT include sentence-transformers or torch (Railway OOM).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Copy project ──
COPY config.py .
COPY main.py .
COPY news_agent/ news_agent/
COPY app/ app/


# ── Timezone ──
ENV TZ=Asia/Kolkata

# ── Create storage dirs (all the ones the pipeline writes to) ──
RUN mkdir -p \
    /app/storage/blogs \
    /app/storage/cache \
    /app/storage/images \
    /app/storage/memory \
    /app/storage/trends \
    /app/storage/vector-store/tenants

# ── Healthcheck: hit the scheduler web UI every 60s ──
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/jobs || exit 1

# ── Default: run the scheduler app (Flask UI + background scheduler thread) ──
CMD ["python", "-m", "app.scheduler_app"]

# ── Alternative entrypoints (use via docker-compose or docker run) ──
# Index bootstrap:    docker compose run --rm news-agent python scripts/bootstrap_vector_store.py --force
# Index refresh:      docker compose run --rm news-agent python scripts/refresh_vector_store.py
# Verify embeddings:  docker compose run --rm news-agent python scripts/verify_embeddings.py
# Test retrieval:     docker compose run --rm news-agent python scripts/test_internal_links.py --topic "Messi"