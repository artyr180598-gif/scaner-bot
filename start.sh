#!/usr/bin/env bash
# Запуск бота одной командой: ./start.sh
#
# Работает и на хостинге (где venv лежит в /app/.venv), и локально.
# Команда хостинга может быть любой из:
#   python -m chris_bots.main        (старая команда — поддержана алиасом)
#   python -m crypto_advisor.main    (каноническая)
set -euo pipefail

cd "$(dirname "$0")"

for candidate in ".venv/bin/python" "venv/bin/python" "python3" "python"; do
  if command -v "$candidate" >/dev/null 2>&1 || [ -x "$candidate" ]; then
    exec "$candidate" -m chris_bots.main "$@"
  fi
done

echo "python не найден" >&2
exit 127
