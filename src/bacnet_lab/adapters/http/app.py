from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from bacnet_lab.adapters.http.dependencies import (
    clear_container,
    get_container,
    has_container,
    set_container,
)
from bacnet_lab.adapters.http.routers import (
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
    scenarios,
    simulation,
)
from bacnet_lab.bootstrap import create_container
from bacnet_lab.infrastructure.config import load_settings


def create_app(auth_username: str = "", auth_password: str = "") -> FastAPI:
    app = FastAPI(title="BACnet Lab", version="0.1.0")
    app.state.owns_container = False

    if auth_username and auth_password:
        from bacnet_lab.adapters.http.auth import BasicAuthMiddleware

        app.add_middleware(BasicAuthMiddleware, username=auth_username, password=auth_password)

    @app.on_event("startup")
    async def startup() -> None:
        if has_container():
            return
        settings = load_settings()
        container = await create_container(settings)
        set_container(container)
        app.state.owns_container = True

    @app.on_event("shutdown")
    async def shutdown_container() -> None:
        if not app.state.owns_container:
            return

        container = get_container()
        await container.copilot_service.stop()
        await container.forecast_scheduler.stop()
        await container.forecast_service.stop()
        await container.historian_service.stop()
        await container.simulation_engine.stop()
        await container.telemetry_service.stop()
        await container.device_service.shutdown()
        clear_container()

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/ui")

    app.include_router(health.router)
    app.include_router(devices.router)
    app.include_router(scenarios.router)
    app.include_router(endpoints.router)
    app.include_router(events.router)
    app.include_router(simulation.router)
    app.include_router(history.router)
    app.include_router(forecast.router)
    app.include_router(copilot.router)
    app.include_router(metrics.router)
    app.include_router(assets.router)
    app.include_router(predictions.router)
    app.include_router(discovery.router)

    # WebSocket router
    from bacnet_lab.adapters.web.websocket import router as websocket_router
    app.include_router(websocket_router)

    # Web UI
    from bacnet_lab.adapters.web.router import router as web_router

    app.include_router(web_router)

    static_dir = Path(__file__).parent.parent / "web" / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    return app
