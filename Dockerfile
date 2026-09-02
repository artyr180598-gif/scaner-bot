# syntax=docker/dockerfile:1

# Образ для Railway (тип процесса Worker). Лёгкий slim-образ + non-root user.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Сначала только зависимости — этот слой кэшируется между сборками.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Затем код: пакет приложения и офлайн-гейт (tools.selftest вызывается
# из `python -m app.main --selftest`).
COPY app ./app
COPY tools ./tools

# Каталог для журнала сигналов и настроек чатов (том Railway).
RUN mkdir -p /app/data && chown -R 1000:1000 /app/data

# Непривилегированный пользователь.
RUN useradd --create-home --shell /usr/sbin/nologin --uid 1000 appuser
USER appuser

# Worker-процесс: не слушает порт, работает бесконечным циклом.
# SIGTERM обрабатывается в app/main.py (graceful shutdown для redeploy).
CMD ["python", "-m", "app.main"]
