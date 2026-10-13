"""The GitHub backend against a scripted `gh`: what it asks for, and how it reads the answers."""
from __future__ import annotations

import json

import pytest
from conftest import git

from qqsync.project.backends import github

SLUG = "acme/app"


def http(status: int, body=None, **headers: str) -> tuple[int, str, str]:
    """What `gh api --include` prints for one response. gh exits 1 on an HTTP error."""
    lines = [f"HTTP/2.0 {status} X", *(f"{k.replace('_', '-')}: {v}" for k, v in headers.items())]
    text = "\r\n".join(lines) + "\r\n\r\n" + ("" if body is None else json.dumps(body))
    return (0 if status < 400 else 1), text, ""


class FakeGh:
    def __init__(self) -> None:
        self.script: dict[tuple[str, str], tuple[int, str, str]] = {}
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def on(self, method: str, path: str, answer: tuple[int, str, str]) -> None:
        self.script[(method, path)] = answer

    def __call__(self, args: list[str]) -> tuple[int, str, str]:
        method, path = args[args.index("--method") + 1], args[6]
        fields = dict(a.split("=", 1) for a in args[7:] if a != "-f")
        self.calls.append((method, path, fields))
        return self.script[(method, path)]


@pytest.fixture
def gh(monkeypatch) -> FakeGh:
    fake = FakeGh()
    monkeypatch.setattr(github, "_gh", fake)
    return fake


@pytest.fixture
def app(world):
    git(world.app, "remote", "set-url", "origin", "https://github.com/Acme/App.git")
    return str(world.app)


COLLABORATORS = "repos/acme/app/collaborators?per_page=100&page=1"
INVITATIONS = "repos/acme/app/invitations?per_page=100&page=1"


def test_members_and_pending_invitations(app, gh, run):
    gh.on("GET", COLLABORATORS, http(200, [{"login": "asha", "role_name": "admin"}, {"login": "ben", "role_name": "write"}]))
    gh.on("GET", INVITATIONS, http(200, [{"id": 7, "invitee": {"login": "chen"}, "permissions": "write"}]))
    code, report = run("members", "--root", app)
    assert code == 0
    assert report["repo"] == "github.com/acme/app"
    assert report["members"] == [{"login": "asha", "role": "admin"}, {"login": "ben", "role": "write"}]
    assert report["invited"] == [{"login": "chen", "role": "write"}]


def test_members_reads_every_page(app, gh, run):
    full = [{"login": f"u{i}", "role_name": "write"} for i in range(100)]
    gh.on("GET", COLLABORATORS, http(200, full))
    gh.on("GET", "repos/acme/app/collaborators?per_page=100&page=2", http(200, [{"login": "last", "role_name": "read"}]))
    gh.on("GET", INVITATIONS, http(200, []))
    code, report = run("members", "--root", app)
    assert (code, len(report["members"]), report["members"][-1]["login"]) == (0, 101, "last")


def test_invitations_hidden_from_a_non_admin_are_unknown_not_empty(app, gh, run):
    gh.on("GET", COLLABORATORS, http(200, [{"login": "asha", "role_name": "admin"}]))
    gh.on("GET", INVITATIONS, http(403, {"message": "Must have admin rights to Repository."}))
    code, report = run("members", "--root", app)
    assert (code, report["invited"]) == (0, None)


def test_invite_sends_an_invitation(app, gh, run):
    gh.on("PUT", "repos/acme/app/collaborators/chen", http(201, {"id": 9}))
    code, done = run("invite", "chen", "--root", app)
    assert (code, done["state"], done["user"]) == (0, "invited", "chen")
    assert gh.calls == [("PUT", "repos/acme/app/collaborators/chen", {})]


def test_invite_someone_who_already_has_access(app, gh, run):
    gh.on("PUT", "repos/acme/app/collaborators/ben", http(204))
    code, done = run("invite", "ben", "--root", app, "--role", "push")
    assert (code, done["state"]) == (0, "member")
    assert gh.calls[0][2] == {"permission": "push"}


