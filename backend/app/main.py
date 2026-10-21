from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from app.core.asgi import EphemresApp, FastAPI, Request, status
from app.core.asgi import CORSMiddleware
from app.core.asgi import JSONResponse
from app.core.orm import text
from app.core.asgi import BaseHTTPMiddleware

from app.config import settings
from app.database import AsyncSessionLocal, check_database_connection
from app.logging_config import setup_logging
from app.routers import (
    alerts,
    auth,
    bandwidth,
    endpoints,
    events,
    reports,
    settings as settings_router,
    topology,
    users,
)
from app.routers.reports import telemetry_router
from app.schemas import APIResponse
from app.services.alert_dispatcher import alert_dispatcher
from app.services.baseline_route import start_midnight_discovery_worker
from app.services.baseline_service import baseline_cache, start_baseline_refresh_task
from app.services.diagnostics import (
    start_diagnostic_cleanup_task,
    start_discovery_worker,
)
from app.services.driver_manager import driver_manager
from app.services.runtime_settings import start_runtime_settings_refresh_task
from app.core.watchdog import start_systemd_watchdog
from app.services.telemetry_relay import telemetry_relay
from app.services.topology import topology_manager

# Initialize dual console and rotating file logging
logger = setup_logging(
    service_name="ephemres-api",
    log_dir=getattr(settings.logging, "log_dir", "/var/log/ephemres"),
    log_level=getattr(settings.logging, "level", "INFO"),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await check_database_connection()
    # Initialize storage driver manager (Redis / PostgreSQL)
    await driver_manager.initialize()

    # Initialize baseline cache and topology DAG manager
    async with AsyncSessionLocal() as db:
        await baseline_cache.refresh_from_db(db)
        await topology_manager.full_rebuild(db)

    # Admin-editable runtime settings; SYSTEM_SETTINGS_SYNC pushes changes, the loop is a safety net.
    runtime_settings_task = await start_runtime_settings_refresh_task(
        AsyncSessionLocal, interval_seconds=60
    )

    # Start inter-process telemetry relay (syncs broker events to topology RAM and browser SSE)
    await telemetry_relay.start()
    # Start Enterprise Alert Dispatcher background worker
    await alert_dispatcher.start()

    from app.services.settings_sync import start_settings_sync_listener

    settings_sync_task = asyncio.create_task(
        start_settings_sync_listener(AsyncSessionLocal),
        name="settings_sync_listener",
    )

    refresh_task = await start_baseline_refresh_task(
        AsyncSessionLocal, interval_seconds=3600
    )
    discovery_task = await start_discovery_worker(AsyncSessionLocal)
    midnight_task = await start_midnight_discovery_worker(AsyncSessionLocal)
    cleanup_task = await start_diagnostic_cleanup_task(
        AsyncSessionLocal, interval_seconds=86400
    )
    watchdog_task = await start_systemd_watchdog(interval_seconds=10)
    logger.info(
        "Ephemres v3.3.2.v started successfully with Enterprise Alerting, Dual-Storage, Flow Telemetry, Interface Metrics, Cluster Settings Sync & Zero-Trust SSRF Protection."
    )
    yield
    watchdog_task.cancel()
    settings_sync_task.cancel()
    runtime_settings_task.cancel()
    await alert_dispatcher.stop()
    await telemetry_relay.stop()
    refresh_task.cancel()
    discovery_task.cancel()
    midnight_task.cancel()
    cleanup_task.cancel()
    logger.info("Ephemres v3.3.2.v platform shutting down cleanly.")


app = EphemresApp(
    title="Ephemres - Network Monitoring Platform",
    description="Ephemres High-Performance Network Monitoring and Observability Platform API",
    version="3.3.2.v",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

class AccessLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start_time = time.perf_counter()
        forwarded = request.headers.get("X-Forwarded-For")
        client_ip = (
            forwarded.split(",")[0].strip()
            if forwarded
            else (request.client.host if request.client else "127.0.0.1")
        )
        path = request.url.path
        method = request.method
        try:
            response = await call_next(request)
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            status = response.status_code
            if status >= 500:
                logger.error(
                    '%s - "%s %s" %d SERVER ERROR - %.2fms',
                    client_ip,
                    method,
                    path,
                    status,
                    latency_ms,
                )
            elif status in (401, 403):
                logger.warning(
                    '%s - "%s %s" %d AUTH REJECTED - %.2fms',
                    client_ip,
                    method,
                    path,
                    status,
                    latency_ms,
                )
            elif status >= 400:
                logger.info(
                    '%s - "%s %s" %d CLIENT ERROR - %.2fms',
                    client_ip,
                    method,
                    path,
                    status,
                    latency_ms,
                )
            else:
                logger.info(
                    '%s - "%s %s" %d OK - %.2fms',
                    client_ip,
                    method,
                    path,
                    status,
                    latency_ms,
                )
            return response
        except Exception as exc:
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            logger.error(
                '%s - "%s %s" 500 EXCEPTION: %s (%.2fms)',
                client_ip,
                method,
                path,
                exc,
                latency_ms,
                exc_info=True,
            )
            raise


app.add_middleware(AccessLoggingMiddleware)


class HSTSMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if settings.security.hsts_enabled:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response


app.add_middleware(HSTSMiddleware)

if "*" in settings.api.allowed_origins:
    raise ValueError(
        "CORS configuration error: allowed_origins cannot contain '*' when allow_credentials=True."
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.api.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(alerts.router, prefix="/api/v1")
app.include_router(auth.router, prefix="/api/v1")
app.include_router(bandwidth.router, prefix="/api/v1")
app.include_router(endpoints.router, prefix="/api/v1")
app.include_router(events.router, prefix="/api/v1")
app.include_router(reports.router, prefix="/api/v1")
app.include_router(settings_router.router, prefix="/api/v1")
app.include_router(topology.router, prefix="/api/v1")
app.include_router(users.router, prefix="/api/v1")
app.include_router(telemetry_router)


@app.get("/api/v1/version", tags=["system"])
async def get_version():
    return APIResponse.success(
        data={"version": "3.3.2.v", "platform": "Ephemres v3.3.2.v"}
    )


@app.get("/api/v1/health", tags=["system"])
async def health_check():
    return APIResponse.success(data={"status": "ok", "version": "3.3.2.v"})


@app.get("/health/liveness", tags=["system"])
@app.get("/api/v1/health/liveness", tags=["system"])
async def health_liveness():
    return {"status": "ok", "process": "healthy", "version": "3.3.2.v"}


@app.get("/health/readiness", tags=["system"])
@app.get("/api/v1/health/readiness", tags=["system"])
async def health_readiness():
    db_ok = True
    db_err = None
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:
        db_ok = False
        db_err = "Database connection failed"
        logger.error("Readiness check: database connectivity failure: %s", exc, exc_info=True)

    redis_ok = True
    redis_err = None
    if settings.redis.enabled:
        try:
            client = driver_manager.get_redis_client()
            if client is not None:
                await client.ping()
            else:
                redis_ok = False
                redis_err = "Redis service unavailable"
        except Exception as exc:
            redis_ok = False
            redis_err = "Redis connection failed"
            logger.error("Readiness check: redis connectivity failure: %s", exc, exc_info=True)

    is_ready = db_ok and (not settings.redis.enabled or redis_ok)
    status_code = (
        status.HTTP_200_OK if is_ready else status.HTTP_503_SERVICE_UNAVAILABLE
    )

    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ready" if is_ready else "degraded",
            "ready": is_ready,
            "database": {"status": "ok" if db_ok else "error", "error": db_err},
            "redis": {
                "status": "ok" if (not settings.redis.enabled or redis_ok) else "error",
                "enabled": settings.redis.enabled,
                "error": redis_err,
            },
            "version": "3.3.2.v",
        },
    )
