FROM hummingbot/hummingbot-api:latest

WORKDIR /hummingbot-api

RUN mkdir -p \
    bots/conf/controllers \
    bots/credentials/master_account/connectors \
    bots/data/backtests/candles

# Keep the official Hummingbot API entrypoint and filesystem layout.
