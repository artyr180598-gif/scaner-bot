#!/usr/bin/env bash
# Запуск CryptoForge Pro одной командой.
# Работает и на Railway (/app/.venv), и локально.
set -euo pipefail
cd "$(dirname "$0")"

for candidate in ".venv/bin/python" "venv/bin/python" "python3" "python"; do
  if command -v "$candidate" >/dev/null 2>&1 || [ -x "$candidate" ]; then
    exec "$candidate" -m cryptoforge_pro.main "$@"
  fi
done

echo "python не найден" >&2
exit 127
