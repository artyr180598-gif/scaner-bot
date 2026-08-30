# PLAYBOOK.md — инструкция для нового агента (прочитай перед работой)

## 0. Быстрый контекст (2 минуты)

- Проект: Telegram-сканер арбитража «спот↔перп» на 4 биржах (ccxt), не торгует.
- Мозг: `AI_AGENTS/BRAIN.md` (состояние), `AI_AGENTS/BACKTESTS.md` (что уже
  пробовали), `AI_AGENTS/DATA_SOURCES.md` (сеть и данные), этот файл.
- Код: `scanner.py` (сбор/команды/формат), `strategy.py` (квантовое ядро),
  `config.py` (env → Settings), `telegram_bot.py` (транспорт),
  `alpha.py` (информационный PULSE), `forge.py` (информационный LOW_CHAN),
  `backtest/` (данные+симулятор), `tests/` (офлайн).

## 1. Окружение

```bash
cd /home/user/scaner-bot          # (или корень вашего чекаута)
python3 -m venv .venv             # если нет
.venv/bin/pip install -r requirements.txt -r backtest/requirements.txt
```

Python 3.11+. Бот запускается `python main.py`; без TELEGRAM_BOT_TOKEN —
DRY-RUN (сообщения в лог). Сеть до бирж из песочницы НЕ работает —
живой сканер здесь не запустить, проверяй логику тестами и бектестом.

## 2. Тесты (обязательный минимум после любого изменения)

```bash
.venv/bin/python -m unittest discover -s tests        # все 127 должны пройти
.venv/bin/python -m unittest tests.test_strategy -v   # только квант-ядро
```

Правило красной строки: **изменил формулу/гейт — напиши тест, который
ловит регрессию** (пример: `test_carry_blocked_when_spread_anomalously_low`).

## 3. Бектест (обязателен при изменении стратегии)

```bash
./backtest/fetch_data.sh                            # если data_cache пуст
.venv/bin/python backtest/prepare_data.py --prune-raw
.venv/bin/python backtest/run_backtest.py --years 2 --tag mytag
```

- Сравнение само печатает таблицу: OLD / CARRY_NAIVE / NEW_CARRYONLY /
  NEW_REVONLY / NEW — **следи, чтобы NEW не стал хуже своих абляций**
  (это признак слота/аллокационного бага, см. BRAIN.md п.6).
- Отчёт `backtest/results/mytag.md` — закоммитить, запись — в BACKTESTS.md.
- Ключевые флаги: `--z-entry`, `--min-funding-edge`, `--persistence`,
  `--take-profit/--stop-loss`, `--flip-hours/--flip-threshold`,
  `--slippage-bps` (стресс 5–10), `--alloc/--alloc-rev`, `--start/--end`.

Направленный бектест (PULSE, **не** ломает арбитраж v3):

```bash
.venv/bin/python backtest/run_pulse.py --tag pulse-vX
```

Канон: `backtest/results/pulse-v1.md` (49 монет). `pulse-smoke.md` — 3 монеты,
игнорировать. Свой picker: `backtest/run_forge3.py` → `forge-v3.md` (LOW_CHAN).
Направленную автоторговлю не включать: арбитраж v3 — единственный proven P&L;
LOW_CHAN яма −30%; VT/DD убивают OOS1.

## 4. Типовые грабли (уже наступали — см. BRAIN.md «ЧТО НЕ РАБОТАЕТ»)

1. Считать NET без комиссий выхода (round-trip!) — запрещено.
2. Кормить движок только связками выше порога — статистика врёт.
3. Выход carry по мгновенному флипу funding — чёрн.
4. Менять параметры, не глядя на абляции и не записывая прогон.
5. pandas 3.x: datetime64[us], не ns (см. DATA_SOURCES.md).
6. В heredoc-скриптах патчинга Python — экранирование кавычек: строковые
   литералы с `\"` внутри `<<'EOF'` прошли в файл с лишними кавычками и
   сломали синтаксис. Проверяй `python3 -c "import ast; ast.parse(...)"`.
7. Мержить изменения без прогона ВСЕХ тестов (дискавери ловит и старые).

## 5. Как добавить новую «аналитику» в ядро (чек-лист)

1. Поле/гейт в `strategy.StrategyConfig` + расчёт в `SignalEngine`
   (И `observe_and_assess`, И `assess_snapshot` — логика зеркальная!).
2. Env-переменная в `config.py` (from_env + describe) + `.env.example`.
3. Тест в `tests/test_strategy.py` (работает / не работает / границы).
4. Прогон бектеста до и после: абляция нового гейта (`enable_x=False`)
   обязана показать его вклад (или его бесполезность — тоже результат).
5. Записать прогон в BACKTESTS.md, решение — в DECISIONS.md, состояние —
   в BRAIN.md. Обновить `/strategy` текст в scanner.py, если меняется логика.

## 6. Как показывать новое в Telegram

- Новое поле оценки → `Assessment` (+`describe_block()` при необходимости).
- В `/top` — компактная колонка (следи за шириной, Telegram ≈ 30-40 симв.
  моноширинно), в `/signal` — блок `<pre>…</pre>` через describe_block.
- Новая команда: метод `_cmd_x`, запись в `telegram_handlers()`,
  кнопка в `MAIN_MENU_KEYBOARD` (telegram_bot.py), строка в
  `format_help_message`, тест диспетчера (`test_handlers_registry`).

## 7. Коммит/PR

- Ветка сессии (в Arena — `arena/...`), осмысленные коммиты.
- НЕ коммитить: `backtest/data_cache/`, `backtest/results/*.json`, `.env`.
- Коммитить: код, тесты, `backtest/results/*.md`, обновленные AI_AGENTS/*.
