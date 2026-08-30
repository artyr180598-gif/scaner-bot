# AGENTS.md — точка входа для ИИ-агентов

🧠 **Мозг проекта живёт в [`AI_AGENTS/`](AI_AGENTS/README.md) — начни с
[`AI_AGENTS/BRAIN.md`](AI_AGENTS/BRAIN.md)** (что работает / что нет /
что уже пробовали), затем [`AI_AGENTS/PLAYBOOK.md`](AI_AGENTS/PLAYBOOK.md)
(как запускать тесты и бектест).

Кратко:

- Квантовое ядро сигналов — `strategy.py` (классы CARRY/REVERSION,
  z-score, round-trip экономика). Бектест использует ТОТ ЖЕ движок.
- Любое изменение стратегии = тесты (`python -m unittest discover -s tests`,
  118 шт.) + прогон бектеста + запись в `AI_AGENTS/BACKTESTS.md`.
- Данные бектеста качаются с GitHub (сеть песочницы не пускает к биржам):
  `./backtest/fetch_data.sh` → `backtest/prepare_data.py`. Детали и ловушки —
  `AI_AGENTS/DATA_SOURCES.md`.
- Не повторяй уже провалившиеся подходы — список в `AI_AGENTS/BRAIN.md`,
  раздел «ЧТО НЕ РАБОТАЕТ».
