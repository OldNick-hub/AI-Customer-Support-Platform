"""Root FastAPI Application Bootstrap.

Configures:
1. Minimal, decoupled lifespan management (ActiveIndexReader memory cache sync only).
2. Distributed tracing correlation ID middleware (X-Request-ID).
3. Standardized CORS middleware.
4. Comprehensive RFC 7807 Problem Details exception handlers.
5. Interservice API v1 router mounting (/api/v1).
6. Basic liveness and readiness health probes.
"""

import asyncio
from contextlib import asynccontextmanager
import logging
from typing import AsyncGenerator
import uuid

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1 import api_v1_router
from app.core.config import settings
from app.core.exceptions import PlatformException
from app.core.logging import configure_safe_logger
from app.db.session import AsyncSessionLocal
from app.modules.knowledge.reader import ActiveIndexReader
from app.modules.ticketing.worker import EscalationWorker
from app.modules.evidence.worker import ReconciliationWorker
from app.modules.evidence.lifecycle_manager import (
    EvidenceRetentionWorker,
    StorageLifecycleManager,
)
from app.services.redis_service import get_redis_service

logger = configure_safe_logger(logging.getLogger(__name__))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """FastAPI application lifespan context manager.

    Coordinates production lifecycle:
    1. Loads active vector index metadata into memory on startup (ActiveIndexReader).
    2. Runs EscalationWorker startup restart recovery for overdue 15m negative cases.
    3. Starts EscalationWorker periodic background scanner.
    4. Gracefully terminates and awaits background tasks on application shutdown.
    """
    logger.info("Initializing FastAPI application lifespan...")

    # Fast-fail configuration safeguards
    if hasattr(settings, "validate_embedding_configuration"):
        settings.validate_embedding_configuration()
    if hasattr(settings, "validate_production_configuration"):
        settings.validate_production_configuration()

    reader = ActiveIndexReader(settings=settings)
    try:
        async with AsyncSessionLocal() as session:
            await reader.sync_on_startup(session=session)
    except Exception as exc:
        logger.debug("Reader startup sync skipped or encountered non-fatal error: %s", exc)

    # Initialize EscalationWorker
    escalation_worker = EscalationWorker(
        session_factory=AsyncSessionLocal,
        poll_interval_seconds=float(getattr(settings, "ESCALATION_POLL_INTERVAL_SECONDS", 30.0)),
    )
    app.state.escalation_worker = escalation_worker

    # Initialize ReconciliationWorker
    reconciliation_worker = ReconciliationWorker(
        session_factory=AsyncSessionLocal,
        poll_interval_seconds=float(getattr(settings, "RECONCILIATION_POLL_INTERVAL_SECONDS", 10.0)),
        stale_timeout_seconds=float(getattr(settings, "RECONCILIATION_STALE_TIMEOUT_SECONDS", 300.0)),
    )
    app.state.reconciliation_worker = reconciliation_worker

    # Initialize EvidenceRetentionWorker
    storage_lifecycle_manager = StorageLifecycleManager(
        max_batch_size=int(getattr(settings, "EVIDENCE_RETENTION_BATCH_SIZE", 50)),
        max_retries=int(getattr(settings, "EVIDENCE_RETENTION_MAX_RETRIES", 3)),
    )
    retention_worker = EvidenceRetentionWorker(
        session_factory=AsyncSessionLocal,
        lifecycle_manager=storage_lifecycle_manager,
        scan_interval_seconds=float(getattr(settings, "EVIDENCE_RETENTION_SCAN_INTERVAL_SECONDS", 3600.0)),
        batch_size=int(getattr(settings, "EVIDENCE_RETENTION_BATCH_SIZE", 50)),
    )
    app.state.retention_worker = retention_worker

    enable_workers = getattr(settings, "ENABLE_BACKGROUND_WORKERS", True)
    if enable_workers:
        # Run startup recovery scans and sweeps
        try:
            async with AsyncSessionLocal() as session:
                await escalation_worker.scan_and_escalate_batch(session)
        except Exception as exc:
            logger.debug("EscalationWorker startup recovery scan skipped or non-fatal error: %s", exc)

        try:
            async with AsyncSessionLocal() as session:
                await reconciliation_worker.recover_stale_jobs(session)
                await reconciliation_worker.scan_and_process_batch(session)
        except Exception as exc:
            logger.debug("ReconciliationWorker startup recovery scan skipped or non-fatal error: %s", exc)

        try:
            async with AsyncSessionLocal() as session:
                await storage_lifecycle_manager.execute_purge_batch(session)
        except Exception as exc:
            logger.debug("EvidenceRetentionWorker startup sweep skipped or non-fatal error: %s", exc)

        escalation_worker.start_periodic_scanner()
        reconciliation_worker.start_periodic_scanner()
        retention_worker.start_periodic_scanner()
        logger.info("Background workers successfully started in FastAPI lifespan.")
    else:
        logger.info("API mode active: Background worker loops and recovery disabled (ENABLE_BACKGROUND_WORKERS=False).")

    yield

    logger.info("Shutting down FastAPI application lifespan...")
    if enable_workers:
        try:
            ret_worker = getattr(app.state, "retention_worker", None)
            if ret_worker:
                await ret_worker.stop_async()
        except Exception as exc:
            logger.warning("Error stopping EvidenceRetentionWorker on shutdown: %s", exc)
        try:
            if reconciliation_worker:
                await reconciliation_worker.stop_async()
        except Exception as exc:
            logger.warning("Error stopping ReconciliationWorker on shutdown: %s", exc)
        try:
            if escalation_worker:
                await escalation_worker.stop_async()
        except Exception as exc:
            logger.warning("Error stopping EscalationWorker on shutdown: %s", exc)
    reader.stop()


