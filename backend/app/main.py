from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

import asyncpg

from app.core.asgi import EphemresApp, FastAPI, Request, status
from app.core.asgi import CORSMiddleware
from app.core.asgi import JSONResponse
from app.core.orm import DatabaseUnavailableError, PoolTimeoutError
from app.core.orm import text
from app.core.asgi import BaseHTTPMiddleware

from app.config import settings
from app.database import AsyncSessionLocal, check_connection_budget, check_database_connection, ping_database
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
from app.core.watchdog import start_loop_stall_guard, start_systemd_watchdog
from app.services.telemetry_relay import telemetry_relay
from app.services.scheduler_leader import scheduler_leader
from app.services.topology import start_topology_flow_refresh_task, topology_manager

# Initialize dual console and rotating file logging
logger = setup_logging(
    service_name="ephemres-api",
    log_dir=getattr(settings.logging, "log_dir", "/var/log/ephemres"),
    log_level=getattr(settings.logging, "level", "INFO"),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await check_database_connection()
    await check_connection_budget()
    # Initialize storage driver manager (Redis / PostgreSQL)
    await driver_manager.initialize()

    # Initialize baseline cache and topology DAG manager
    async with AsyncSessionLocal() as db:
        await baseline_cache.refresh_from_db(db)
        await topology_manager.full_rebuild(db)
    # Topology links checked against the next hops exporters report in flows
    topology_flow_task = await start_topology_flow_refresh_task(AsyncSessionLocal)

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
    # Elect the worker that runs cluster-wide schedules (midnight discovery, diagnostic cleanup).
    await scheduler_leader.start()
    discovery_task = await start_discovery_worker(AsyncSessionLocal)
    midnight_task = await start_midnight_discovery_worker(AsyncSessionLocal)
    cleanup_task = await start_diagnostic_cleanup_task(
        AsyncSessionLocal, interval_seconds=86400
    )
    watchdog_task = await start_systemd_watchdog(interval_seconds=10)
    stall_guard_task = start_loop_stall_guard()
    logger.info(
        "Ephemres v3.3.5 started successfully with Enterprise Alerting, Dual-Storage, Flow Telemetry, Interface Metrics, Cluster Settings Sync & Zero-Trust SSRF Protection."
    )
    yield
    if stall_guard_task is not None:
        stall_guard_task.cancel()
    watchdog_task.cancel()
    topology_flow_task.cancel()
    settings_sync_task.cancel()
    runtime_settings_task.cancel()
    await alert_dispatcher.stop()
    await telemetry_relay.stop()
    refresh_task.cancel()
    discovery_task.cancel()
    midnight_task.cancel()
    cleanup_task.cancel()
    await scheduler_leader.stop()
    logger.info("Ephemres v3.3.5 platform shutting down cleanly.")


# The OpenAPI document lists every route and the exact version, and is served without signing
# in, so it is only served in development and test (the same rule as the secret-key check in
# config.py: any other environment counts as production). Nothing in Ephemres reads it, and a
# public copy would hand an attacker the route map and version in one request (M3 coverage
# review, 2026-11-10). docs_url and redoc_url are None because the framework serves neither page.
_SERVE_OPENAPI = settings.environment.lower() in ("development", "test")

app = EphemresApp(
    title="Ephemres - Network Monitoring Platform",
    description="Ephemres High-Performance Network Monitoring and Observability Platform API",
    version="3.3.5",
    docs_url=None,
    redoc_url=None,
    openapi_url="/api/openapi.json" if _SERVE_OPENAPI else None,
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


async def database_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    """503, not a bare 500 or a 401: the request may be fine and the session certainly is, so the
    browser must not sign the user out of it."""
    logger.warning("Database unavailable on %s %s: %s", request.method, request.url.path, type(exc).__name__)
    return JSONResponse(
        status_code=503,
        content={"detail": "Ephemres can't reach its database right now. Try again shortly."},
        headers={"Retry-After": "15"},
    )


for _exc_class in (
    DatabaseUnavailableError,
    ConnectionError,
    asyncpg.PostgresConnectionError,
    asyncpg.InterfaceError,
    asyncpg.exceptions.OperatorInterventionError,
):
    app.add_exception_handler(_exc_class, database_unavailable_handler)


@app.exception_handler(asyncio.TimeoutError)
async def query_timeout_handler(request: Request, exc: Exception) -> JSONResponse:
    """A query hit the database command timeout: say so instead of a bare 500."""
    logging.getLogger(__name__).warning("Query timed out on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=504,
        content={"detail": "This request took too long to answer. Try a shorter time window."},
    )


@app.get("/api/v1/version", tags=["system"])
async def get_version():
    return APIResponse.success(
        data={"version": "3.3.5", "platform": "Ephemres v3.3.5"}
    )


@app.get("/api/v1/health", tags=["system"])
async def health_check():
    return APIResponse.success(data={"status": "ok", "version": "3.3.5"})


@app.get("/health/liveness", tags=["system"])
@app.get("/api/v1/health/liveness", tags=["system"])
async def health_liveness():
    return {"status": "ok", "process": "healthy", "version": "3.3.5"}


@app.get("/health/readiness", tags=["system"])
@app.get("/api/v1/health/readiness", tags=["system"])
async def health_readiness():
    db_ok = True
    db_status = "ok"
    db_err = None
    try:
        # At most 2 s for a free pooled connection. It used to wait with no limit and answer
        # 200 "ready" after 10 s while requests were failing (scale and core review S6).
        await ping_database(timeout=2.0)
    except PoolTimeoutError:
        db_ok = False
        db_status = "busy"
        db_err = "No database connection free: the pool is in use"
        logger.warning("Readiness check: every pooled database connection is busy")
    except Exception as exc:
        db_ok = False
        db_status = "error"
        db_err = "Database connection failed"
        # One line per failed probe, no traceback: probes repeat every few seconds while down.
        logger.warning("Readiness check: database connectivity failure: %s", exc)

    # FastStore only matters in performance mode: on a standard-mode server no client exists, and
    # failing on that made every healthy server answer 503.
    faststore_in_use = driver_manager.driver_mode == "redis"
    redis_ok = True
    redis_err = None
    if faststore_in_use:
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
            logger.warning("Readiness check: redis connectivity failure: %s", exc)

    is_ready = db_ok and redis_ok
    status_code = (
        status.HTTP_200_OK if is_ready else status.HTTP_503_SERVICE_UNAVAILABLE
    )

    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ready" if is_ready else "degraded",
            "ready": is_ready,
            "database": {"status": db_status, "error": db_err},
            "redis": {
                "status": "ok" if redis_ok else "error",
                "enabled": faststore_in_use,
                "error": redis_err,
            },
            "version": "3.3.5",
        },
    )
