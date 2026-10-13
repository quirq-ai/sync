"""update: fetch a clone's remote, then bring the clone up to date with its strategy.

`fetch_only` stops after the fetch: new commits are downloaded and the working files are left
exactly as they were. That is the step a background poller runs; moving the working files is
left to a person (or to whoever calls it without --fetch-only).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from qqsync.project import git, identity, strategies
from qqsync.project.errors import REFUSED, ProjectError, refused

SCHEMA = "qq-sync-project-update/1"
JOBS = 8   # clones updated at once


def update_clone(clone: Path, branch: str | None, *, fetch_only: bool, strategy: str | None) -> dict:
    origin = git.origin_url(clone)
    if origin is None:
        raise refused("no-remote", f"{clone.name} has no origin to update from")
    current = git.current_branch(clone)
    branch = branch or current
    if branch is None:
        raise refused("detached", f"{clone.name}: HEAD is on no branch; check out a branch first"
                                  " (or name one with --branch to only fetch it)")
    entry = {"name": clone.name, "path": str(clone), "repo": identity.normalize(origin), "branch": branch}
    done = git.fetch(clone)
    if not done.ok:
        raise refused("fetch-failed", f"{clone.name}: git fetch failed: {done.reason()}")
    target = f"origin/{branch}"
    if git.rev(clone, f"refs/remotes/{target}") is None:
        raise refused("no-branch", f"{clone.name}: origin has no branch {branch}")
    if fetch_only:
        return {**entry, "state": "fetched", "behind": git.count(clone, "HEAD", target),
                "head": git.rev(clone, "HEAD")}
    if current != branch:
        # A strategy moves what is checked out; moving another branch to follow this one is never meant.
        raise refused("other-branch", f"{clone.name}: {current or 'no branch'} is checked out, not {branch}; "
                      f"new commits of {branch} were fetched and the working files left alone")
    name = strategy or strategies.configured(clone)
    applied = strategies.load(name).apply(clone, branch)
    return {**entry, "state": "synced", "strategy": name, "applied": applied, "head": git.rev(clone, "HEAD")}


def update(found: list[Path], branch: str | None, *, fetch_only: bool, strategy: str | None) -> dict:
    """Each clone on its own: one that is refused never stops the others. Clones are separate
    repositories, so several are updated at once; most of the time is the wait on each fetch."""
    def one(clone: Path) -> dict:
        try:
            return update_clone(clone, branch, fetch_only=fetch_only, strategy=strategy)
        except ProjectError as e:
            if e.exit_code != REFUSED:
                raise   # git missing, a bad strategy name: the same for every clone
            return {"name": clone.name, "path": str(clone), "state": "refused", "error": e.to_json()}

    with ThreadPoolExecutor(max_workers=JOBS) as pool:
        repos = list(pool.map(one, found))
    return {"schema": SCHEMA, "repos": repos, "refused": sum(r["state"] == "refused" for r in repos)}
