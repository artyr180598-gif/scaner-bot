# CryptoPilot 2.0

Telegram‑помощник для анализа ликвидных бессрочных USDT‑фьючерсов Bybit или Binance.
Система не открывает позиции и не имеет модуля исполнения ордеров: она собирает публичные
рыночные данные, отбрасывает слабые ситуации и формирует объяснимый план `LONG`, `SHORT`
или `NO TRADE`.

## Что делает бот

- автоматически проверяет рынок с заданным интервалом;
- сначала фильтрует торгуемые USDT perpetual по обороту и bid/ask‑спреду;
- делает быстрый отбор universe, затем углублённо анализирует shortlist;
- использует только закрытые свечи и три таймфрейма (по умолчанию 15m/1h/4h);
- учитывает EMA 20/50/200, RSI, ATR, ADX, MACD, Bollinger position, объём, пробой структуры,
  funding и рыночный режим BTC;
- отправляет автоматический сигнал только выше строгого порога уверенности;
- выдаёт зону входа, технический стоп, TP1/TP2/TP3, срок действия, отмену сценария,
  размер позиции по заданному риску, причины и риски;
- хранит историю в SQLite и не повторяет одинаковый алерт до окончания cooldown;
- выполняет консервативный walk‑forward backtest без утечки будущих свечей;
- имеет `/healthz`, `/readyz` и `/metrics` для Railway.

Уверенность в сообщении — **внутренний рейтинг согласованности модели**, а не обещание
процента выигрышных сделок. Значение намеренно ограничено 89%. Если данных или edge
недостаточно, правильный результат — `NO TRADE`.

## Telegram

Кнопки и команды:

- `🔎 Сканировать рынок` / `/scan` — ручной полный скан;
- `🪙 Анализ монеты` / `/analyze BTC` — подробный разбор одной монеты;
- `⭐ Лучшие сигналы` / `/best` — последние сохранённые планы;
- `📊 Бэктест` / `/backtest ETH` — walk‑forward тест на 1h;
- `⚙️ Статус` / `/status` — Telegram, API и автомониторинг;
- `❓ Помощь` / `/help` — правила использования.

Доступ разрешён только ID из `TELEGRAM_CHAT_ID`. Это обязательный защитный барьер.

## Railway

1. Подключите репозиторий как Railway service.
2. Добавьте Variables:
   - `TELEGRAM_BOT_TOKEN` — токен BotFather;
   - `TELEGRAM_CHAT_ID` — ваш numeric ID; несколько ID можно указать через запятую;
   - `EXCHANGE=bybit` либо `EXCHANGE=binance`.
3. При необходимости измените параметры из `.env.example`.
4. Railway автоматически использует `Dockerfile`, запускает `python -m cryptopilot` и
   проверяет `/healthz`.
5. Для постоянной истории подключите Railway Volume и задайте `DATA_DIR=/data`.

Старые имена переменных `TELEGRAM_TOKEN`, `BOT_TOKEN`, `TG_TOKEN`, `BOT_API_TOKEN`,
`TELEGRAM_USER_ID`, `CHAT_ID`, `ALLOWED_CHAT_IDS`, `ADMIN_CHAT_IDS` и `ADMIN_ID`
поддерживаются, чтобы существующий деплой не сломался. Также сохранена совместимость с
`MIN_VOLUME_USD_24H`, `TOP_N_SYMBOLS` и `HTTP_TIMEOUT`.
Ключи Bybit/Binance для анализа не нужны: используются публичные market-data endpoints.

## Основные настройки

| Variable | Default | Назначение |
|---|---:|---|
| `SCAN_INTERVAL_SECONDS` | `900` | Период автоматического скана, минимум 300 сек |
| `MIN_VOLUME_USDT` | `20000000` | Минимальный суточный оборот |
| `MAX_SPREAD_BPS` | `12` | Максимальный bid/ask spread |
| `UNIVERSE_SIZE` | `80` | Сколько ликвидных рынков быстро проверить |
| `SHORTLIST_SIZE` | `12` | Сколько кандидатов разобрать глубоко |
| `MIN_AUTO_CONFIDENCE` | `78` | Порог автоматического алерта |
| `ALERT_COOLDOWN_MINUTES` | `180` | Защита от повторов |
| `ACCOUNT_EQUITY_USDT` | `1000` | База для иллюстрации риска |
| `RISK_PER_TRADE_PCT` | `0.5` | Максимальный риск по стопу |
| `MAX_POSITION_PCT` | `25` | Ограничение расчётного notional |

## Логика решения

```text
active USDT perpetuals
  → liquidity/spread filter
  → top universe by turnover
  → quick 1h directional score
  → full 15m + 1h + 4h analysis
  → BTC regime + funding + data-quality gates
  → risk plan or NO TRADE
  → confidence threshold + deduplication
  → Telegram
```

Автоматический порог должен быть не ниже ручного. LONG блокируется против сильного
медвежьего режима BTC, SHORT — против сильного бычьего. Также блокируются устаревшие
данные, слабая ликвидность, широкий спред, конфликт старших таймфреймов, экстремальный
funding, слишком низкая/высокая волатильность и вход после чрезмерного удаления от EMA20.

## Локальная проверка

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest ruff
ruff check .
pytest
TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python -m cryptopilot
```

## Источники market API

- [Bybit V5 Get Kline](https://bybit-exchange.github.io/docs/v5/market/kline)
- [Bybit V5 Get Tickers](https://bybit-exchange.github.io/docs/v5/market/tickers)
- [Bybit V5 Instruments Info](https://bybit-exchange.github.io/docs/v5/market/instrument)
- [Binance USDⓈ‑M Futures Market Data](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/market-data/rest-api/Kline-Candlestick-Data)

## Важное ограничение

Фьючерсы высокорискованны. Backtest и рейтинг уверенности не гарантируют будущую
доходность. Перед реальными деньгами используйте paper trading, ограничивайте риск и
проверяйте цену/спред непосредственно на бирже. Бот специально не содержит автоторговлю,
martingale и скрытое увеличение позиции.
