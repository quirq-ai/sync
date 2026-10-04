"""Errors qqsync raises. Each carries a message a person can act on."""
from __future__ import annotations


class ManifestError(Exception):
    """A manifest could not be read, or does not follow its schema."""

    def __init__(self, source: str, problems: list[str]):
        self.source = source
        self.problems = problems
        super().__init__("\n".join(f"{source}: {p}" for p in problems))
