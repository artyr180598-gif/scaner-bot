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

# Patch Docker-dependent orchestration services for Railway research mode.
# No host Docker socket is available, so bot-container/Gateway management is disabled;
# market-data and backtesting services remain available.

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

g = Path("services/gateway_service.py")
gs = g.read_text()
gs = gs.replace(
    "import docker\n",
    "import docker\n\n\nclass _DisabledGatewayContainers:\n    def get(self, *args, **kwargs):\n        raise docker.errors.NotFound(\"Docker disabled in Railway research mode\")\n\n\nclass _DisabledGatewayClient:\n    containers = _DisabledGatewayContainers()\n    networks = _DisabledGatewayContainers()\n\n\n_DISABLED_GATEWAY_CLIENT = _DisabledGatewayClient()\n"
)
gs = gs.replace(
    "self.client = docker.from_env()",
    "self.client = (\n            _DISABLED_GATEWAY_CLIENT\n            if os.getenv(\"HUMMINGBOT_DISABLE_DOCKER\", \"false\").lower() in {\"1\", \"true\", \"yes\"}\n            else docker.from_env()\n        )"
)
g.write_text(gs)
PY
