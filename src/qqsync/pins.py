"""Pins: every toolchain and dependency is pinned by digest, and every fetch is checked against it.

A fetched artifact whose bytes do not hash to its pin is never put in place: `fetch` downloads to a
temporary file next to the destination, hashes while it downloads, and only renames the file into
place when the digest matches. Anything else raises PinMismatch, and the build stops.
"""
from __future__ import annotations

import hashlib
import ipaddress
import http.client
import json
import os
import re
import platform as _platform
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from qqsync.manifest import PIN_SECTIONS
from qqsync.schema import OCI_PIN_RULE, OCI_SOURCE

PLACEHOLDER_HEX = frozenset({"0" * 40, "0" * 64})
CHUNK = 1 << 20
MAX_MANIFEST = 4 << 20


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
    try:
        actual = file_digest(path)
    except OSError as e:
        raise PinError(f"{pin.label}: cannot read {path}: {e.strerror or e}") from None
    if actual != pin.digest:
        raise PinMismatch(f"{pin.label}: {path} has digest {actual}, but the manifest pins {pin.digest}")


def verify_checkout(pin: Pin, path: str | Path) -> None:
    """Raise PinMismatch unless the checkout at `path` is exactly the pinned commit, unmodified.

    Untracked files (build output, caches) are allowed; changes to tracked files are not.
    """
    if pin.algorithm != "git":
        raise PinError(f"{pin.label}: a {pin.algorithm} pin names file bytes, not a commit; use verify_file")
    path = Path(path).resolve()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}  # GIT_DIR would override -C

    def git(*args: str) -> str:
        try:
            return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                                  check=True, env=env).stdout.strip()
        except (OSError, subprocess.CalledProcessError) as e:
            detail = e.stderr.strip() if isinstance(e, subprocess.CalledProcessError) else str(e)
            raise PinError(f"{pin.label}: cannot read the checkout at {path}: {detail}") from None

    top = git("rev-parse", "--show-toplevel")
    if Path(top).resolve() != path:
        raise PinError(f"{pin.label}: {path} is not the root of a checkout (it is inside {top})")
    head = git("rev-parse", "--verify", "HEAD^{commit}")
    expected = pin.digest.split(":", 1)[1]
    if head != expected:
        raise PinMismatch(f"{pin.label}: {path} is at commit {head}, but the manifest pins {expected}")
    changed = git("status", "--porcelain", "--untracked-files=no")
    if changed:
        raise PinMismatch(f"{pin.label}: {path} is at the pinned commit but tracked files were changed:\n"
                          + changed)


# --- fetching ------------------------------------------------------------------------------------

class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to https: the digest protects the bytes, this protects the request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme.lower() != "https":
            raise urllib.error.URLError(f"refusing a redirect to {newurl}; only https is followed")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_HttpsOnlyRedirects)

OCI_ACCEPT = ", ".join(["application/vnd.oci.image.manifest.v1+json",
                        "application/vnd.docker.distribution.manifest.v2+json"])
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def oci_parts(source: str) -> tuple[str, str, str | None]:
    """registry, repository and manifest digest (None when missing) of oci://REGISTRY/REPOSITORY@sha256:<manifest>."""
    m = OCI_SOURCE.match(source)
    if not m:
        raise PinError(f"{source!r} is not oci://REGISTRY/REPOSITORY@sha256:<64 hex>")
    return m["registry"], m["repository"], m["manifest"]


def _local_http_allowed(host: str | None) -> bool:
    """Plain http only for a registry on this machine (tests, a local mirror), and only when asked."""
    return host in _LOCAL_HOSTS and os.environ.get("QQ_OCI_SCHEME") == "http"


def _internal_address(host: str) -> bool:
    """True for localhost or an IP literal that is loopback, private, link-local or unspecified."""
    if host.rstrip(".") == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    address = getattr(address, "ipv4_mapped", None) or address
    return address.is_loopback or address.is_private or address.is_link_local or address.is_unspecified


def _oci_open(url: str, accept: str | None, token: list[str]):
    """Open a registry URL, answering one bearer-token challenge anonymously (public packages only).

    The token is never sent on to the storage host a blob redirects to.
    """
    for attempt in range(2):
        request = urllib.request.Request(url, headers={"Accept": accept} if accept else {})
        if token:
            request.add_unredirected_header("Authorization", f"Bearer {token[0]}")
        try:
            return _OPENER.open(request, timeout=120)
        except urllib.error.HTTPError as e:
            challenge = e.headers.get("WWW-Authenticate", "") if e.headers else ""
            if e.code != 401 or attempt or not challenge.lower().startswith("bearer "):
                hint = " (is the package public?)" if e.code in (401, 403) else ""
                raise PinError(f"cannot fetch {url}: HTTP {e.code}{hint}") from None
            token[:] = [_anonymous_token(challenge)]
    raise AssertionError("unreachable")


