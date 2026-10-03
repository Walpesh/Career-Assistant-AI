# Career-Assistant-AI — backend (Python 3.12+, multi-stage production image).
# Stage runtime: python:3.12-slim, non-root user appuser,
# Playwright Chromium для парсинга (docs/04_PARSING_RULES.md fallback chain).

FROM python:3.12-slim AS builder
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
# Системные зависимости для сборки (psycopg/asyncpg не нужны — только asyncpg wheel;
# curl нужен healthcheck'ам; build-essential — страховка для sdists).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*
COPY backend/pyproject.toml backend/requirements.lock ./
RUN pip install --upgrade pip \
    && pip wheel --wheel-dir /wheels -r requirements.lock

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ENVIRONMENT=production \
    QUEUE_EMBEDDED_WORKERS=false
WORKDIR /app
# Playwright Chromium + системные зависимости браузера (docs/04 §fallback).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r appuser && useradd -r -g appuser -m -d /home/appuser appuser \
    && mkdir -p /home/appuser/.cache/ms-playwright /app \
    && chown -R appuser:appuser /home/appuser /app
COPY --from=builder /wheels /wheels
COPY backend/pyproject.toml backend/requirements.lock ./
RUN pip install --upgrade pip \
    && pip install --no-index --find-links=/wheels -r requirements.lock \
    && rm -rf /wheels \
    && python -c "import fastapi, arq, sqlalchemy, asyncpg, redis, httpx, playwright; print('deps ok')"
# Установка браузера от non-root невозможна для --with-deps (нужен apt),
# поэтому браузер ставится здесь (root), запуск — под appuser ниже.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN pip install playwright \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /ms-playwright && chown -R appuser:appuser /ms-playwright
COPY --chown=appuser:appuser backend/app ./app
COPY --chown=appuser:appuser backend/alembic ./alembic
COPY --chown=appuser:appuser backend/alembic.ini ./alembic.ini
COPY --chown=appuser:appuser backend/worker_entry.py ./worker_entry.py
COPY --chown=appuser:appuser frontend ./frontend
USER appuser
EXPOSE 8000
# LivenessProbe внутри образа: /health/live без внешних зависимостей.
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=8).status==200 else 1)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
