"""git plumbing. The only module that runs git.

Every call has a timeout, so a hung network or a stuck lock cannot stall a caller that polls. The
network calls (ls-remote, fetch, clone) run with GIT_TERMINAL_PROMPT=0: with no credentials git
fails at once instead of waiting on a prompt nobody will answer. The rest of the caller's
environment is passed through, so a credential helper, GIT_ASKPASS or GIT_CONFIG_* settings the
caller supplies still apply.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from qqsync.project.errors import ProjectError

# Seconds. A fetch or clone may download a large pack; the others read refs or local objects.
LOCAL_TIMEOUT = 60.0
LS_REMOTE_TIMEOUT = 60.0
FETCH_TIMEOUT = 300.0
CLONE_TIMEOUT = 600.0

PROTOCOLS = "https:ssh:file"   # the transports qq itself fetches over
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


@dataclass(frozen=True)
class Result:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    def reason(self) -> str:
        """git's own explanation: the first `fatal:` or `error:` line it wrote to stderr, else the
        last line (its advice comes after the error, and is not the reason)."""
        lines = [line.strip() for line in self.err.splitlines() if line.strip()]
        for line in lines:
            if line.startswith(("fatal:", "error:")):
                return line[:300]
        return lines[-1][:300] if lines else f"git exited with {self.code}"


def _env(network: bool) -> dict[str, str]:
    env = dict(os.environ)
    env["LC_ALL"] = "C"   # git's messages are read for the reason, so in one language
    if network:
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ALLOW_PROTOCOL"] = PROTOCOLS
    return env


def run(args: list[str], cwd: Path | None = None, *, timeout: float = LOCAL_TIMEOUT,
        network: bool = False) -> Result:
    if shutil.which("git") is None:
        raise ProjectError("git-missing", "this needs git on PATH")
    cmd = ["git", *(["-C", str(cwd)] if cwd else []), *args]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=_env(network), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return Result(-1, "", f"git {args[0]} timed out after {int(timeout)}s")
    except OSError as e:
        raise ProjectError("git-missing", f"cannot run git: {e}") from None
    return Result(done.returncode, done.stdout, done.stderr)


def valid_branch(name: str) -> bool:
    """A branch name that can never be read as an option or a revision range."""
    return bool(BRANCH_RE.fullmatch(name)) and ".." not in name and not name.endswith("/")


def toplevel(path: Path) -> Path | None:
    """The root of the work tree containing `path`, or None outside one."""
    done = run(["rev-parse", "--show-toplevel"], path)
    out = done.out.strip()
    return Path(out) if done.ok and out else None


def origin_url(clone: Path) -> str | None:
    done = run(["config", "--get", "remote.origin.url"], clone)
    out = done.out.strip()
    return out if done.ok and out else None


def config_get(clone: Path, key: str) -> str | None:
    """A value from the clone's own .git/config, which never travels with the repo."""
    done = run(["config", "--local", "--get", key], clone)
    out = done.out.strip()
    return out if done.ok and out else None


def current_branch(clone: Path) -> str | None:
    """The checked-out branch, or None when HEAD is detached."""
    done = run(["symbolic-ref", "--quiet", "--short", "HEAD"], clone)
    out = done.out.strip()
    return out if done.ok and out else None


def rev(clone: Path, ref: str) -> str | None:
    done = run(["rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}"], clone)
    out = done.out.strip()
    return out if done.ok and out else None


def has_commit(clone: Path, sha: str) -> bool:
    return bool(sha) and run(["cat-file", "-e", f"{sha}^{{commit}}"], clone).ok


def count(clone: Path, since: str, until: str) -> int | None:
    """How many commits `until` has that `since` lacks."""
    done = run(["rev-list", "--count", f"{since}..{until}"], clone)
    try:
        return int(done.out.strip()) if done.ok else None
    except ValueError:
        return None


def ls_remote(clone: Path, branch: str) -> tuple[str | None, str]:
    """(tip of origin's branch, "") from the remote itself: ref names only, no objects.
    (None, "") when origin has no such branch; (None, reason) when origin cannot be read."""
    done = run(["ls-remote", "origin", f"refs/heads/{branch}"], clone, timeout=LS_REMOTE_TIMEOUT, network=True)
    if not done.ok:
        return None, done.reason()
    line = done.out.strip().splitlines()[0] if done.out.strip() else ""
    return (line.split("\t")[0].strip() or None), ""


def fetch(clone: Path) -> Result:
    return run(["fetch", "--quiet", "origin"], clone, timeout=FETCH_TIMEOUT, network=True)


def merging(clone: Path) -> bool:
    """True while a merge is unfinished in the clone."""
    return run(["rev-parse", "--verify", "--quiet", "MERGE_HEAD"], clone).ok


def merge_ff(clone: Path, ref: str) -> Result:
    return run(["merge", "--ff-only", "--quiet", "--end-of-options", ref], clone)


def clone(url: str, dest: Path) -> Result:
    return run(["clone", "--quiet", "--", url, str(dest)], timeout=CLONE_TIMEOUT, network=True)
