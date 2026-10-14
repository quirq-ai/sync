"""GitHub backend: access to a repo is its collaborators, read and changed through `gh api`.

Every call goes through `gh api --include`, so the HTTP status and headers are read as data and
a rate limit is told apart from a refusal. Nothing here retries: a caller that polls decides when
to come back, from `retry_after` and `reset` in the error.

What GitHub decides, not this module:
- an invitation is pending until the person accepts it, and gives no access before that;
- on a personal repo a collaborator always gets write access (roles apply to organization repos);
- only a repo admin may invite, revoke or see pending invitations;
- at most 50 invitations per repo in 24 hours.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass

from qqsync.project.errors import ProjectError, refused

HOSTS = ("github.com",)
API_TIMEOUT = 60.0
PAGE = 100
MAX_PAGES = 10   # 1,000 collaborators; more than that is reported, never silently cut
USER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
ROLES = ("pull", "triage", "push", "maintain", "admin")
STATUS_RE = re.compile(r"^HTTP/\S+ (\d{3})")


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict[str, str]
    body: object


def _gh(args: list[str]) -> tuple[int, str, str]:
    if shutil.which("gh") is None:
        raise ProjectError("gh-missing", "this needs the GitHub CLI `gh` on PATH (https://cli.github.com), "
                                      "signed in with gh auth login")
    env = {**os.environ, "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"}
    try:
        done = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=API_TIMEOUT, env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return -1, "", f"gh timed out after {int(API_TIMEOUT)}s"
    except OSError as e:
        raise ProjectError("gh-missing", f"cannot run gh: {e}") from None
    return done.returncode, done.stdout, done.stderr


def _parse(out: str) -> Response | None:
    """The status line, headers and JSON body `gh api --include` printed; None if it printed none."""
    head, _, body = out.replace("\r\n", "\n").partition("\n\n")
    lines = head.split("\n")
    m = STATUS_RE.match(lines[0])
    if not m:
        return None
    headers = {}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()
    try:
        data = json.loads(body) if body.strip() else None
    except json.JSONDecodeError:
        data = None
    return Response(int(m.group(1)), headers, data)


def _number(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _message(resp: Response) -> str:
    body = resp.body if isinstance(resp.body, dict) else {}
    text = body.get("message", "")
    # A 422 says only "Validation Failed" there; the reason is in the first entry of `errors`.
    for error in body.get("errors") or []:
        if isinstance(error, dict) and error.get("message"):
            text = error["message"]
            break
    first = str(text).splitlines()[0] if text else ""
    return "".join(c for c in first if c.isprintable())[:200]


def _fail(resp: Response, slug: str) -> ProjectError:
    message, headers = _message(resp), resp.headers
    limited = resp.status == 429 or (resp.status == 403 and (
        headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers or "rate limit" in message.lower()))
    if limited:
        return refused("rate-limited", "GitHub is rate limiting this account; try again later",
                       retry_after=_number(headers.get("retry-after")), reset=_number(headers.get("x-ratelimit-reset")))
    if resp.status == 401:
        return ProjectError("not-signed-in", "GitHub rejected the credentials; run gh auth login")
    if resp.status == 403:
        return refused("no-access", f"GitHub refused this for {slug}: {message or 'forbidden'}")
    if resp.status == 404:
        return refused("not-found", f"{slug}: GitHub found nothing there: the repo or the user does not exist, "
                                    "or this account may not see it")
    return refused("backend-error", f"GitHub answered HTTP {resp.status} for {slug}: {message}")


def api(method: str, path: str, slug: str, fields: dict[str, str] | None = None) -> Response:
    """One API call. A 2xx answer is returned; anything else is raised as a ProjectError."""
    args = ["api", "--include", "--method", method, "-H", "Accept: application/vnd.github+json", path]
    for key, value in (fields or {}).items():
        args += ["-f", f"{key}={value}"]
    code, out, err = _gh(args)
    resp = _parse(out)
    if resp is None:
        reason = ([line.strip() for line in err.splitlines() if line.strip()] or [f"gh exited with {code}"])[-1]
        if code == 4 or "gh auth login" in err:
            raise ProjectError("not-signed-in", "gh is not signed in; run gh auth login")
        raise refused("backend-unavailable", f"GitHub could not be reached: {reason[:200]}")
    if not 200 <= resp.status < 300:
        raise _fail(resp, slug)
    return resp


def _user(user: str) -> str:
    if not USER_RE.fullmatch(user):
        raise ProjectError("bad-user", f"{user!r} is not a GitHub username")
    return user


def _pages(path: str, slug: str) -> list[dict]:
    rows: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        body = api("GET", f"{path}?per_page={PAGE}&page={page}", slug).body
        batch = body if isinstance(body, list) else []
        rows += [row for row in batch if isinstance(row, dict)]
        if len(batch) < PAGE:
            return rows
    raise refused("too-many", f"{slug}: more than {PAGE * MAX_PAGES} entries; not listed")


def _invitations(slug: str) -> list[dict]:
    return [{"id": row.get("id"), "login": (row.get("invitee") or {}).get("login"), "role": row.get("permissions")}
            for row in _pages(f"repos/{slug}/invitations", slug)]


def members(slug: str) -> dict:
    """`members` have access now. `invited` have a pending invitation and no access yet; it is None
    when this account may not see invitations (only a repo admin may)."""
    current = [{"login": row.get("login"), "role": row.get("role_name")}
               for row in _pages(f"repos/{slug}/collaborators", slug)]
    try:
        invited = [{"login": i["login"], "role": i["role"]} for i in _invitations(slug)]
    except ProjectError as e:
        if e.code not in ("no-access", "not-found"):
            raise
        invited = None
    return {"members": current, "invited": invited}


def invite(slug: str, user: str, role: str | None = None) -> dict:
    if role is not None and role not in ROLES:
        raise ProjectError("bad-role", f"{role!r} is not a role; one of {', '.join(ROLES)}")
    resp = api("PUT", f"repos/{slug}/collaborators/{_user(user)}", slug, {"permission": role} if role else None)
    # 201: an invitation was sent. 204: already a collaborator, or an organization member added at once.
    return {"user": user, "state": "invited" if resp.status == 201 else "member"}


def revoke(slug: str, user: str) -> dict:
    """Cancel the user's pending invitations, then remove them as a collaborator. Both are
    no-ops on GitHub's side when there is nothing to remove, so a repeat is harmless."""
    wanted = _user(user).lower()
    cancelled = 0
    for invitation in _invitations(slug):
        if (invitation["login"] or "").lower() == wanted and invitation["id"] is not None:
            api("DELETE", f"repos/{slug}/invitations/{invitation['id']}", slug)
            cancelled += 1
    api("DELETE", f"repos/{slug}/collaborators/{user}", slug)
    return {"user": user, "state": "revoked", "invitations_cancelled": cancelled}
