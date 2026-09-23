FROM hummingbot/hummingbot-api:latest

WORKDIR /hummingbot-api

RUN mkdir -p \
    bots/conf/controllers \
    bots/credentials/master_account/connectors \
    bots/data/backtests/candles \
    bots/controllers/directional_trading

COPY scaner_premove_v1.py bots/controllers/directional_trading/scaner_premove_v1.py
COPY scaner_premove_v1_btc.yml bots/conf/controllers/scaner_premove_v1_btc.yml

# Railway does not expose a host Docker socket. Keep the API usable for research/backtesting
# while disabling only live bot-container orchestration; trading/backtesting routes remain loaded.
RUN python - <<'PY'
from pathlib import Path

p = Path("services/bots_orchestrator.py")
s = p.read_text()
s = s.replace(
    "import docker\n",
    "import docker\n\n\nclass _DisabledDockerContainers:\n    def list(self):\n        return []\n\n\nclass _DisabledDockerClient:\n    containers = _DisabledDockerContainers()\n"
)
s = s.replace(
    "self.docker_client = docker.from_env()",
    "self.docker_client = (\n"
    "            _DisabledDockerClient()\n"
    "            if os.getenv(\"HUMMINGBOT_DISABLE_DOCKER\", \"false\").lower() in {\"1\", \"true\", \"yes\"}\n"
    "            else docker.from_env()\n"
    "        )"
)
p.write_text(s)
PY
