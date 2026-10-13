"""Which clones a command acts on.

`--root DIR` names one clone (DIR is its top), a folder whose direct children are clones, or a
directory somewhere inside one clone, tried in that order. Nothing is searched deeper: a clone
inside a clone is that clone's business.
"""
from __future__ import annotations

from pathlib import Path

from qqsync.project import git
from qqsync.project.errors import ProjectError


def _is_clone(d: Path) -> bool:
    return (d / ".git").exists()


def find(root: Path) -> list[Path]:
    root = root.resolve()
    if not root.is_dir():
        raise ProjectError("no-such-directory", f"{root} is not a directory")
    if _is_clone(root):
        return [root]
    children = sorted(d for d in root.iterdir() if d.is_dir() and not d.name.startswith(".") and _is_clone(d))
    if children:
        return children
    top = git.toplevel(root)
    return [top] if top is not None else []


def one(root: Path) -> Path:
    """The single clone `root` names, for commands that act on exactly one repo."""
    found = find(root)
    if len(found) != 1:
        raise ProjectError("not-one-repo", f"{root.resolve()} holds {len(found)} clones; "
                                        "run this inside one clone or pass --root CLONE")
    return found[0]