def _anonymous_token(challenge: str) -> str:
    fields = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    realm = fields.get("realm", "")
    if not realm:
        raise PinError(f"registry asked for a token without a realm: {challenge!r}")
    # The registry names the realm, so it must not be able to point qq at a local file or an
    # internal service and have the reply sent back to it as a token.
    parts = urllib.parse.urlsplit(realm)
    if "#" in realm or not parts.hostname or not (
            parts.scheme == "https" or (parts.scheme == "http" and _local_http_allowed(parts.hostname))):
        raise PinError(f"refusing token realm {realm!r}; it must be an https:// URL")
    if not _local_http_allowed(parts.hostname) and _internal_address(parts.hostname):
        # TODO(expert): a public name that resolves to an internal address still gets through.
        raise PinError(f"refusing token realm {realm!r}; it points at an internal address")
    query = urllib.parse.urlencode({k: fields[k] for k in ("service", "scope") if k in fields})
    sep = "&" if "?" in realm else "?"
    with _OPENER.open(f"{realm}{sep}{query}", timeout=60) as r:
        body = json.loads(r.read(MAX_MANIFEST + 1))
    token = (body.get("token") or body.get("access_token")) if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise PinError(f"registry token endpoint {realm} returned no token")
    return token


def _open_oci_layer(pin: Pin):
    """The pinned layer's blob, after checking the named manifest (if any) lists it.

    `oci://REGISTRY/REPOSITORY@sha256:<manifest>` with digest `sha256:<layer>`: the manifest's bytes
    must hash to its digest and list the layer, so a pin cannot pair one artifact's manifest with
    another's bytes. The layer's bytes are then checked against the pin like any download.
    """
    registry, repository, manifest_digest = oci_parts(pin.source)
    if not manifest_digest or manifest_digest == pin.digest:
        raise PinError(f"{pin.label}: {OCI_PIN_RULE}")
    scheme = "http" if _local_http_allowed(urllib.parse.urlsplit(f"//{registry}").hostname) else "https"
    base = f"{scheme}://{registry}/v2/{repository}"
    token: list[str] = []
    if manifest_digest:
        with _oci_open(f"{base}/manifests/{manifest_digest}", OCI_ACCEPT, token) as r:
            raw = r.read(MAX_MANIFEST + 1)
        if len(raw) > MAX_MANIFEST:
            raise PinError(f"{pin.label}: manifest {manifest_digest} is larger than {MAX_MANIFEST} bytes")
        if "sha256:" + hashlib.sha256(raw).hexdigest() != manifest_digest:
            raise PinMismatch(f"{pin.label}: the registry's manifest does not hash to {manifest_digest}")
        try:
            doc = json.loads(raw)
            if isinstance(doc, dict) and "manifests" in doc:
                raise PinError(f"{pin.label}: {manifest_digest} is an image index; pin the manifest of "
                               "one platform instead")
            layers = [layer.get("digest") for layer in doc.get("layers", [])]
        except (ValueError, AttributeError, TypeError):
            raise PinError(f"{pin.label}: manifest {manifest_digest} is not an image manifest") from None
        if pin.digest not in layers:
            raise PinMismatch(f"{pin.label}: manifest {manifest_digest} has no layer {pin.digest}")
    return _oci_open(f"{base}/blobs/{pin.digest}", None, token)


def fetch(pin: Pin, dest: str | Path) -> Path:
    """Download a sha256 pin's source to `dest`, verified. On a mismatch nothing is left at `dest`.

    Sources are https:// URLs, oci:// registry layers (see _open_oci_layer) or file:// URLs (for
    tests and local mirrors). Commit pins are checked out by the qq CLI and verified with
    verify_checkout.
    """
    if pin.algorithm != "sha256":
        raise PinError(f"{pin.label}: only sha256 pins are fetched here; check out commit pins and "
                       "use verify_checkout")
    scheme = urllib.parse.urlsplit(pin.source).scheme
    if scheme not in ("https", "file", "oci"):
        raise PinError(f"{pin.label}: cannot fetch {pin.source!r}; sources must be https://, oci:// "
                       "or file:// URLs")
    dest = Path(dest)
    if dest.is_dir():
        raise PinError(f"{pin.label}: destination {dest} is a directory; name the file to write")
    tmp = None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".part")
        sha = hashlib.sha256()
        with os.fdopen(fd, "wb") as out:
            opened = _open_oci_layer(pin) if scheme == "oci" else _OPENER.open(pin.source, timeout=60)
            with opened as resp:
                while chunk := resp.read(CHUNK):
                    sha.update(chunk)
                    out.write(chunk)
        actual = "sha256:" + sha.hexdigest()
        if actual != pin.digest:
            raise PinMismatch(f"{pin.label}: {pin.source} has digest {actual}, but the manifest pins "
                              f"{pin.digest}; refusing to use it")
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(tmp, 0o666 & ~umask)  # mkstemp makes 0600; give the file normal permissions
        os.replace(tmp, dest)
    except PinError:
        if tmp:
            Path(tmp).unlink(missing_ok=True)
        raise
    except (OSError, ValueError, http.client.HTTPException) as e:  # URLError is an OSError
        if tmp:
            Path(tmp).unlink(missing_ok=True)
        reason = getattr(e, "reason", None) or getattr(e, "strerror", None) or e
        raise PinError(f"{pin.label}: fetching {pin.source} to {dest} failed: {reason}") from None
    except BaseException:
        if tmp:
            Path(tmp).unlink(missing_ok=True)
        raise
    return dest


__all__ = ["Pin", "PinError", "PinMismatch", "current_platform", "fetch", "file_digest", "find_pin",
           "iter_pins", "oci_parts", "placeholders", "verify_checkout", "verify_file"]
