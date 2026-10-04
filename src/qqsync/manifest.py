"""Reading manifests. The one place a manifest's text becomes data."""
from __future__ import annotations

import tomllib
from collections.abc import Iterable
from pathlib import Path

from qqsync.errors import ManifestError
from qqsync.schema import validate

DEFAULT_PATH = Path("infra/repo.toml")


def loads(text: str, source: str = "<string>", known_kinds: Iterable[str] | None = None) -> dict:
    """Parse and validate manifest text. Raises ManifestError listing every problem."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ManifestError(source, [f"not valid TOML: {e}"]) from None
    problems = validate(data, known_kinds)
    if problems:
        raise ManifestError(source, problems)
    return data


def load(path: str | Path = DEFAULT_PATH, known_kinds: Iterable[str] | None = None) -> dict:
    """Read, parse and validate the manifest at `path`."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ManifestError(str(path), [f"cannot read: {e.strerror or e}"]) from None
    return loads(text, str(path), known_kinds)
