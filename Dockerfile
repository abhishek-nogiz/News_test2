# ─── Dockerfile for News Agent Pipeline ───
# Build:  docker build -t news-agent .
# Run:    docker run --env-file .env news-agent
# Shell:  docker run -it --env-file .env news-agent bash

FROM python:3.10-slim

# ── System deps ──
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# ── Workdir ──
WORKDIR /app

# ── Install Python deps first (layer cache) ──
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Copy project ──
COPY config.py .
COPY main.py .
COPY news_agent/ news_agent/
COPY app/ app/

ENV TZ=Asia/Kolkata
# ── Create storage dirs ──
RUN mkdir -p /app/storage/blogs /app/storage/cache /app/storage/images \
    /app/storage/memory /app/storage/trends /app/storage/vector-store/tenants

# ── Default: run the scheduler app ──
CMD ["python", "-m", "app.scheduler_app"]
