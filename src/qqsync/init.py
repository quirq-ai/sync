"""`qqsync init`: write a new repo's infra/repo.toml, so no consumer needs a manifest generator.

The manifest gets one target per kind, the toolchain pins those kinds need, and the qq version.
Everything language-specific comes from the caller's inputs, not from this code:
  - which toolchain a kind needs: infra-config's config/kinds.toml (`[[kind]] name, toolchain`);
  - the pins: quirq-ai/toolchains' promoted.toml (`[[toolchain]] name, version, platform, ref,
    layer_sha256`), written as `source = ref` (the OCI image manifest) and `digest = sha256:<layer>`.
The text is built in a fixed order, so the same inputs always give the same bytes, and checked by
the schema before it is written. An existing manifest is never replaced.
"""
from __future__ import annotations

import errno
import os
import re
import tempfile
import tomllib
from pathlib import Path

from qqsync.errors import ManifestError
from qqsync.manifest import DEFAULT_PATH, loads
from qqsync.schema import CURRENT

PROMOTED_SCHEMA = "quirq-toolchains-promoted/1"
HEADER = """\
# quirq infra (qq) manifest, schema {schema} (quirq-ai/sync). Read and edit it only through qqsync
# (`qqsync show`, `qqsync pin`, or the qqsync.manifest library); never parse it any other way.
# Written by `qqsync init`. The toolchain pins are the ones quirq-ai/toolchains promoted: `source`
# names the OCI image manifest and `digest` the layer in it. The toolchain roller moves them.
"""


class InitError(Exception):
    """`qqsync init` could not write the manifest; nothing was written."""


def _toml_file(path: str | Path, what: str) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except OSError as e:
        raise InitError(f"{path}: cannot read the {what}: {e.strerror}") from None
    except tomllib.TOMLDecodeError as e:
        raise InitError(f"{path}: the {what} is not valid TOML: {e}") from None
    except UnicodeDecodeError:
        raise InitError(f"{path}: the {what} is not valid TOML (not UTF-8)") from None


