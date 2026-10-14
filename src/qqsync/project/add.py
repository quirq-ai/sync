"""add: clone a repo into a folder of clones.

Unlike `qq fetch`, the repo needs no infra/repo.toml: any git repo can be added. git works in a
hidden `.<name>.cloning` folder and the finished clone is renamed into place, so nothing that
lists the folder ever sees a half clone as a project.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import sys
from pathlib import Path

from qqsync.project import git, identity
from qqsync.project.errors import ProjectError, refused

SCHEMA = "qq-sync-project-add/1"
CLONING_SUFFIX = ".cloning"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def default_name(url: str) -> str:
    last = re.split(r"[/\\:]", url.rstrip("/\\"))[-1]
    return last[:-4] if last.endswith(".git") else last


def _rmtree(path: Path) -> None:
    """Remove a tree git wrote. Some platforms keep object files read only."""
    def writable(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)
    key = "onexc" if sys.version_info >= (3, 12) else "onerror"
    shutil.rmtree(path, **{key: writable})


def add(url: str, root: Path, name: str | None = None) -> dict:
    if not url or url.startswith("-"):
        raise ProjectError("bad-url", f"{url!r} is not a repository URL")
    root = root.resolve()
    if not root.is_dir():
        raise ProjectError("no-such-directory", f"{root} is not a directory")
    name = name or default_name(url)
    if not NAME_RE.fullmatch(name):
        raise ProjectError("bad-name", f"{name!r} is not a folder name this command will create; pass a NAME")
    target = root / name
    if target.exists() or target.is_symlink():
        raise refused("exists", f"{target} already exists")
    tmp = root / f".{name}{CLONING_SUFFIX}"
    if tmp.exists():
        _rmtree(tmp)   # left by a clone that was interrupted
    done = git.clone(url, tmp)
    if not done.ok:
        if tmp.exists():
            _rmtree(tmp)
        raise refused("clone-failed", f"cannot clone {url}: {done.reason()}")
    try:
        tmp.rename(target)
    except OSError as e:
        _rmtree(tmp)
        raise refused("exists", f"could not move the clone to {target}: {e}") from None
    return {"schema": SCHEMA, "name": name, "path": str(target),
            "repo": identity.normalize(git.origin_url(target)), "head": git.rev(target, "HEAD")}
