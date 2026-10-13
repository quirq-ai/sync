"""ff-only: fast-forward HEAD to origin/<branch>, or refuse and say why.

The default. It can never create a merge commit, rewrite history or touch a diverged branch. What
is in the way is named before git is asked to move anything, or read from git's own refusal:

    diverged              both sides have commits the other lacks (your own, or a rewritten remote)
    unfinished            a merge is in progress in the clone
    local-changes         uncommitted edits to files the update changes
    untracked-in-the-way  files of yours, not in git, that the update adds
    locked                another git command is running in the clone, or one left its lock behind
    not-fast-forward      anything else git refused, with its reason
"""
from __future__ import annotations

from pathlib import Path

from qqsync.project import git
from qqsync.project.errors import ProjectError, refused

FILES_SHOWN = 5


def _refusal(clone: Path, target: str, done: git.Result) -> ProjectError:
    """Why git would not fast-forward, from what it wrote. git lists the files in the way one per
    line, indented with a tab."""
    files = [line.strip() for line in done.err.splitlines() if line.startswith("\t")]
    named = ", ".join(files[:FILES_SHOWN]) + (f" and {len(files) - FILES_SHOWN} more" if len(files) > FILES_SHOWN else "")
    if "index.lock" in done.err:
        return refused("locked", f"{clone.name}: another git command is running in this clone, or one stopped "
                                 "and left .git/index.lock behind; nothing was changed")
    if files and "untracked working tree files" in done.err:
        return refused("untracked-in-the-way", f"{clone.name}: the update adds {named}, and you have your own "
                       "copy that is not in git; move or delete it, then run this again", files=files)
    if files and "local changes" in done.err:
        return refused("local-changes", f"{clone.name}: you have uncommitted changes to {named}, which the update "
                       "also changes; commit or stash them, then run this again", files=files)
    return refused("not-fast-forward", f"{clone.name}: cannot fast-forward to {target}: {done.reason()}")


def apply(clone: Path, branch: str) -> int:
    target = f"origin/{branch}"
    behind = git.count(clone, "HEAD", target)
    if behind is None:
        raise refused("not-fetched", f"{target} is not known in {clone.name}; nothing has been fetched")
    if behind == 0:
        return 0
    ahead = git.count(clone, target, "HEAD") or 0
    if ahead:
        raise refused("diverged", f"{clone.name}: your {branch} and {target} have diverged ({ahead} only here, "
                      f"{behind} only there), so it cannot be fast-forwarded; rebase or merge by hand",
                      behind=behind, ahead=ahead)
    if git.merging(clone):
        raise refused("unfinished", f"{clone.name}: a merge is in progress; finish it or run git merge --abort, "
                                    "then run this again")
    done = git.merge_ff(clone, target)
    if not done.ok:
        raise _refusal(clone, target, done)
    return behind
