# CryptoPilot 3.6

Telegram-система для поиска ранних сценариев на ликвидных бессрочных USDT-фьючерсах.
Основной принцип версии 3.6 — **PRE-MOVE first**: лучше выдать `NO TRADE`, чем показать
красивый сигнал после того, как основная свеча уже ушла.

Бот не размещает реальные ордера. Он собирает публичные данные Bybit/Binance, фильтрует
рынок, строит сценарии LONG/SHORT, рассчитывает зону входа, stop и TP, а затем проверяет
качество стратегии на последующих данных.

## Что изменилось в 3.6

Вся пользовательская цепочка переведена на ранний поиск:

- основной `/scan` ранжирует не самые сильные уже идущие тренды, а лучшие
  **pre-move opportunities**;
- уже подтверждённый объёмный пробой, высокий RVOL, сильное удаление от VWAP/EMA и
  реализованный импульс являются причинами отказаться от нового входа;
- `/early` теперь может перейти в `ARMED_PREMOVE` **до** 15m-пробоя; пробой не
  превращается задним числом в ранний сигнал;
- `/smartmoney` по умолчанию скрывает стадию `ENTRY`, потому что она уже после BOS;
- `/prime` остаётся самым строгим ранним режимом: структура + compression + spot +
  derivatives + liquidity + live flow + независимая проверка второй биржей;
- live WebSocket умеет видеть `EARLY_PRESSURE`: OI/CVD/taker-flow начинают смещаться,
  но цена и объём ещё не разогнаны. Такое событие немедленно запускает повторную
  PRIME-проверку конкретной монеты;
- `/best` больше не показывает старые сохранённые сигналы. Он делает новый анализ рынка
  и возвращает PRIME, ранний Smart Money, ранний compression-кандидат или `NO TRADE`;
- `/analyze BTC` сначала проходит PRIME/Smart Money pre-move стек и только затем
  основной SignalEngine;
- старые trend-auto уведомления выключены по умолчанию, чтобы не конкурировать с PRIME
  поздними сигналами;
- статистика старой стратегии не смешивается с новой: production paper-калибровка имеет
  отдельную версию `premove-3.6`, PRIME Shadow — `prime-3.6`.

## Как устроен поиск до движения

```text
USDT perpetual universe
  → liquidity / spread filter
  → 15m pre-move ranking
      compression
      distance to structure trigger
      range pressure
      RVOL not expanded
      ATR not expanded
      VWAP / EMA chase protection
  → 1h + 4h direction guard
  → BTC regime / relative strength
  → OI / funding / taker flow / order book
  → Smart Money deep check
      spot taker flow
      spot order book
      spot block-trade proxy
      perp/spot basis
      persistent liquidity walls
      replenishment
      liquidations
  → Binance ↔ Bybit independent confirmation
  → live EARLY_PRESSURE / absorption / CVD / OI acceleration
  → executable PRIME plan
      ENTRY ZONE
      STOP
      TP1 / TP2 / TP3
      expiry
      risk amount
  → Telegram or NO TRADE
```

Ни один отдельный индикатор не считается доказательством будущего движения. Публичные
данные также не позволяют достоверно определить личность или намерения конкретного
«кита». Система ищет измеримые следы подготовки, а не выдумывает скрытые ордера.

## PRIME

PRIME — самый строгий пользовательский сигнал. Чтобы он появился, цена должна ещё
находиться до структурного trigger и пройти независимые группы фильтров.

В типичном PRIME-сообщении есть:

- направление LONG/SHORT и PRIME score;
- текущая цена и структурный trigger;
- точная зона входа;
- защитный stop;
- TP1 / TP2 / TP3;
- net R/R для TP2 с учётом заданных издержек;
- срок действия входа;
- 15m / 1h / 4h контекст;
- Spot / OI / funding / live flow / liquidity evidence;
- подтверждение второй биржей;
- ссылка на соответствующий контракт TradingView.

Если цена уже слишком близко к trigger, слишком далеко от trigger, уже пробила структуру
с объёмом или не позволяет построить приемлемый stop/RR, PRIME-план не создаётся.

## Live early pressure

Bybit WebSocket отслеживает отобранный shortlist, а не пытается подписаться на весь рынок.
Новый `EARLY_PRESSURE` предназначен именно для раннего повторного анализа.

Он ищет комбинацию:

- directional taker delta уже смещается;
- CVD proxy начинает поддерживать сторону;
- open interest растёт;
- OI acceleration становится положительной;
- объём растёт умеренно, но ещё не находится в полном burst;
- цена за 60 секунд остаётся относительно спокойной;
- структурный trigger находится недалеко.

`EARLY_PRESSURE` сам по себе **не является входом**. Он служит событием, которое заставляет
бот немедленно пересчитать PRIME для этой монеты вместо ожидания следующего полного REST-скана.

## Проверка второй биржей

При основной бирже Bybit PRIME дополнительно проверяется на Binance, и наоборот.
Сравниваются 15m/1h структура, OI, taker-flow и расхождение цены.

Для автоматического PRIME эта проверка по умолчанию обязательна. Если вторая биржа
недоступна или показывает несколько сильных конфликтов, система работает по принципу
fail-closed и не отправляет PRIME.

## Shadow learning и статистика

