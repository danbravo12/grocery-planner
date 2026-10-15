"""Errors leave the API as {code, message} (plus a few documented extras), never a stack trace."""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        headers: dict[str, str] | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.headers, self.extra = headers or {}, extra

    def body(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.extra}


def validation_fields(errors: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Pydantic errors as [{field, message}], e.g. meals.0.people: Input should be ..."""
    out = []
    for e in errors:
        loc = [str(p) for p in e.get("loc", ()) if p != "body"]
        message = str(e.get("msg", "invalid")).removeprefix("Value error, ")
        out.append({"field": ".".join(loc) or "body", "message": message})
    return out


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(exc.body(), status_code=exc.status, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = validation_fields(list(exc.errors()))
        body = {
            "code": "validation_error",
            "message": "The request is not valid.",
            "fields": fields,
        }
        return JSONResponse(body, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        message = "Not found." if exc.status_code == 404 else str(exc.detail)
        return JSONResponse({"code": code, "message": message}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error in the API", exc_info=exc)
        body = {"code": "internal", "message": "Something went wrong on our side."}
        return JSONResponse(body, status_code=500)
