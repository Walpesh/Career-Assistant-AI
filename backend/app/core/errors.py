"""Единый формат ошибок API (docs/03_API_CONTRACTS.md §1, коды §9).

Любая ошибка возвращается как JSON: { "detail": "...", "error_code": "OPTIONAL_CODE" }.
Используется HTTPException-подклассом `AppError`, чтобы коды ошибок были явными,
а handler'ы в app.main преобразовывали исключения в контрактный формат.
"""

from __future__ import annotations

from fastapi import HTTPException, status

# Коды ошибок по умолчанию для статусов из docs/03 §9.
DEFAULT_ERROR_CODES: dict[int, str] = {
    status.HTTP_400_BAD_REQUEST: "BAD_REQUEST",
    status.HTTP_401_UNAUTHORIZED: "UNAUTHORIZED",
    status.HTTP_403_FORBIDDEN: "FORBIDDEN",
    status.HTTP_404_NOT_FOUND: "NOT_FOUND",
    status.HTTP_409_CONFLICT: "CONFLICT",
    status.HTTP_413_CONTENT_TOO_LARGE: "PAYLOAD_TOO_LARGE",
    status.HTTP_429_TOO_MANY_REQUESTS: "TOO_MANY_REQUESTS",
    status.HTTP_500_INTERNAL_SERVER_ERROR: "INTERNAL_ERROR",
    status.HTTP_503_SERVICE_UNAVAILABLE: "SERVICE_UNAVAILABLE",
}


class AppError(HTTPException):
    """HTTP-ошибка с явным error_code в ответе."""

    def __init__(
        self,
        status_code: int,
        detail: str,
        error_code: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.error_code = error_code or DEFAULT_ERROR_CODES.get(status_code, "ERROR")


def unauthorized(detail: str = "Не авторизован", error_code: str = "UNAUTHORIZED") -> AppError:
    return AppError(status.HTTP_401_UNAUTHORIZED, detail, error_code)
