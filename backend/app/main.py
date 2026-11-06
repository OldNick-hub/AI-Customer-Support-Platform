"""Root FastAPI Application Bootstrap.

Configures:
1. Minimal, decoupled lifespan management (ActiveIndexReader memory cache sync only).
2. Distributed tracing correlation ID middleware (X-Request-ID).
3. Standardized CORS middleware.
4. Comprehensive RFC 7807 Problem Details exception handlers.
5. Interservice API v1 router mounting (/api/v1).
6. Basic liveness and readiness health probes.
"""

from contextlib import asynccontextmanager
import logging
from typing import AsyncGenerator
import uuid

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1 import api_v1_router
from app.core.config import settings
from app.core.exceptions import PlatformException
from app.core.logging import configure_safe_logger
from app.db.session import AsyncSessionLocal
from app.modules.knowledge.reader import ActiveIndexReader
from app.modules.ticketing.worker import EscalationWorker

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
    reader = ActiveIndexReader(settings=settings)
    try:
        async with AsyncSessionLocal() as session:
            await reader.sync_on_startup(session=session)
    except Exception as exc:
        logger.debug("Reader startup sync skipped or encountered non-fatal error: %s", exc)

    # Initialize EscalationWorker for production runtime
    escalation_worker = EscalationWorker(
        session_factory=AsyncSessionLocal,
        poll_interval_seconds=float(getattr(settings, "ESCALATION_POLL_INTERVAL_SECONDS", 30.0)),
    )
    try:
        async with AsyncSessionLocal() as session:
            await escalation_worker.scan_and_escalate_batch(session)
    except Exception as exc:
        logger.debug("EscalationWorker startup recovery scan skipped or non-fatal error: %s", exc)

    escalation_worker.start_periodic_scanner()
    app.state.escalation_worker = escalation_worker

    yield

    logger.info("Shutting down FastAPI application lifespan...")
    try:
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
    return {"status": "ready"}
