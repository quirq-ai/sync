"""The code host behind `members`, `invite` and `revoke`.

A backend is a module `qqsync.project.backends.<name>` with:

    HOSTS                                   the hosts it speaks for
    members(slug) -> dict                   who has access, and who has been invited
    invite(slug, user, role=None) -> dict   give access
    revoke(slug, user) -> dict              remove access, or cancel an invitation

`slug` is `owner/name`. Backends are found, not registered: `launchpad` (quirq's own cloud) is one
new module. Nothing outside this package names a code host.
"""
from __future__ import annotations

import importlib
import os
import re

from qqsync.project.errors import ProjectError

PACKAGE = "qqsync.project.backends"
DEFAULT = "github"
ENV = "QQ_BACKEND"   # the variable qq itself reads
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def load(name: str | None = None):
    name = name or os.environ.get(ENV) or DEFAULT
    if not NAME_RE.fullmatch(name):
        raise ProjectError("bad-backend", f"{name!r} is not a backend name")
    module = f"{PACKAGE}.{name.replace('-', '_')}"
    try:
        return importlib.import_module(module)
    except ModuleNotFoundError as e:
        if e.name != module:
            raise
        raise ProjectError("unknown-backend", f"no backend named {name!r}") from None
