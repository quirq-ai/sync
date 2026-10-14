"""What "the same repo" means: a remote URL normalized to `host/owner/name`.

The https and ssh spellings of one repo give one identity, so two clones made in different ways
are recognised as the same project. A local path or a file:// URL has no identity (None).
"""
from __future__ import annotations

import re

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
_HOST_PATH_RE = re.compile(r"^(?:[^@/\s]+@)?([^:/\s]+)[:/](.+)$")


def normalize(url) -> str | None:
    """`host/owner/name` in lower case, or None. Drops the scheme, embedded credentials, the ssh
    `git@host:` prefix, a trailing `.git` and trailing slashes. A bare host or free text is None,
    never a made-up identity."""
    if not isinstance(url, str):
        return None
    u = _SCHEME_RE.sub("", url.strip())
    m = _HOST_PATH_RE.match(u)
    if not m:
        return None
    host, path = m.group(1), m.group(2).strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    path = path.strip("/")
    if not path or "/" not in path:
        return None
    return f"{host}/{path}".lower()


def split(identity: str) -> tuple[str, str]:
    """(`host`, `owner/name`) of an identity."""
    host, _, slug = identity.partition("/")
    return host, slug
