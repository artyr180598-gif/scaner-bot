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

# Затем код приложения (все Python-модули: directional, market_data, strategy
# и др.). Раньше копировался фиксированный список — при добавлении новых модулей
# бот падал на старте с ModuleNotFoundError.
COPY *.py ./

# Непривилегированный пользователь.
RUN useradd --create-home --shell /usr/sbin/nologin appuser
USER appuser

# Worker-процесс: не слушает порт, работает бесконечным циклом.
# SIGTERM обрабатывается в main.py (graceful shutdown для redeploy на Railway).
CMD ["python", "main.py"]
