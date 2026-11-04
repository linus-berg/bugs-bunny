"""Error type shared by the API layer and the worker processes."""

from __future__ import annotations

from collections.abc import Mapping

from starlette.exceptions import HTTPException


class ServiceError(HTTPException):
    """An error with an HTTP status and a stable, machine-readable ``code``.

    Raised both in the web process and inside worker processes, so it must
    survive pickling. ``HTTPException`` does not pickle cleanly on its own
    (keyword arguments are lost), hence the explicit ``__reduce__``.

    Subclassing Starlette's ``HTTPException`` matters: FastAPI re-raises
    those unchanged when they occur while parsing the request body, which
    lets the upload size limit surface as a 413 instead of a generic 400.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.code = code

    def __reduce__(self):
        headers = dict(self.headers) if self.headers else None
        return (type(self), (self.status_code, self.code, self.detail, headers))
