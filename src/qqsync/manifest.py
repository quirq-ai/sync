"""Reading and editing manifests. The one place a manifest's text becomes data.

Every manifest is read twice: by `tomllib` (the standard library's TOML parser, which defines what
the data is) and by `tomlkit` (which keeps comments, order and formatting so edits can be written
back). If the two disagree, or `tomlkit` cannot reproduce the file byte for byte, the manifest is
refused rather than risk an edit that changes more than it says.

Edits are transactional: each one is applied, the whole result is validated, and on any problem the
edit is undone and ManifestError is raised. A Manifest is therefore always valid.
"""
from __future__ import annotations

import os
import tempfile
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.items import AoT, Array, InlineTable, Table

from qqsync.errors import ManifestError
from qqsync.schema import validate

DEFAULT_PATH = Path("infra/repo.toml")
PIN_SECTIONS = ("toolchains", "deps")


class Manifest:
    """A validated manifest that can be edited without disturbing anything it does not change."""

    def __init__(self, text: str, source: str = "<string>", known_kinds: Iterable[str] | None = None):
        self.source = source
        self.known_kinds = None if known_kinds is None else frozenset(known_kinds)
        self._check(_parse(text, source))
        self._text = text

    @classmethod
    def read(cls, path: str | Path = DEFAULT_PATH, known_kinds: Iterable[str] | None = None) -> Manifest:
        path = Path(path)
        try:
            with open(path, encoding="utf-8", newline="") as f:  # newline="" keeps CRLF as written
                text = f.read()
        except (OSError, UnicodeDecodeError) as e:
            reason = e.strerror if isinstance(e, OSError) and e.strerror else str(e)
            raise ManifestError(str(path), [f"cannot read: {reason}"]) from None
        return cls(text, str(path), known_kinds)

    # --- reading ---------------------------------------------------------------------------------

    @property
    def data(self) -> dict:
        """The manifest as plain Python data (dicts, lists, strings), as tomllib reads it."""
        return tomllib.loads(self._text)  # a fresh copy, so callers cannot edit around the checks

    def dumps(self) -> str:
        """The manifest text. Unchanged since reading means byte-identical to what was read."""
        return self._text

    def write(self, path: str | Path | None = None) -> None:
        """Write the manifest atomically, to `path` or back where it was read from."""
        path = Path(path if path is not None else self.source)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(self._text)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # --- editing ---------------------------------------------------------------------------------

    def set_pin(self, section: str, name: str, *, digest: str, source: str | None = None,
                version: str | None = None, platform: str | None = None) -> None:
        """Pin toolchain or dependency `name` to `digest`, adding the pin if it is new.

        `source` and `version` are changed only when given. For a per-platform pin, name the
        `platform`; a pin is either single or per-platform, and this never converts between them.
        """
        if section not in PIN_SECTIONS:
            raise ValueError(f"section must be one of {', '.join(PIN_SECTIONS)}, not {section!r}")

        def edit(doc):
            pins = doc.get(section)
            if pins is None:
                pins = doc[section] = tomlkit.table(is_super_table=True)
            pin = pins.get(name)
            if pin is None:
                if source is None:
                    raise ManifestError(self.source, [f"{section}.{name}: new pin needs a source"])
                pin = pins[name] = tomlkit.table()
            if version is not None:
                pin["version"] = version
            if platform is None:
                if "platforms" in pin:
                    raise ManifestError(self.source, [f"{section}.{name}: pinned per platform; name the platform"])
                target = pin
            else:
                if "digest" in pin or "source" in pin:
                    raise ManifestError(self.source, [f"{section}.{name}: has one pin for all platforms; "
                                                      "drop the platform"])
                platforms = pin.get("platforms")
                if platforms is None:
                    platforms = pin["platforms"] = tomlkit.table()
                target = platforms.get(platform)
                if target is None:
                    if source is None:
                        raise ManifestError(self.source, [f"{section}.{name}.platforms.{platform}: "
                                                          "new pin needs a source"])
                    target = platforms[platform] = tomlkit.inline_table()
            if source is not None:
                target["source"] = source
            target["digest"] = digest

        self._edit(edit)

    def set_qq_version(self, version: str, *, source: str | None = None, digest: str | None = None) -> None:
        """Pin the qq CLI version this repo runs."""
        def edit(doc):
            qq = doc.get("qq")
            if qq is None:
                qq = doc["qq"] = tomlkit.table()
            qq["version"] = version
            if source is not None:
                qq["source"] = source
            if digest is not None:
                qq["digest"] = digest
        self._edit(edit)

    def set_target(self, name: str, key: str, value: Any) -> None:
        """Set one field of an existing target, for example its `srcs`."""
        def edit(doc):
            target = _find_target(doc, name, self.source)
            target[key] = value
        self._edit(edit)

    def add_target(self, target: dict) -> None:
        """Append a target. It is written in the style the manifest already uses."""
        def edit(doc):
            targets = doc["targets"]
            if isinstance(targets, AoT):
                table = tomlkit.table()
                table.update(target)
                targets.append(table)
            else:
                item = tomlkit.inline_table()
                item.update(target)
                targets.append(item)
        self._edit(edit)

    def remove_target(self, name: str) -> None:
        def edit(doc):
            targets = doc["targets"]
            for i, t in enumerate(targets):
                if t.get("name") == name:
                    del targets[i]
                    return
            raise ManifestError(self.source, [f"targets: no target named {name!r}"])
        self._edit(edit)

    # --- internals -------------------------------------------------------------------------------

    def _edit(self, change) -> None:
        doc = tomlkit.parse(self._text)  # edit a copy; self stays untouched until the result checks out
        change(doc)
        text = tomlkit.dumps(doc)
        if "\r\n" in self._text and "\n" not in self._text.replace("\r\n", ""):  # the editor writes new lines with \n; keep the file's own endings
            text = text.replace("\r\n", "\n").replace("\n", "\r\n")
        self._check(_parse(text, self.source))
        self._text = text

    def _check(self, data: dict) -> None:
        problems = validate(data, self.known_kinds)
        if problems:
            raise ManifestError(self.source, problems)


def _parse(text: str, source: str) -> dict:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ManifestError(source, [f"not valid TOML: {e}"]) from None
    try:
        doc = tomlkit.parse(text)
    except Exception as e:  # tomlkit raises several types; any of them means we cannot edit safely
        raise ManifestError(source, [f"cannot be edited safely (the editor cannot parse it: {e})"]) from None
    if doc.unwrap() != data:
        raise ManifestError(source, ["cannot be edited safely: the editor reads different data than "
                                     "the standard TOML parser; simplify the formatting"])
    if tomlkit.dumps(doc) != text:
        raise ManifestError(source, ["cannot be edited safely: the editor would not write it back byte "
                                     "for byte; simplify the formatting"])
    return data


def _find_target(doc, name: str, source: str) -> Table | InlineTable:
    targets = doc["targets"]
    assert isinstance(targets, (AoT, Array))
    for t in targets:
        if t.get("name") == name:
            return t
    raise ManifestError(source, [f"targets: no target named {name!r}"])


def loads(text: str, source: str = "<string>", known_kinds: Iterable[str] | None = None) -> dict:
    """Parse and validate manifest text. Raises ManifestError listing every problem."""
    return Manifest(text, source, known_kinds).data


def load(path: str | Path = DEFAULT_PATH, known_kinds: Iterable[str] | None = None) -> dict:
    """Read, parse and validate the manifest at `path`."""
    return Manifest.read(path, known_kinds).data
