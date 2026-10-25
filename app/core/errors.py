"""Typed application errors and their HTTP representation.

Every failure the client sees has the same shape::

    {"error": {"code": "provider_unavailable", "message": "...", "request_id": "..."}}

which lets a frontend branch on ``code`` instead of scraping prose.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class AppError(Exception):
    """Base class for errors that are safe to surface to a client."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "internal_error"

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class AuthenticationError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"


class ProviderUnavailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "provider_unavailable"


class UpstreamAWSError(AppError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "upstream_aws_error"


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


def _payload(code: str, message: str, request: Request, detail: str | None = None) -> dict:
    body: dict = {"error": {"code": code, "message": message, "request_id": _request_id(request)}}
    if detail:
        body["error"]["detail"] = detail
    return body


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_payload(exc.code, exc.message, request, exc.detail),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Request validation failed",
                    "request_id": _request_id(request),
                    "fields": [
                        {"loc": ".".join(str(p) for p in err["loc"]), "msg": err["msg"]}
                        for err in exc.errors()
                    ],
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_payload(
                f"http_{exc.status_code}",
                str(exc.detail) if exc.detail else "Request failed",
                request,
            ),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Deliberately opaque: internals never leak to the client.
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_payload("internal_error", "An unexpected error occurred", request),
        )
