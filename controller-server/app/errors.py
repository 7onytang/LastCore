"""Domain-level errors; the API layer translates them to HTTP responses."""

from fastapi import HTTPException


class ControllerError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def to_http_exception(error: ControllerError) -> HTTPException:
    return HTTPException(error.status_code, error.detail)
