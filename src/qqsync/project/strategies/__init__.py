"""What "bring this clone up to date" means, as a replaceable part.

A strategy is a module `qqsync.project.strategies.<name>` with one function:

    apply(clone: Path, branch: str) -> int

It is called after `git fetch origin`, so `origin/<branch>` is fresh. It moves the working clone
and returns how many commits it applied (0 when there was nothing to do), or raises
`errors.refused(...)` when it will not move it. Strategies are found, not registered: adding one
adds a module and edits nothing else.

Which strategy a clone uses is recorded on this machine only, in the clone's own .git/config:

    git config qqsync.strategy NAME

That file never travels with the repo, so a project shared with you cannot choose the code that
runs on your machine.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

from qqsync.project import git
from qqsync.project.errors import ProjectError

PACKAGE = "qqsync.project.strategies"
DEFAULT = "ff-only"
CONFIG_KEY = "qqsync.strategy"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def configured(clone: Path) -> str:
    return git.config_get(clone, CONFIG_KEY) or DEFAULT


def load(name: str):
    if not NAME_RE.fullmatch(name):
        raise ProjectError("bad-strategy", f"{name!r} is not a strategy name (lower case letters, digits and -)")
    module = f"{PACKAGE}.{name.replace('-', '_')}"
    try:
        return importlib.import_module(module)
    except ModuleNotFoundError as e:
        if e.name != module:
            raise   # the strategy exists and one of its own imports is broken: never "unknown"
        raise ProjectError("unknown-strategy", f"no strategy named {name!r} (looked for {module})") from None
