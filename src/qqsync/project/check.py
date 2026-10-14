"""check: is each clone behind its remote? Reads only; changes nothing, not even remote refs.

One `git ls-remote` per clone asks the remote where the branch is now. Everything else is local.

States, per clone:

    current        HEAD already has the remote's tip
    behind         the tip is fetched and HEAD can fast-forward to it
    ahead          HEAD has commits the remote lacks, and nothing to receive
    diverged       both sides have commits the other lacks
    fetch-needed   the remote moved and its commits are not here yet (`project --fetch-only` gets them)
    unreachable    the remote could not be read (network, credentials, a removed repo)
    no-remote      the clone has no origin, so there is nothing to compare with
    no-branch      origin has no branch of that name
    detached       HEAD is on no branch and --branch was not given
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from qqsync.project import git, identity

SCHEMA = "qq-sync-project-check/1"
JOBS = 8   # clones asked at once; each is one short-lived ls-remote

UPDATES = frozenset({"behind", "diverged", "fetch-needed"})
FAILED = frozenset({"unreachable"})


def check_clone(clone: Path, branch: str | None) -> dict:
    entry = {"name": clone.name, "path": str(clone), "repo": None, "branch": branch,
             "head": git.rev(clone, "HEAD"), "remote": None, "state": "", "behind": None, "ahead": None}
    origin = git.origin_url(clone)
    if origin is None:
        return {**entry, "state": "no-remote"}
    entry["repo"] = identity.normalize(origin)
    branch = branch or git.current_branch(clone)
    if branch is None:
        return {**entry, "state": "detached"}
    entry["branch"] = branch
    remote, problem = git.ls_remote(clone, branch)
    if problem:
        return {**entry, "state": "unreachable", "error": problem}
    if remote is None:
        return {**entry, "state": "no-branch"}
    entry["remote"] = remote
    if not git.has_commit(clone, remote):
        return {**entry, "state": "fetch-needed"}
    behind, ahead = git.count(clone, "HEAD", remote), git.count(clone, remote, "HEAD")
    if behind and ahead:
        state = "diverged"
    elif behind:
        state = "behind"
    elif ahead:
        state = "ahead"
    else:
        state = "current"
    return {**entry, "state": state, "behind": behind, "ahead": ahead}


def check(found: list[Path], branch: str | None) -> dict:
    with ThreadPoolExecutor(max_workers=JOBS) as pool:
        repos = list(pool.map(lambda clone: check_clone(clone, branch), found))
    return {"schema": SCHEMA, "repos": repos,
            "updates": sum(r["state"] in UPDATES for r in repos),
            "failed": sum(r["state"] in FAILED for r in repos)}