@pytest.mark.parametrize("argv, error", [
    (["invite", "not a user"], "bad-user"),
    (["invite", "../../orgs/acme"], "bad-user"),
    (["invite", "chen", "--role", "owner"], "bad-role"),
    (["revoke", "a_b"], "bad-user"),
])
def test_bad_input_never_reaches_github(app, gh, run, argv, error):
    code, done = run(*argv, "--root", app)
    assert code == 2
    assert done["error"]["code"] == error
    assert gh.calls == []


def test_revoke_cancels_a_pending_invitation_and_removes_access(app, gh, run):
    gh.on("GET", INVITATIONS, http(200, [{"id": 7, "invitee": {"login": "Chen"}, "permissions": "write"},
                                         {"id": 8, "invitee": {"login": "dana"}, "permissions": "write"}]))
    gh.on("DELETE", "repos/acme/app/invitations/7", http(204))
    gh.on("DELETE", "repos/acme/app/collaborators/chen", http(204))
    code, done = run("revoke", "chen", "--root", app)
    assert (code, done["state"], done["invitations_cancelled"]) == (0, "revoked", 1)
    assert [c[:2] for c in gh.calls] == [("GET", INVITATIONS), ("DELETE", "repos/acme/app/invitations/7"),
                                         ("DELETE", "repos/acme/app/collaborators/chen")]


def test_primary_rate_limit_says_when_to_come_back(app, gh, run):
    gh.on("GET", COLLABORATORS, http(403, {"message": "API rate limit exceeded for user ID 1."},
                                     x_ratelimit_remaining="0", x_ratelimit_reset="1790000000"))
    code, done = run("members", "--root", app)
    assert code == 1
    assert done["error"] == {**done["error"], "code": "rate-limited", "reset": 1790000000, "retry_after": None}
    assert len(gh.calls) == 1   # no retry: the caller decides when


def test_secondary_rate_limit(app, gh, run):
    gh.on("PUT", "repos/acme/app/collaborators/chen", http(429, {"message": "You have exceeded a secondary rate limit."},
                                                           retry_after="60"))
    code, done = run("invite", "chen", "--root", app)
    assert (code, done["error"]["code"], done["error"]["retry_after"]) == (1, "rate-limited", 60)


def test_forbidden_is_not_mistaken_for_a_rate_limit(app, gh, run):
    gh.on("PUT", "repos/acme/app/collaborators/chen", http(403, {"message": "Must have admin rights to Repository."},
                                                           x_ratelimit_remaining="4990"))
    code, done = run("invite", "chen", "--root", app)
    assert (code, done["error"]["code"]) == (1, "no-access")


def test_not_signed_in_cannot_run(app, gh, run):
    gh.on("GET", COLLABORATORS, (4, "", "To get started with GitHub CLI, please run:  gh auth login\n"))
    code, done = run("members", "--root", app)
    assert (code, done["error"]["code"]) == (2, "not-signed-in")


def test_message_from_the_host_is_cut_to_one_printable_line(app, gh, run):
    gh.on("GET", COLLABORATORS, http(403, {"message": "No\x1b[31m\nsecond line"}))
    code, done = run("members", "--root", app)
    assert done["error"]["message"].endswith("No[31m")


def test_other_host_is_refused_before_any_call(world, gh, run):
    git(world.app, "remote", "set-url", "origin", "https://gitlab.example/acme/app.git")
    code, done = run("members", "--root", str(world.app))
    assert (code, done["error"]["code"]) == (1, "unsupported-host")
    assert gh.calls == []


def test_clone_with_a_local_origin_has_no_repo_to_ask_about(world, gh, run):
    code, done = run("members", "--root", str(world.app))
    assert (code, done["error"]["code"]) == (2, "no-remote")


def test_access_commands_need_exactly_one_clone(world, gh, run):
    world.clone("second")
    code, done = run("members", "--root", str(world.projects))
    assert (code, done["error"]["code"]) == (2, "not-one-repo")


def test_validation_error_gives_the_host_s_reason(app, gh, run):
    gh.on("PUT", "repos/acme/app/collaborators/asha", http(422, {
        "message": "Validation Failed", "errors": [{"message": "Repository owner cannot be a collaborator"}]}))
    code, done = run("invite", "asha", "--root", app)
    assert (code, done["error"]["code"]) == (1, "backend-error")
    assert done["error"]["message"].endswith("Repository owner cannot be a collaborator")