### `/primestats`

Бот молча сохраняет более широкий набор PRIME-планов и проверяет, что произошло потом.
Shadow-исполнение консервативное:

- вход считается только после реального касания зоны;
- если SL и TP попали внутрь одной OHLC-свечи, первым считается SL;
- `NO_ENTRY` не записывается искусственным проигрышем;
- учитываются модельные торговые издержки;
- рассчитываются win rate, expectancy в R и profit factor;
- до минимальной выборки вывод помечается как недостаточный.

Shadow-данные старых поколений не смешиваются с `prime-3.6`.

### `/flowstats`

Forward validation теперь показывает отдельно:

- `EARLY_PRESSURE`;
- `FLOW_BUILDUP`;
- `ABSORPTION`.

Это важно: раннее давление нельзя оценивать одной статистикой с уже более зрелым потоком.
Flow validation проверяет достижение структурного trigger и lead time, а не гарантированную
прибыль сделки.

### `/performance`

Paper-статистика основного SignalEngine фильтруется по текущему
`strategy_version=premove-3.6`, поэтому старые trend-сигналы не искажают новую калибровку.

## Telegram

Основные кнопки и команды:

- `🔎 Лучшие до движения` / `/scan` — полный PRE-MOVE скан;
- `🪙 Анализ монеты` / `/analyze ETH` — PRIME-first анализ одной монеты;
- `⚡ До импульса` / `/early` — compression / ARMED_PREMOVE;
- `🐋 Крупный капитал` / `/smartmoney` — Smart Money PRE-MOVE;
- `🎯 PRIME` / `/prime` — самые строгие ранние кандидаты;
- `⭐ Лучший сейчас` / `/best` — один свежий лучший вариант или NO TRADE;
- `/primestats` — PRIME Shadow;
- `/flowstats` — forward-проверка live pre-move событий;
- `📈 Результаты` / `/performance` — paper-статистика текущего SignalEngine;
- `📊 Бэктест` / `/backtest ETH` — исторический trend-baseline. Он не является
  точной симуляцией PRIME 3.6, потому что исторические свечи не содержат всего live
  Spot/order-book/CVD/OI evidence;
- `⚙️ Статус` / `/status` — версия, API и режимы;
- `/live` — состояние WebSocket радара;
- `/lab` — изолированная squeeze-лаборатория;
- `/help`, `/menu`.

## Консервативные defaults

Ключевые значения из `.env.example`:

| Variable | Default | Назначение |
|---|---:|---|
| `STANDARD_AUTO_ALERTS_ENABLED` | `false` | Не отправлять старые trend-auto сигналы |
| `MAIN_SCAN_PREMOVE_ONLY` | `true` | Основной скан блокирует поздние входы |
| `MAIN_SCAN_MIN_PREMOVE_READINESS` | `72` | Минимальная pre-move готовность |
| `SMART_MONEY_SCAN_INTERVAL_SECONDS` | `180` | Полное обновление Smart Money shortlist |
| `SMART_MONEY_INCLUDE_POST_BREAKOUT` | `false` | Не показывать ENTRY после BOS |
| `FLOW_EARLY_PRESSURE_ENABLED` | `true` | Live раннее давление |
| `FLOW_AUTO_ALERTS_ENABLED` | `false` | Flow сам не спамит торговыми сообщениями |
| `EARLY_AUTO_ALERTS` | `false` | Ранний radar не спамит наблюдениями |
| `PRIME_ALERTS_ENABLED` | `true` | Строгий PRIME может приходить автоматически |
| `PRIME_MIN_SCORE` | `88` | Минимум PRIME |
| `PRIME_CROSS_EXCHANGE_REQUIRED` | `true` | Обязательная вторая биржа |
| `PRIME_MIN_PLAN_RR` | `2.0` | Минимальный net R/R TP2 |
| `PRIME_MAX_LEVERAGE` | `2` | Верхний предел PRIME-плана |
| `RISK_PER_TRADE_PCT` | `0.5` | Расчётный риск на сделку |

## Railway

1. Подключите GitHub-репозиторий как Railway service.
2. Добавьте `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID`.
3. Оставьте `EXCHANGE=bybit` для полного Bybit WebSocket flow; Binance используется как
   независимое подтверждение PRIME.
4. Для постоянной SQLite-истории подключите Railway Volume и задайте `DATA_DIR=/data`.
5. Railway запускает `python -m cryptopilot`; health endpoints: `/healthz`, `/readyz`,
   `/metrics`.

Публичные market-data endpoints не требуют API-ключей для анализа. Бот не использует
ключи для размещения ордеров.

## Локальная проверка

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest ruff
ruff check .
python -m compileall -q cryptopilot
pytest
docker build -t cryptopilot .
```

## Ограничения

PRE-MOVE означает попытку обнаружить условия, которые исторически/в forward-наблюдении
возникают **до** расширения цены. Это не означает, что движение обязательно произойдёт.
Ложные накопления, spoofing стакана, ликвидационные импульсы, новости, гэпы и задержки данных
остаются возможными.

Поэтому главный показатель качества версии 3.6 — не количество сообщений и не внутренний
score, а накопленная forward expectancy после достаточной выборки. Если подтверждений нет,
правильный ответ системы — `NO TRADE`.