def _string(value: str) -> str:
    """`value` as a TOML basic string: escape the quote, the backslash and control characters."""
    out = []
    for ch in value:
        if ch in '"\\':
            out.append("\\" + ch)
        elif ch < " " or ch == "\x7f":
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _key(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else _string(value)  # bare when TOML allows


def _kind_toolchains(kinds_file: str | Path, kinds: list[str]) -> list[str]:
    """The toolchain each kind needs (None for none), from infra-config's kinds.toml."""
    data = _toml_file(kinds_file, "kinds list")
    listed = {}
    for entry in data.get("kind", []) if isinstance(data.get("kind"), list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            if entry["name"] in listed:
                raise InitError(f"{kinds_file}: kind {entry['name']!r} is listed twice")
            listed[entry["name"]] = entry.get("toolchain")
    if not listed:
        raise InitError(f"{kinds_file}: lists no [[kind]] entries; pass infra-config's config/kinds.toml")
    unknown = [k for k in kinds if k not in listed]
    if unknown:
        raise InitError(f"{', '.join(map(repr, unknown))}: not a kind {kinds_file} lists "
                        f"({', '.join(sorted(listed))})")
    for k in kinds:
        if listed[k] is not None and (not isinstance(listed[k], str) or not listed[k]):
            raise InitError(f"{kinds_file}: kind {k!r} has a toolchain that is not a name")
    return [listed[k] for k in kinds]


def _promoted_pins(promoted_file: str | Path, names: list[str]) -> dict[str, dict]:
    """{toolchain: {"version": ..., "platforms": {platform: (source, digest)}}} for `names`."""
    data = _toml_file(promoted_file, "promoted toolchains")
    if data.get("schema") != PROMOTED_SCHEMA:
        raise InitError(f"{promoted_file}: schema is {data.get('schema')!r}, not {PROMOTED_SCHEMA!r}; "
                        "pass quirq-ai/toolchains' promoted.toml")
    pins: dict[str, dict] = {}
    for entry in data.get("toolchain", []) if isinstance(data.get("toolchain"), list) else []:
        if not isinstance(entry, dict) or entry.get("name") not in names:
            continue
        name = entry["name"]
        fields = {k: entry.get(k) for k in ("version", "platform", "ref", "layer_sha256")}
        bad = [k for k, v in fields.items() if not isinstance(v, str) or not v]
        if bad:
            raise InitError(f"{promoted_file}: toolchain {name!r}: {', '.join(bad)} must be a non-empty string")
        if not re.fullmatch(r"[0-9a-f]{64}", fields["layer_sha256"]):
            raise InitError(f"{promoted_file}: toolchain {name!r}: layer_sha256 must be 64 lowercase hex "
                            f"characters, not {fields['layer_sha256']!r}")
        pin = pins.setdefault(name, {"version": fields["version"], "platforms": {}})
        if pin["version"] != fields["version"]:
            raise InitError(f"{promoted_file}: toolchain {name!r} is promoted at two versions "
                            f"({pin['version']} and {fields['version']}); one manifest pins one")
        if fields["platform"] in pin["platforms"]:
            raise InitError(f"{promoted_file}: toolchain {name!r} has two pins for {fields['platform']}")
        pin["platforms"][fields["platform"]] = (fields["ref"], "sha256:" + fields["layer_sha256"])
    missing = sorted(set(names) - set(pins))
    if missing:
        raise InitError(f"{promoted_file}: no promoted pin for toolchain {', '.join(map(repr, missing))}")
    return pins


def render(kinds: list[str], kinds_file: str | Path, promoted_file: str | Path, qq_version: str) -> str:
    """The manifest text for `kinds`, checked against the schema and the kinds list."""
    if not kinds:
        raise InitError("name at least one --kind")
    repeated = sorted({k for k in kinds if kinds.count(k) > 1})
    if repeated:
        raise InitError(f"kind {', '.join(map(repr, repeated))} given more than once")
    toolchains = _kind_toolchains(kinds_file, kinds)
    names = sorted({t for t in toolchains if t})
    pins = _promoted_pins(promoted_file, names)

    lines = [HEADER.format(schema=CURRENT) + f"schema = {_string(CURRENT)}", "",
             "[qq]", f"version = {_string(qq_version)}"]
    for name in names:
        lines += ["", f"[toolchains.{_key(name)}]", f"version = {_string(pins[name]['version'])}"]
        for platform, (source, digest) in sorted(pins[name]["platforms"].items()):
            lines.append(f"platforms.{_key(platform)} = "
                         f"{{ source = {_string(source)}, digest = {_string(digest)} }}")
    for kind in kinds:
        lines += ["", "[[targets]]", f"name = {_string(kind)}", f"kind = {_string(kind)}"]
    text = "\n".join(lines) + "\n"
    try:
        loads(text, source="the new manifest", known_kinds=kinds)
    except ManifestError as e:
        raise InitError("the inputs do not make a valid manifest:\n  " + "\n  ".join(e.problems)) from None
    return text


def _write_new(dest: Path, text: str) -> None:
    """Write `dest` whole, never replacing a file there. A temp file is hard-linked into place, so
    readers never see half a manifest; where the file system has no hard links, an exclusive create."""
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".repo.toml.", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(tmp, 0o666 & ~umask)
        try:
            os.link(tmp, dest)  # fails if the manifest appeared meanwhile: never replace one
            return
        except FileExistsError:
            raise InitError(f"{dest}: a manifest already exists; edit it with `qqsync pin` instead") from None
        except OSError as e:
            if e.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV):
                raise
    finally:
        os.unlink(tmp)
    try:
        out = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o666)
    except FileExistsError:
        raise InitError(f"{dest}: a manifest already exists; edit it with `qqsync pin` instead") from None
    try:
        with os.fdopen(out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        os.unlink(dest)
        raise


def init(root: str | Path, kinds: list[str], kinds_file: str | Path, promoted_file: str | Path,
         qq_version: str) -> Path:
    """Write `root`/infra/repo.toml. Refuses an existing manifest; writes nothing on any error."""
    root = Path(root)
    if not root.is_dir():
        raise InitError(f"{root}: not a directory")
    text = render(kinds, kinds_file, promoted_file, qq_version)
    dest = root / DEFAULT_PATH
    infra = dest.parent
    if infra.is_symlink() or (infra.exists() and not infra.is_dir()):
        raise InitError(f"{infra}: must be a directory in the repo, not a link or a file")
    if os.path.lexists(dest):
        what = "a directory, not a manifest, is there" if dest.is_dir() else "a manifest already exists"
        raise InitError(f"{dest}: {what}; edit a manifest with `qqsync pin` instead")
    made_infra = False
    try:
        if not infra.exists():
            infra.mkdir()
            made_infra = True
        _write_new(dest, text)
    except (OSError, InitError) as e:
        if made_infra:
            try:
                infra.rmdir()  # only if still empty: something else may have written there meanwhile
            except OSError:
                pass
        if isinstance(e, InitError):
            raise
        raise InitError(f"{dest}: cannot write: {e.strerror or e}") from None
    return dest


__all__ = ["InitError", "init", "render"]
