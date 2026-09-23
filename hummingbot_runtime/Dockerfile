FROM hummingbot/hummingbot-api:latest

WORKDIR /hummingbot-api

RUN mkdir -p \
    bots/conf/controllers \
    bots/credentials/master_account/connectors \
    bots/data/backtests/candles \
    bots/controllers/directional_trading

COPY scaner_premove_v1.py bots/controllers/directional_trading/scaner_premove_v1.py
COPY scaner_premove_v1_btc.yml bots/conf/controllers/scaner_premove_v1_btc.yml
