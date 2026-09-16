FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ENVIRONMENT=production \
    SERVER_HOST=0.0.0.0 \
    SERVER_PORT=8000

WORKDIR /app

COPY server/pyproject.toml /app/server/pyproject.toml
COPY server/app /app/server/app
COPY server/README.md /app/server/README.md

RUN pip install --upgrade pip \
    && pip install /app/server \
    && useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app

USER appuser
WORKDIR /app/server

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
