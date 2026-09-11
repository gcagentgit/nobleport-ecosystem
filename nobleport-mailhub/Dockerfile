FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MAILHUB_DATA_DIR=/data \
    MAILHUB_HOST=0.0.0.0

WORKDIR /srv

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml requirements.txt ./
COPY mailhub ./mailhub
RUN pip install .

VOLUME ["/data"]
EXPOSE 8025

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD curl -fsS http://localhost:8025/health || exit 1

CMD ["mailhub", "serve"]
