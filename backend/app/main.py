import asyncio
from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.api.analytics_routes import analytics_router
from app.api.business_routes import business_router
from app.api.payment_routes import payment_router
from app.api.routes import router as api_router
from app.api.tiktok_fashion_routes import router as tiktok_router
from app.api.zalo_routes import zalo_router
from fastapi.staticfiles import StaticFiles
from app.business.orchestrator import AutonomousBusinessOrchestrator
from app.core.config import configure_safe_logging, get_settings
import app.core.database as db_module
from app.core.database import Base
from app.events.bus import event_bus
import app.models  # noqa: F401
from app.services.real_news_engine import run_30min_periodic_news_loop
from app.services.seed import recover_interrupted_tasks_on_startup, seed_initial_data
from app.services.zalo_guardian_service import (
    apply_windows_anti_sleep,
    discover_and_sync_tunnels,
    get_or_create_zalo_config,
    zalo_guardian_service,
)

logger = logging.getLogger("app.main")


async def _bounded_watchdog_loop() -> None:
    """Bounded periodic watchdog checking stalled tasks every 30s without infinite agent loops."""
    orch = AutonomousBusinessOrchestrator()
    while True:
        await asyncio.sleep(30.0)
        try:
            with db_module.SessionLocal() as db:
                orch.detect_and_recover_stalled_tasks(db)
        except Exception as exc:
            logger.warning("Bounded watchdog error: %s", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_safe_logging()
    Base.metadata.create_all(bind=db_module.engine)
    with db_module.SessionLocal() as db:
        seed_initial_data(db)
        recover_interrupted_tasks_on_startup(db)
        z_cfg = get_or_create_zalo_config(db)
        if z_cfg.anti_sleep_enabled:
            apply_windows_anti_sleep(True)
        db.commit()
    discover_and_sync_tunnels()
    await zalo_guardian_service.ensure_cloud_webhook_registered(send_boot_notice=True)
    watchdog_task = asyncio.create_task(_bounded_watchdog_loop())
    news_crawler_task = asyncio.create_task(run_30min_periodic_news_loop())
    zalo_polling_task = asyncio.create_task(zalo_guardian_service.run_zalo_polling_loop())
    zalo_monitor_task = asyncio.create_task(zalo_guardian_service.run_proactive_health_monitor_loop())
    try:
        yield
    finally:
        watchdog_task.cancel()
        news_crawler_task.cancel()
        zalo_polling_task.cancel()
        zalo_monitor_task.cancel()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        description="Autonomous AI Company Management System with 2.5D Isometric Operations Interface",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1|.*\.github\.io|.*\.onrender\.com|.*\.trycloudflare\.com)(:\d+)?",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router)
    app.include_router(business_router)
    app.include_router(tiktok_router)
    app.include_router(zalo_router)
    app.include_router(payment_router)
    app.include_router(analytics_router)

    @app.websocket("/ws/events")
    async def websocket_events(websocket: WebSocket, since_seq: int = Query(default=0, ge=0)) -> None:
        await websocket.accept()
        # Replay missed events immediately upon connect/reconnect
        with db_module.SessionLocal() as db:
            missed = event_bus.replay_events(db, since_seq=since_seq, limit=100)
        for ev in missed:
            await websocket.send_json(ev)

        queue = event_bus.subscribe()
        try:
            while True:
                try:
                    event_msg = await asyncio.wait_for(queue.get(), timeout=25.0)
                    await websocket.send_json(event_msg)
                except asyncio.TimeoutError:
                    await websocket.send_json({"event_type": "HEARTBEAT", "schema_version": "1.0"})
        except WebSocketDisconnect:
            pass
        finally:
            event_bus.unsubscribe(queue)

    from pathlib import Path
    base_proj_dir = Path(__file__).resolve().parent.parent.parent
    news_static_dir = base_proj_dir / "aipulse_deploy"
    if news_static_dir.exists():
        app.mount("/news", StaticFiles(directory=str(news_static_dir), html=True), name="news")

    tiktok_studio_dir = base_proj_dir / "workspaces" / "tiktok-fashion-ai-automation" / "public"
    if tiktok_studio_dir.exists():
        app.mount("/tiktok-studio", StaticFiles(directory=str(tiktok_studio_dir), html=True), name="tiktok-studio")

    frontend_dist_dir = base_proj_dir / "frontend" / "dist"
    if frontend_dist_dir.exists():
        assets_dir = frontend_dist_dir / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        @app.api_route("/{full_path:path}", methods=["GET", "HEAD"])
        async def serve_spa_frontend(full_path: str):
            if full_path.startswith(("api/", "ws/", "docs", "redoc", "openapi.json", "news", "tiktok-studio")):
                raise HTTPException(status_code=404, detail="Not found")
            return FileResponse(frontend_dist_dir / "index.html")

    return app


app = create_app()
