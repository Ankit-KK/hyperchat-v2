"""Generic API error with the same {"error": ...} JSON shape as the auth/security errors."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(self, status: int, error: str, reason: str) -> None:
        super().__init__(reason)
        self.status, self.error, self.reason = status, error, reason


def install_api_error_handler(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _handle(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse({"error": exc.error}, status_code=exc.status)
