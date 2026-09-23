FROM hummingbot/hummingbot-api:latest

WORKDIR /hummingbot-api

RUN mkdir -p \
    bots/conf/controllers \
    bots/credentials/master_account/connectors \
    bots/data/backtests/candles

# Install Scaner's Hummingbot V2 PREMOVE research controller/config.
COPY hummingbot/controllers/directional_trading/scaner_premove_v1.py bots/controllers/directional_trading/scaner_premove_v1.py
COPY hummingbot/configs/scaner_premove_v1_btc.yml bots/conf/controllers/scaner_premove_v1_btc.yml

# Keep the official Hummingbot API entrypoint.
