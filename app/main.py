"""Application entrypoint.

Run locally with::

    uvicorn app.main:app --reload

Interactive API docs: http://localhost:8000/docs
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.routers import billing, health, overview, resources

STATIC_DIR = Path(__file__).parent / "static"
log = get_logger("app")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        description=(
            "Read-only control-plane API for an AWS account: S3, EC2, CloudWatch "
            "and Cost Explorer behind a validated, cached, authenticated REST "
            "interface.\n\n"
            "Authenticate with the **Authorize** button (X-API-Key) or send "
            "`X-API-Key: <key>` on each request."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        contact={"name": "Cloud Control API"},
        license_info={"name": "MIT"},
    )

    # -- middleware (order matters: last added runs first) ----------------
    app.add_middleware(GZipMiddleware, minimum_size=800)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["X-API-Key", "Authorization", "Content-Type"],
        max_age=600,
    )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-ms"] = str(elapsed_ms)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        log.info(
            "request",
            extra={
                "extra_fields": {
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": elapsed_ms,
                    "request_id": request_id,
                }
            },
        )
        return response

    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(overview.router)
    app.include_router(resources.router)
    app.include_router(billing.router)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def dashboard() -> HTMLResponse:
        """Serve the dashboard, optionally bootstrapping the local API key."""
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        bootstrap: dict[str, Any] = {"apiKey": None, "provider": None}
        if settings.api_key_injected:
            bootstrap = {"apiKey": settings.api_key, "provider": settings.provider_mode}
        html = html.replace("__DASHBOARD_BOOTSTRAP__", json.dumps(bootstrap))
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/health", include_in_schema=False)
    def health_redirect() -> RedirectResponse:
        return RedirectResponse(url="/livez", status_code=307)

    @app.exception_handler(404)
    async def _not_found(request: Request, exc: Exception) -> Response:
        if request.url.path.startswith(("/api/", "/livez", "/readyz")):
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "not_found",
                        "message": f"No route for {request.url.path}",
                        "request_id": getattr(request.state, "request_id", "unknown"),
                    }
                },
            )
        return HTMLResponse(
            "<h1>404</h1><p>Not found. Try <a href='/'>the dashboard</a> or "
            "<a href='/docs'>/docs</a>.</p>",
            status_code=404,
        )

    logging.getLogger(__name__).info(
        "application configured",
        extra={
            "extra_fields": {
                "environment": settings.environment,
                "provider_mode": settings.provider_mode,
                "auth_enabled": settings.auth_enabled,
            }
        },
    )
    return app


app = create_app()
