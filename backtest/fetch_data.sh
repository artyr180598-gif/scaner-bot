#!/usr/bin/env bash
# fetch_data.sh — загрузка сырых исторических данных для бектеста.
#
# Источник: публичный GitHub-репозиторий brasdor/UngerFink-TREND, где автор
# автоматически обновляет реальные данные Binance (спот klines, перп klines
# 1d/4h, funding 8ч) с 2019 года по сейчас. Мы берём ТОЛЬКО данные,
# никакой код того репозитория не используется.
#
# Требования: git с доступом к github.com (в песочнице Arena работает;
# прямые API бирж заблокированы сетью — см. AI_AGENTS/DATA_SOURCES.md).
#
# Качается ~600 МБ (sparse partial clone). Затем запусти:
#   .venv/bin/python backtest/prepare_data.py --prune-raw

set -euo pipefail
cd "$(dirname "$0")"
mkdir -p data_cache
rm -rf data_cache/raw_uf

git clone --depth 1 --filter=blob:none --sparse \
  https://github.com/brasdor/UngerFink-TREND.git data_cache/raw_uf

cd data_cache/raw_uf
git sparse-checkout set \
  data/futures_universe/funding_rates \
  data/futures_universe/ohlcv_4h \
  data/futures_universe/ohlcv_1d \
  data/universe/ohlcv_1d \
  data/research_intradaybias_t1/ohlcv_cache

echo "OK: $(find . -name '*.csv' | wc -l) CSV скачано в backtest/data_cache/raw_uf"
