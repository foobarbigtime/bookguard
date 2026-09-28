FROM python:3.12-slim

ARG BOOKGUARD_VERSION=0.6.0
ARG BOOKGUARD_VCS_REF=unknown

LABEL org.opencontainers.image.title="BookGuard" \
      org.opencontainers.image.description="Conservative validation, quarantine, and metadata-repair companion for Bindery" \
      org.opencontainers.image.version="${BOOKGUARD_VERSION}" \
      org.opencontainers.image.revision="${BOOKGUARD_VCS_REF}" \
      org.opencontainers.image.source="https://github.com/foobarbigtime/bookguard"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BOOKGUARD_BUILD_VERSION="${BOOKGUARD_VERSION}" \
    BOOKGUARD_BUILD_REVISION="${BOOKGUARD_VCS_REF}" \
    BOOKGUARD_BUILD_SOURCE="https://github.com/foobarbigtime/bookguard"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tools ./tools
COPY templates ./templates
COPY static ./static

RUN mkdir -p /config /quarantine

EXPOSE 8788

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8788/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8788"]
