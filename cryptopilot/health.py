from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from aiohttp import web


@dataclass(slots=True)
class RuntimeHealth:
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    ready: bool = False
    bot_username: str = ""
    exchange: str = ""
    last_error: str | None = None
    scans_total: int = 0
    alerts_total: int = 0


async def start_health_server(state: RuntimeHealth, port: int) -> web.AppRunner:
    async def healthz(_: web.Request) -> web.Response:
        payload = {
            "status": "ok" if state.ready else "starting",
            "ready": state.ready,
            "bot": state.bot_username,
            "exchange": state.exchange,
            "started_at": state.started_at.isoformat(),
            "last_error": state.last_error,
        }
        return web.json_response(payload, status=200 if state.ready else 503)

    async def metrics(_: web.Request) -> web.Response:
        uptime = max(0.0, (datetime.now(UTC) - state.started_at).total_seconds())
        body = (
            "# TYPE cryptopilot_ready gauge\n"
            f"cryptopilot_ready {int(state.ready)}\n"
            "# TYPE cryptopilot_uptime_seconds gauge\n"
            f"cryptopilot_uptime_seconds {uptime:.0f}\n"
            "# TYPE cryptopilot_scans_total counter\n"
            f"cryptopilot_scans_total {state.scans_total}\n"
            "# TYPE cryptopilot_alerts_total counter\n"
            f"cryptopilot_alerts_total {state.alerts_total}\n"
        )
        return web.Response(text=body, content_type="text/plain")

    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/readyz", healthz)
    app.router.add_get("/metrics", metrics)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner
