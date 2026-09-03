# 🔥 CryptoForge Pro

Профессиональный Telegram-бот крипто-помощник. Работает **только на реальных
данных** с официальных API Binance и Bybit (опционально CoinGlass / CryptoPanic).
Никаких заглушек и mock-данных: если биржа недоступна — бот честно пишет об этом.

> ⚠️ Бот выдаёт аналитические идеи и **не является финансовой рекомендацией**. Торговля криптовалютами связана с высоким риском.

## Возможности

- 🔥 **Лучшие сетапы** — скрининг топ-монет по реальному объёму 24h.
- 📈 **Только Лонги** / 📉 **Только Шорты**.
- ⚡ **Скальп** (15m–1h) и 🎯 **Свинг** (4h–D).
- 📊 **Обзор рынка**: BTC/ETH, ширина рынка, топ роста/падения, суммарный объём, Fear & Greed.
- 🔍 **Поиск по монете / условию** (например `ETH long 1h`, `BTC`, `volume > 100m`).
- 📊 **Глубокий анализ монеты**: цена, ATR, RSI, EMA, объём, funding, OI, ликвидации, BTC-корреляция, новости.
- 📰 **Новости рынка** через CryptoPanic (при наличии ключа).
- 🔔 **Ценовые алерты**: бот в фоне проверяет реальные цены и шлёт уведомление при срабатывании.
- 📚 **История идей**: все выданные сигналы сохраняются в SQLite.
- 🧮 **Риск-калькулятор**: расчёт риска и потенциальной прибыли по R:R.
- ⚙️ **Настройки риска**: консервативный / сбалансированный / агрессивный профиль, порог уверенности.
- ℹ️ **Помощь** прямо в боте.
- Формат сигнала строго по ТЗ: вход, стоп, TP1–TP3, R:R, уверенность, обоснование, риски, дисклеймер.

## Стек

| Слой | Технология |
| --- | --- |
| Python | 3.11+ |
| Telegram | aiogram 3.x (последняя стабильная), полностью async |
| Хранилище | SQLite (`aiosqlite`) с простой миграцией на Postgres |
| Состояние | aiogram FSM |
| Биржи | Binance REST, Bybit REST |
| Производные | CoinGlass (optional, `COINGLASS_API_KEY`) |
| Новости | CryptoPanic (optional, `CRYPTOPANIC_API_KEY`) |
| Конфиг | pydantic-settings + `.env` |
| Логи | loguru |
| Деплой | Railway (Nixpacks + `railway.json` + `Procfile`) |

## Быстрый старт

```bash
# 1. Python 3.11+
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Токен
cp .env.example .env
# заполни TELEGRAM_TOKEN (от @BotFather)

# 3. Запуск
python -m cryptoforge_pro.main
# или
./start.sh
# или
python main.py
```

## Команды Telegram

| Команда | Что делает |
| --- | --- |
| `/start` | Главное меню |
| `/scan` | Лучшие сетапы сейчас |
| `/analyze BTC` | Глубокий анализ монеты |
| `/search ETH long 1h` | Поиск по условию |
| `/market` | Обзор рынка |
| `/news` | Новости рынка |
| `/alerts` | Ценовые алерты |
| `/history` | История идей |
| `/risk` | Риск-калькулятор |
| `/status` | Проверка доступа к реальным данным |
| `/settings` | Настройки риска |
| `/help` | Помощь |

## Настройки риска

Профиль задаёт минимальный порог уверенности:

- 🛡️ **Консервативный** — 58%
- ⚖️ **Сбалансированный** — 62%
- 🚀 **Агрессивный** — 67%

Порог можно изменить вручную от 40 до 95 через меню.

## Переменные окружения

См. `.env.example`. Для Railway задай переменные в панели проекта:

- `TELEGRAM_TOKEN` — **обязательно**.
- `ADMIN_CHAT_IDS`, `ALLOWED_CHAT_IDS` — опционально, через запятую.
- `EXCHANGES=binance,bybit`.
- `COINGLASS_API_KEY`, `CRYPTOPANIC_API_KEY` — опционально.
- `ALERT_CHECK_INTERVAL_SECONDS`, `ALERT_PRICE_WINDOW_PCT` — настройки вотчера.
- `RISK_PROFILES`, `MIN_CONFIDENCE` — тюнинг движка.

## Деплой на Railway

1. Загрузи репозиторий в GitHub.
2. В Railway: New Project → Deploy from GitHub → выбери репозиторий.
3. Добавь переменную `TELEGRAM_TOKEN`.
4. Railway сам увидит `railway.json` / `nixpacks.toml` и запустит `python -m cryptoforge_pro.main`.

`Procfile` также поддерживает режим worker.

## Архитектура

```
cryptoforge_pro/
├── config.py              # pydantic-settings + .env
├── models.py              # Candle, Ticker, Derivatives, MarketData, Signal
├── db.py                  # aiosqlite, users + signals
├── text_parse.py          # детерминированный парсер запросов
├── data/
│   ├── http.py            # async httpx session
│   ├── exchanges.py       # Binance + Bybit REST
│   ├── coinglass.py       # CoinGlass (optional)
│   ├── news.py            # CryptoPanic (optional)
│   └── sentiment.py       # Fear & Greed (alternative.me)
├── market.py              # агрегатор рынка, BTC-контекст
├── alerts.py              # фоновый вотчер ценовых алертов
├── analysis/
│   ├── indicators.py      # EMA, RSI, ATR, MACD, объём, структура
│   └── engine.py          # signal score / entry / stop / TP / confidence
├── telegram/
│   ├── handlers.py        # aiogram handlers (menu, scan, analyze, settings)
│   ├── keyboards.py       # inline-клавиатуры
│   ├── format.py          # HTML-формат сигнала и разбора
│   ├── context.py         # DI-контекст
│   └── states.py          # FSM
├── app.py                 # bootstrap
├── main.py                # точка входа
└── selftest.py            # live self-test (реальные API)
```

## Проверка без токена

```bash
python -m cryptoforge_pro.selftest
```

Self-test использует живые публичные API Binance/Bybit. Если сеть до биржи недоступна — тест завершится ошибкой, а не подставит синтетику.

## Как проверить живые данные

В меню нажмите **🩺 Проверка данных** или отправьте `/status`.
Бот выполнит live-проверку:
- `binance` / `bybit` — доступность публичного REST;
- `coinglass` — активен только при `COINGLASS_API_KEY`;
- `cryptopanic` — активен только при `CRYPTOPANIC_API_KEY`;
- `fear_greed` — external.me (опционально).

Команда честно покажет зелёный/жёлтый/красный статус для каждого источника.
Если на Railway виден красный — проверьте исходящий интернет и DNS/домены
`api.binance.com`, `api.bybit.com`.

## Честность данных

- Цены, свечи, объёмы — только Binance/Bybit официальные публичные REST.
- Funding / Open Interest — Binance Futures, Bybit Linear, опционально CoinGlass.
- News — CryptoPanic при наличии ключа.
- Fear & Greed — alternative.me (optional noise layer, never a trading signal alone).
- Если источник недоступен, бот сообщает об этом и не выдумывает числа.
