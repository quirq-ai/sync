"""Pins: every toolchain and dependency is pinned by digest, and every fetch is checked against it.

A fetched artifact whose bytes do not hash to its pin is never put in place: `fetch` downloads to a
temporary file next to the destination, hashes while it downloads, and only renames the file into
place when the digest matches. Anything else raises PinMismatch, and the build stops.
"""
from __future__ import annotations

import hashlib
import os
import platform as _platform
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from qqsync.manifest import PIN_SECTIONS

PLACEHOLDER_HEX = frozenset({"0" * 40, "0" * 64})
CHUNK = 1 << 20


class PinError(Exception):
    """A pin cannot be used as asked (unknown name, wrong platform, unsupported source)."""


class PinMismatch(PinError):
    """Fetched or local content does not match its pin."""


@dataclass(frozen=True)
class Pin:
    section: str
    name: str
    platform: str | None
    source: str
    digest: str

    @property
    def label(self) -> str:
        where = f"{self.section}.{self.name}"
        return f"{where}.platforms.{self.platform}" if self.platform else where

    @property
    def algorithm(self) -> str:
        return self.digest.split(":", 1)[0]


def iter_pins(data: dict) -> Iterator[Pin]:
    """Every pin in validated manifest data, one per platform for per-platform pins."""
    for section in PIN_SECTIONS:
        for name, pin in data.get(section, {}).items():
            if "platforms" in pin:
                for plat, art in pin["platforms"].items():
                    yield Pin(section, name, plat, art["source"], art["digest"])
            else:
                yield Pin(section, name, None, pin["source"], pin["digest"])


def find_pin(data: dict, section: str, name: str, platform: str | None = None) -> Pin:
    """The pin to fetch for `name`. For a per-platform pin, `platform` defaults to this machine's."""
    pin = data.get(section, {}).get(name)
    if pin is None:
        raise PinError(f"{section}.{name}: no such pin in the manifest")
    if "platforms" not in pin:
        if platform is not None:
            raise PinError(f"{section}.{name}: has one pin for all platforms; drop the platform")
        return Pin(section, name, None, pin["source"], pin["digest"])
    platform = platform or current_platform()
    art = pin["platforms"].get(platform)
    if art is None:
        raise PinError(f"{section}.{name}: no pin for platform {platform}; "
                       f"pinned for {', '.join(sorted(pin['platforms']))}")
    return Pin(section, name, platform, art["source"], art["digest"])


def placeholders(data: dict) -> list[str]:
    """Pins whose digest is all zeros: a stand-in, not a pin. Strict checks reject them."""
    return [f"{p.label}: digest is a placeholder (all zeros); pin the real digest"
            for p in iter_pins(data) if p.digest.split(":", 1)[1] in PLACEHOLDER_HEX]


def current_platform() -> str:
    """This machine as <os>-<arch>, the form manifests use (linux-x86_64, macos-arm64, ...)."""
    os_name = {"darwin": "macos", "win32": "windows"}.get(sys.platform, sys.platform)
    arch = _platform.machine().lower()
    arch = {"amd64": "x86_64", "aarch64": "arm64"}.get(arch, arch)
    return f"{os_name}-{arch}"


# --- verifying -----------------------------------------------------------------------------------

def file_digest(path: str | Path) -> str:
    with open(path, "rb") as f:
        return "sha256:" + hashlib.file_digest(f, "sha256").hexdigest()


def verify_file(pin: Pin, path: str | Path) -> None:
    """Raise PinMismatch unless the file at `path` has exactly the pinned digest."""
    if pin.algorithm != "sha256":
        raise PinError(f"{pin.label}: a {pin.algorithm} pin names a commit, not a file; use verify_checkout")
    actual = file_digest(path)
    if actual != pin.digest:
        raise PinMismatch(f"{pin.label}: {path} has digest {actual}, but the manifest pins {pin.digest}")


def verify_checkout(pin: Pin, path: str | Path) -> None:
    """Raise PinMismatch unless the checkout at `path` is at exactly the pinned commit."""
    if pin.algorithm != "git":
        raise PinError(f"{pin.label}: a {pin.algorithm} pin names file bytes, not a commit; use verify_file")
    try:
        out = subprocess.run(["git", "-C", str(path), "rev-parse", "--verify", "HEAD^{commit}"],
                             capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as e:
        detail = e.stderr.strip() if isinstance(e, subprocess.CalledProcessError) else str(e)
        raise PinError(f"{pin.label}: cannot read the commit at {path}: {detail}") from None
    expected = pin.digest.split(":", 1)[1]
    if out != expected:
        raise PinMismatch(f"{pin.label}: {path} is at commit {out}, but the manifest pins {expected}")


# --- fetching ------------------------------------------------------------------------------------

def fetch(pin: Pin, dest: str | Path) -> Path:
    """Download a sha256 pin's source to `dest`, verified. On a mismatch nothing is left at `dest`.

    Sources are https:// or file:// URLs (file:// is for tests and local mirrors). Commit pins are
    checked out by the qq CLI and verified with verify_checkout.
    """
    if pin.algorithm != "sha256":
        raise PinError(f"{pin.label}: only sha256 pins are fetched here; check out commit pins and "
                       "use verify_checkout")
    scheme = urllib.parse.urlsplit(pin.source).scheme
    if scheme not in ("https", "file"):
        raise PinError(f"{pin.label}: cannot fetch {pin.source!r}; sources must be https:// or file:// URLs")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".part")
    try:
        sha = hashlib.sha256()
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(pin.source, timeout=60) as resp:
            while chunk := resp.read(CHUNK):
                sha.update(chunk)
                out.write(chunk)
        actual = "sha256:" + sha.hexdigest()
        if actual != pin.digest:
            raise PinMismatch(f"{pin.label}: {pin.source} has digest {actual}, but the manifest pins "
                              f"{pin.digest}; refusing to use it")
        os.replace(tmp, dest)
    except OSError as e:  # URLError is an OSError
        Path(tmp).unlink(missing_ok=True)
        raise PinError(f"{pin.label}: fetching {pin.source} failed: {getattr(e, 'reason', e)}") from None
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return dest


__all__ = ["Pin", "PinError", "PinMismatch", "current_platform", "fetch", "file_digest", "find_pin",
           "iter_pins", "placeholders", "verify_checkout", "verify_file"]