app = FastAPI(
    title="Intelligent Customer Support AI Platform",
    description="Unified enterprise customer support modular monolith API",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# 1. CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS if hasattr(settings, "CORS_ORIGINS") else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 2. Correlation ID Header Middleware
@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    corr_id = (
        request.headers.get("X-Request-ID")
        or request.headers.get("X-Correlation-ID")
        or str(uuid.uuid4())
    ).strip()[:128]

    request.state.correlation_id = corr_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = corr_id
    return response


# 3. Standardized RFC 7807 Exception Handlers
@app.exception_handler(PlatformException)
async def platform_exception_handler(request: Request, exc: PlatformException):
    corr_id = getattr(request.state, "correlation_id", "unknown")
    logger.warning("Handled PlatformException [%s]: %s (status %s)", exc.error_code, exc.message, exc.status_code)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "error": {
                "code": exc.error_code,
                "message": exc.message,
                "details": exc.detail,
                "request_id": corr_id,
            },
        },
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    corr_id = getattr(request.state, "correlation_id", "unknown")
    err_code = "HTTP_ERROR"
    if exc.status_code == 401:
        err_code = "UNAUTHORIZED"
    elif exc.status_code == 403:
        err_code = "FORBIDDEN"
    elif exc.status_code == 404:
        err_code = "NOT_FOUND"
    elif exc.status_code == 500:
        err_code = "INTERNAL_SERVER_ERROR"

    headers = getattr(exc, "headers", None)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "error": {
                "code": err_code,
                "message": exc.detail,
                "request_id": corr_id,
            },
        },
        headers=headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    corr_id = getattr(request.state, "correlation_id", "unknown")
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={
            "success": False,
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "Request payload validation failed.",
                "details": exc.errors(),
                "request_id": corr_id,
            },
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    corr_id = getattr(request.state, "correlation_id", "unknown")
    logger.error("Unhandled server exception on correlation %s: %s", corr_id, exc, exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "success": False,
            "error": {
                "code": "INTERNAL_SERVER_ERROR",
                "message": "An unexpected error occurred while processing your request. Please try again later.",
                "request_id": corr_id,
            },
        },
    )


# 4. Mount API v1 Routers
app.include_router(api_v1_router, prefix="/api/v1")


# 5. Basic Liveness and Readiness Probes (API_SPECIFICATION.md Section 2.2)
@app.get("/api/v1/health/live", tags=["Health"], summary="Liveness Probe")
async def health_live():
    return {"status": "alive"}


@app.get("/api/v1/health/ready", tags=["Health"], summary="Readiness Probe")
async def health_ready():
    """Dependency-aware readiness health probe.

    Verifies live connectivity to PostgreSQL (SELECT 1) and Redis (PING if required or configured)
    with a strict ~1.0-second timeout per dependency check.
    Returns HTTP 200 when ready, HTTP 503 if any required dependency fails or times out.
    """
    db_healthy = False
    db_status = "unhealthy"
    for attempt in range(2):
        try:
            async with AsyncSessionLocal() as session:
                await asyncio.wait_for(session.execute(text("SELECT 1")), timeout=1.0)
            db_healthy = True
            db_status = "healthy"
            break
        except asyncio.TimeoutError:
            logger.warning("Readiness probe: database check timed out (>1.0s).")
            db_status = "timeout"
            break
        except Exception as exc:
            if "another operation is in progress" in str(exc) and attempt == 0:
                await asyncio.sleep(0.05)
                continue
            logger.warning("Readiness probe: database connection failed: %s", exc)
            db_status = "unhealthy"
            break

    redis_required = getattr(settings, "REDIS_REQUIRED", False) or settings.ENVIRONMENT in ("staging", "production")
    redis_healthy = False
    redis_status = "unhealthy"
    try:
        redis_svc = get_redis_service()
        await asyncio.wait_for(redis_svc.ping(), timeout=1.0)
        redis_healthy = True
        redis_status = "healthy"
    except asyncio.TimeoutError:
        logger.warning("Readiness probe: Redis ping timed out (>1.0s).")
        redis_status = "timeout"
        redis_healthy = not redis_required
    except Exception as exc:
        logger.debug("Readiness probe: Redis check encountered: %s", exc)
        redis_status = "unhealthy" if redis_required else "unavailable (optional)"
        redis_healthy = not redis_required

    all_healthy = db_healthy and redis_healthy
    if not all_healthy:
        payload = {
            "status": "unhealthy",
            "error": "One or more required dependencies are unavailable.",
            "checks": {
                "database": db_status,
                "redis": redis_status,
            },
        }
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=payload,
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"status": "ready"},
    )
