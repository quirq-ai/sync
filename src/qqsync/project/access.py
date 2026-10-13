"""Who has access to a clone's repo: finds the code host's backend and asks it.

The repo is always the one the clone's origin names, never a name typed by the caller, so a
command run in a clone can only read or change access to that clone's own repo.
"""
from __future__ import annotations

from pathlib import Path

from qqsync.project import backends, git, identity
from qqsync.project.errors import ProjectError, refused

MEMBERS_SCHEMA = "qq-sync-project-members/1"
ACCESS_SCHEMA = "qq-sync-project-access/1"


def target(clone: Path, backend: str | None):
    """(backend module, identity, `owner/name`) for the clone's origin."""
    origin = git.origin_url(clone)
    repo = identity.normalize(origin) if origin else None
    if repo is None:
        raise ProjectError("no-remote", f"{clone.name} has no origin on a code host")
    host, slug = identity.split(repo)
    module = backends.load(backend)
    if host not in module.HOSTS:
        raise refused("unsupported-host", f"{host} is not a host the {module.__name__.rsplit('.', 1)[-1]} backend speaks for")
    return module, repo, slug


def members(clone: Path, backend: str | None) -> dict:
    module, repo, slug = target(clone, backend)
    return {"schema": MEMBERS_SCHEMA, "repo": repo, **module.members(slug)}


def invite(clone: Path, backend: str | None, user: str, role: str | None) -> dict:
    module, repo, slug = target(clone, backend)
    return {"schema": ACCESS_SCHEMA, "repo": repo, **module.invite(slug, user, role)}


def revoke(clone: Path, backend: str | None, user: str) -> dict:
    module, repo, slug = target(clone, backend)
    return {"schema": ACCESS_SCHEMA, "repo": repo, **module.revoke(slug, user)}
