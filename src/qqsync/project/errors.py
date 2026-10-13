"""One error type, so every command fails the same way.

Callers decide on the exit code and the `code` word, never on the message text:

    1  refused: the command was understood but not done (diverged, no access, rate limited, ...)
    2  could not run: bad usage, git or gh missing, not signed in, not a clone
"""
from __future__ import annotations

REFUSED = 1
CANNOT_RUN = 2


class ProjectError(Exception):
    def __init__(self, code: str, message: str, *, exit_code: int = CANNOT_RUN, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.detail = detail or {}

    def to_json(self) -> dict:
        return {"code": self.code, "message": self.message, **self.detail}


def refused(code: str, message: str, **detail) -> ProjectError:
    return ProjectError(code, message, exit_code=REFUSED, detail=detail)
