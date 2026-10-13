from conftest import commit, git

from qqsync.cli import main
from qqsync.project import cli


def only(report: dict) -> dict:
    assert len(report["repos"]) == 1
    return report["repos"][0]


def test_current(world, run):
    code, report = run("check", "--root", str(world.app))
    assert code == 0
    assert only(report)["state"] == "current"
    assert only(report)["branch"] == "main"


def test_remote_moved_is_fetch_needed_and_check_fetches_nothing(world, run):
    before = git(world.app, "rev-parse", "origin/main")
    sha = world.push("a")
    code, report = run("check", "--root", str(world.app))
    assert code == cli.UPDATES_AVAILABLE
    assert only(report)["state"] == "fetch-needed"
    assert only(report)["remote"] == sha
    assert only(report)["behind"] is None
    assert git(world.app, "rev-parse", "origin/main") == before   # check reads, it never fetches


def test_behind_once_fetched(world, run):
    world.push("a")
    world.push("b")
    git(world.app, "fetch", "-q", "origin")
    code, report = run("check", "--root", str(world.app))
    assert code == cli.UPDATES_AVAILABLE
    assert (only(report)["state"], only(report)["behind"], only(report)["ahead"]) == ("behind", 2, 0)


def test_ahead_has_nothing_to_receive(world, run):
    commit(world.app, "mine")
    code, report = run("check", "--root", str(world.app))
    assert code == 0
    assert (only(report)["state"], only(report)["ahead"]) == ("ahead", 1)


def test_diverged(world, run):
    commit(world.app, "mine")
    world.push("theirs")
    git(world.app, "fetch", "-q", "origin")
    code, report = run("check", "--root", str(world.app))
    assert code == cli.UPDATES_AVAILABLE
    assert (only(report)["state"], only(report)["behind"], only(report)["ahead"]) == ("diverged", 1, 1)


def test_unreachable_remote_is_a_failure_not_current(world, run):
    git(world.app, "remote", "set-url", "origin", str(world.projects / "gone.git"))
    code, report = run("check", "--root", str(world.app))
    assert code == 1
    assert only(report)["state"] == "unreachable"
    assert only(report)["error"]


def test_folder_of_clones(world, run):
    world.clone("second")
    local = world.projects / "local-only"
    git(world.projects, "init", "-q", "-b", "main", str(local))
    (world.projects / "notes").mkdir()   # not a clone: left out
    world.push("a")
    code, report = run("check", "--root", str(world.projects))
    assert code == cli.UPDATES_AVAILABLE
    assert [(r["name"], r["state"]) for r in report["repos"]] == [
        ("app", "fetch-needed"), ("local-only", "no-remote"), ("second", "fetch-needed")]
    assert (report["updates"], report["failed"]) == (2, 0)


def test_root_inside_a_clone_names_that_clone(world, run):
    sub = world.app / "docs"
    sub.mkdir()
    code, report = run("check", "--root", str(sub))
    assert code == 0
    assert only(report)["name"] == "app"


def test_branch_the_remote_lacks(world, run):
    code, report = run("check", "--root", str(world.app), "--branch", "release")
    assert code == 0
    assert only(report)["state"] == "no-branch"


def test_detached_head_without_branch(world, run):
    git(world.app, "checkout", "-q", "--detach")
    code, report = run("check", "--root", str(world.app))
    assert code == 0
    assert only(report)["state"] == "detached"


def test_bad_branch_name_is_usage(world, run):
    code, report = run("check", "--root", str(world.app), "--branch=--upload-pack=evil")
    assert code == 2
    assert report["error"]["code"] == "bad-branch"


def test_text_output(world, capsys):
    world.push("a")
    git(world.app, "fetch", "-q", "origin")
    assert main(["project", "check", "--root", str(world.app)]) == cli.UPDATES_AVAILABLE
    assert capsys.readouterr().out.split() == ["app", "behind", "1", "to", "apply"]
