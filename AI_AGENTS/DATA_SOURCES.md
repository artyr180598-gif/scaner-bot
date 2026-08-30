# DATA_SOURCES.md — где брать данные для бектеста (проверено в песочнице Arena)

Обновлено: 2026-08-30. Среда: Linux-песочница Arena с **бело-списочной сетью**.

## Сетевая реальность песочницы (проверено curl'ом)

| Домен | Статус | Для чего |
|---|---|---|
| `github.com` (git clone, smart HTTP) | ✅ работает | клон репозиториев с данными |
| `api.github.com` | ✅ | поиск, метаданные, содержимое файлов (git blobs до 100 МБ, raw через `Accept: application/vnd.github.raw`) |
| `codeload.github.com` | ✅ | архивы репо (zip/tar) |
| `release-assets.githubusercontent.com` | ❌ (EOF/блок) | релизные аттачи НЕ скачать (gh release download падает) |
| `raw.githubusercontent.com`, `cdn.jsdelivr.net`, `huggingface.co`, `storage.googleapis.com` | ❌ | зеркала недоступны |
| `pypi.org`, `files.pythonhosted.org`, `registry.npmjs.org` | ✅ | pip/npm пакеты |
| `api.binance.com`, `fapi.binance.com`, `data.binance.vision`, `api.bybit.com` (CloudFront-блок), `api.gateio.ws` (требует заголовки) | ❌ напрямую | биржевые API из песочницы закрыты |
| `www.okx.com`, `api.bitget.com` | ⚠️ только через платформенный инструмент `fetch_page` (не из bash!) | мелкие запросы JSON; НЕ для мегабайт — ответ приходит в контекст агента |
| google.com и прочий «обычный» интернет | ❌ из bash / ✅ через `web_search` | поиск — инструментом, не curl'ом |

Вывод: **всё тяжёлое качаем с GitHub через git clone**, мелкие проверки —
`fetch_page`, поиск — `web_search`.

## Основной источник (используется сейчас): brasdor/UngerFink-TREND

Публичный репозиторий, автор которого автоматически обновляет РЕАЛЬНЫЕ данные
Binance (скрипты качают fapi.binance.com + data.binance.vision из GitHub
Actions — у них нет гео-блока). Мы используем ТОЛЬКО CSV-данные, никакой код.

Что там есть (проверено 2026-08-30):
- `data/futures_universe/funding_rates/*USDT_funding.csv` — funding 8ч,
  ~290 перпов, **2019-09 → сейчас** (обновляется);
- `data/futures_universe/ohlcv_4h/`, `ohlcv_1d/` — перп-кладлы 4ч/1д;
- `data/universe/ohlcv_1d/*_USDT_1d.csv` — **спот** 1д (~420 монет);
- `data/research_intradaybias_t1/ohlcv_cache/*_USDT_1h.csv` — **спот 1ч**
  для 70 монет (мажоры; 2021-11 → 2026-05, 40k строк на монету).

Пересечение всех наборов = **49 монет с полным комплектом** (спот 1ч/1д,
перп 4ч/1д, funding). Схемы файлов отличаются (ISO-время у спота, мс у перпа)
— нормализацию делает `backtest/prepare_data.py`.

Как обновить: `./backtest/fetch_data.sh && .venv/bin/python backtest/prepare_data.py --prune-raw`
(клон ~600 МБ → кеш ~40-70 МБ гзипнутых CSV).

## Альтернативы / где искать ещё (не проверено до конца)

- **powakadata/powakadata-crypto-funding-sample** — funding+OI 8 мажоров
  2019→now (сэмпл, полный — платный).
- **`gh api search/repositories?q=...`** — искать по темам
  `topic:dataset topic:klines`, `funding rate csv`, `historical candles`.
  Код-поиск: `gh api search/code?q=funding_rate+extension:csv`.
- **fetch_page + OKX**: `https://www.okx.com/api/v5/market/history-candles?instId=BTC-USDT&bar=1H&limit=100`
  — работает, лимит 100 свечей/запрос; годится для проверки гипотез,
  НЕ для выкачки месяцев (каждый ответ грузится в контекст).
- **fetch_page + Bitget v2**: `api.bitget.com/api/v2/spot/market/candles?symbol=BTCUSDT&granularity=1h&limit=...`
  и фьючерсы `/api/v2/mix/market/candles` + funding history — до 1000
  свечей/запрос; тот же ограничение по контексту.
- **Чего НЕ нашли**: синхронные мультибиржевые спредовые данные (Bybit/OKX/
  Gate спот vs Binance перп на одну сетку времени). Если найдёте — это
  закроет главный пробел бектеста (см. BRAIN.md, открытые вопросы).

## Ловушки (грабли, на которые уже наступали)

1. **pandas 3.x**: `pd.to_datetime(...).astype("int64")` — datetime64[us],
   НЕ наносекунды; делить на 1e6 нельзя. Только через
   `.dt.tz_convert("UTC").dt.tz_localize(None).astype("datetime64[ns]")`.
2. У спот-1д файлов встречаются битые строки с датой `1970-01-01` — фильтр
   `ts >= 2017-01-01` в `prepare_data.py`.
3. У funding-файлов значения — ДОЛЯ за 8ч (0.0001 = 0.01%); prepare_data
   умножает на 100 → `rate_pct`.
4. `git clone --filter=blob:none --sparse` качает блобы лениво — экономит
   гигабайты; полный клон того репо ≈ 1+ ГБ.
5. Данные чужого репо: перед крупным использованием сверить пару значений
   с реальностью (мы сверяли funding BTC с текущими ставками — совпадает).
