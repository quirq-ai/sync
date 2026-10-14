"""Keep working clones up to date and manage who has access to their repo.

    project [--fetch-only]           fetch, then bring each clone up to date with its strategy
    project check                    is each clone behind its remote? changes nothing
    project add URL [NAME]           clone a repo (it needs no manifest)
    project members                  who has access to this clone's repo, and who is invited
    project invite USER [--role R]   give USER access
    project revoke USER              remove USER's access, or cancel their invitation

The same commands run as `qq sync project ...` and as `qqsync project ...`.

--root DIR is one clone, or a folder whose direct children are clones (default: where you are).
--json prints one JSON document on stdout; an error is {"error": {"code": ..., "message": ...}}.

Exit codes: 0 done; 1 refused (understood, not done); 2 could not run (usage, git or gh missing,
not signed in); 3 from `check` only: at least one clone has updates to receive.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from qqsync.project import access, add, check, clones, git, update
from qqsync.project.errors import ProjectError

UPDATES_AVAILABLE = 3
UPDATE_ONLY = ("fetch_only", "strategy")   # options of the bare command; a subcommand takes none of them


def _root(args) -> Path:
    return Path(args.root) if args.root else Path.cwd()


def _branch(args) -> str | None:
    if args.branch is not None and not git.valid_branch(args.branch):
        raise ProjectError("bad-branch", f"{args.branch!r} is not a branch name")
    return args.branch


def _emit(args, payload: dict, text) -> None:
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for line in text(payload):
            print(line)


def _table(rows: list[tuple[str, ...]]) -> list[str]:
    if not rows:
        return []
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return ["  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip() for row in rows]


def _check_text(report: dict) -> list[str]:
    def note(r: dict) -> str:
        if r["state"] == "behind":
            return f"{r['behind']} to apply"
        if r["state"] == "diverged":
            return f"{r['behind']} to receive, {r['ahead']} of your own"
        if r["state"] == "ahead":
            return f"{r['ahead']} of your own"
        return r.get("error", "")
    return _table([(r["name"], r["state"], note(r)) for r in report["repos"]]) or ["no clones here"]


def _update_text(report: dict) -> list[str]:
    def note(r: dict) -> str:
        if r["state"] == "refused":
            return r["error"]["message"]
        if r["state"] == "fetched":
            return f"{r['behind']} to apply" if r["behind"] else "nothing to apply"
        return f"applied {r['applied']}" if r["applied"] else "already up to date"
    return _table([(r["name"], r["state"], note(r)) for r in report["repos"]]) or ["no clones here"]


def _members_text(report: dict) -> list[str]:
    rows = [(m["login"] or "?", m["role"] or "", "") for m in report["members"]]
    rows += [(m["login"] or "?", m["role"] or "", "invited, not accepted yet") for m in report["invited"] or []]
    lines = _table(rows) or ["nobody"]
    if report["invited"] is None:
        lines.append("(pending invitations are visible to a repo admin only)")
    return lines


def cmd_update(args) -> int:
    report = update.update(clones.find(_root(args)), _branch(args), fetch_only=args.fetch_only, strategy=args.strategy)
    _emit(args, report, _update_text)
    return 1 if report["refused"] else 0


def cmd_check(args) -> int:
    report = check.check(clones.find(_root(args)), _branch(args))
    _emit(args, report, _check_text)
    if report["failed"]:
        return 1
    return UPDATES_AVAILABLE if report["updates"] else 0


def cmd_add(args) -> int:
    done = add.add(args.url, _root(args), args.name)
    _emit(args, done, lambda d: [f"cloned into {d['path']}"])
    return 0


def cmd_members(args) -> int:
    _emit(args, access.members(clones.one(_root(args)), args.backend), _members_text)
    return 0


def cmd_invite(args) -> int:
    done = access.invite(clones.one(_root(args)), args.backend, args.user, args.role)
    said = "invited; they have access once they accept" if done["state"] == "invited" else "has access"
    _emit(args, done, lambda d: [f"{d['user']}: {said}"])
    return 0


def cmd_revoke(args) -> int:
    done = access.revoke(clones.one(_root(args)), args.backend, args.user)
    _emit(args, done, lambda d: [f"{d['user']}: access removed"])
    return 0


def _shared(*, top: bool) -> argparse.ArgumentParser:
    """--root, --json and --branch, accepted before or after the subcommand. A subcommand's copy
    has no default of its own, so it never wipes out a value given before the subcommand."""
    default = None if top else argparse.SUPPRESS
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--root", metavar="DIR", default=default,
                        help="a clone, or a folder of clones (default: where you are)")
    shared.add_argument("--json", action="store_true", default=False if top else argparse.SUPPRESS,
                        help="print one JSON document")
    shared.add_argument("--branch", default=default, help="the branch to follow (default: the one checked out)")
    return shared


def register(sub, prefix: str = "qqsync") -> None:
    """Add `project` to a parser's subcommands: qqsync's own, and `qq sync`'s. Its parser sets
    `run`, a function of the parsed args returning the exit code. `prefix` starts error lines."""
    project = sub.add_parser("project", parents=[_shared(top=True)],
                             help="keep working clones up to date; manage who has access",
                             description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    project.add_argument("--fetch-only", action="store_true", help="download new commits; leave the working files alone")
    project.add_argument("--strategy", help="use this strategy once (default: git config qqsync.strategy, else ff-only)")
    project.set_defaults(func=cmd_update)

    commands = project.add_subparsers(dest="project_command", metavar="COMMAND")
    shared = _shared(top=False)
    host = argparse.ArgumentParser(add_help=False)
    host.add_argument("--backend", help="the code host backend (default: $QQ_BACKEND, else github)")

    s = commands.add_parser("check", parents=[shared], help="is each clone behind its remote?")
    s.set_defaults(func=cmd_check)

    s = commands.add_parser("add", parents=[shared], help="clone a repo into --root")
    s.add_argument("url", help="the repo to clone")
    s.add_argument("name", nargs="?", help="the folder to create (default: the repo's name)")
    s.set_defaults(func=cmd_add)

    s = commands.add_parser("members", parents=[shared, host], help="who has access to this clone's repo")
    s.set_defaults(func=cmd_members)

    s = commands.add_parser("invite", parents=[shared, host], help="give a user access")
    s.add_argument("user")
    s.add_argument("--role", help="the access to give, on a repo whose host has roles")
    s.set_defaults(func=cmd_invite)

    s = commands.add_parser("revoke", parents=[shared, host], help="remove a user's access or invitation")
    s.add_argument("user")
    s.set_defaults(func=cmd_revoke)

    def run(args) -> int:
        try:
            if args.project_command and any(getattr(args, name) for name in UPDATE_ONLY):
                raise ProjectError("bad-usage", f"--fetch-only and --strategy belong to `project` alone, "
                                                f"not to `project {args.project_command}`")
            return args.func(args)
        except ProjectError as e:
            if args.json:
                print(json.dumps({"error": e.to_json()}, indent=2, sort_keys=True))
            print(f"{prefix}: {e.message}", file=sys.stderr)
            return e.exit_code

    project.set_defaults(run=run)
