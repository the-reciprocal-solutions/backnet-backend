from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from bacnet_lab.adapters.http.routers import (
    anomaly_feed,
    assets,
    copilot,
    devices,
    discovery,
    endpoints,
    events,
    forecast,
    health,
    history,
    metrics,
    predictions,
    protocols,
    scenarios,
    simulation,
    timeseries,
    ws,
)


def create_app(auth_username: str = "", auth_password: str = "") -> FastAPI:
    app = FastAPI(title="BACnet Lab", version="0.1.0")

    if auth_username and auth_password:
        from bacnet_lab.adapters.http.auth import BasicAuthMiddleware

        app.add_middleware(BasicAuthMiddleware, username=auth_username, password=auth_password)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/ui")

    app.include_router(health.router)
    app.include_router(devices.router)
    app.include_router(discovery.router)
    app.include_router(scenarios.router)
    app.include_router(endpoints.router)
    app.include_router(events.router)
    app.include_router(simulation.router)
    app.include_router(history.router)
    app.include_router(timeseries.router)
    app.include_router(protocols.router)
    app.include_router(forecast.router)
    app.include_router(copilot.router)
    app.include_router(metrics.router)
    app.include_router(assets.router)
    app.include_router(predictions.router)
    app.include_router(anomaly_feed.router)
    app.include_router(ws.router)

    # Web UI
    from bacnet_lab.adapters.web.router import router as web_router

    app.include_router(web_router)

    static_dir = Path(__file__).parent.parent / "web" / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    return app
