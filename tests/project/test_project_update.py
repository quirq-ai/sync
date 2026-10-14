import sys
import types

from conftest import commit, git


def only(report: dict) -> dict:
    assert len(report["repos"]) == 1
    return report["repos"][0]


def head(repo) -> str:
    return git(repo, "rev-parse", "HEAD")


def test_fetch_only_downloads_and_leaves_the_working_files(world, run):
    before = head(world.app)
    sha = world.push("a")
    code, report = run("--root", str(world.app), "--fetch-only")
    assert code == 0
    assert (only(report)["state"], only(report)["behind"]) == ("fetched", 1)
    assert head(world.app) == before
    assert not (world.app / "a").exists()
    assert git(world.app, "rev-parse", "origin/main") == sha


def test_sync_fast_forwards(world, run):
    world.push("a")
    sha = world.push("b")
    code, report = run("--root", str(world.app))
    assert code == 0
    assert only(report) == {**only(report), "state": "synced", "strategy": "ff-only", "applied": 2, "head": sha}
    assert head(world.app) == sha
    code, report = run("--root", str(world.app))   # again: nothing left to do
    assert (code, only(report)["applied"]) == (0, 0)


def test_diverged_clone_is_refused_and_untouched(world, run):
    mine = commit(world.app, "mine")
    world.push("theirs")
    code, report = run("--root", str(world.app))
    assert code == 1
    assert only(report)["state"] == "refused"
    assert only(report)["error"] == {**only(report)["error"], "code": "diverged", "ahead": 1, "behind": 1}
    assert head(world.app) == mine


def test_local_change_in_the_way_is_refused_and_kept(world, run):
    world.push("README", "theirs")
    (world.app / "README").write_text("mine, not committed")
    code, report = run("--root", str(world.app))
    assert code == 1
    assert only(report)["error"] == {**only(report)["error"], "code": "local-changes", "files": ["README"]}
    assert (world.app / "README").read_text() == "mine, not committed"


def test_untracked_file_in_the_way_is_refused_and_kept(world, run):
    world.push("notes", "theirs")
    (world.app / "notes").write_text("mine, not in git")
    code, report = run("--root", str(world.app))
    assert code == 1
    assert only(report)["error"] == {**only(report)["error"], "code": "untracked-in-the-way", "files": ["notes"]}
    assert (world.app / "notes").read_text() == "mine, not in git"


def test_local_change_not_in_the_way_is_carried_along(world, run):
    sha = world.push("a")
    (world.app / "README").write_text("mine, not committed")
    code, report = run("--root", str(world.app))
    assert (code, head(world.app)) == (0, sha)
    assert (world.app / "README").read_text() == "mine, not committed"


def test_another_branch_checked_out_is_never_moved(world, run):
    git(world.app, "switch", "-q", "-c", "feature")
    before = head(world.app)
    sha = world.push("a")
    code, report = run("--root", str(world.app), "--branch", "main")
    assert code == 1
    assert only(report)["error"]["code"] == "other-branch"
    assert head(world.app) == before
    assert git(world.app, "rev-parse", "origin/main") == sha   # fetched all the same


def test_lock_left_by_another_git_is_named(world, run):
    world.push("a")
    (world.app / ".git" / "index.lock").write_text("")
    code, report = run("--root", str(world.app))
    assert (code, only(report)["error"]["code"]) == (1, "locked")


def test_merge_in_progress_is_refused(world, run):
    before = head(world.app)
    world.push("a")
    (world.app / ".git" / "MERGE_HEAD").write_text(before)   # what an unfinished git merge leaves
    code, report = run("--root", str(world.app))
    assert (code, only(report)["error"]["code"]) == (1, "unfinished")
    assert head(world.app) == before


def test_one_refusal_does_not_stop_the_others(world, run):
    second = world.clone("second")
    commit(world.app, "mine")
    sha = world.push("theirs")
    code, report = run("--root", str(world.projects))
    assert code == 1
    assert [(r["name"], r["state"]) for r in report["repos"]] == [("app", "refused"), ("second", "synced")]
    assert head(second) == sha


def test_unreachable_remote_is_refused(world, run):
    git(world.app, "remote", "set-url", "origin", str(world.projects / "gone.git"))
    code, report = run("--root", str(world.app))
    assert code == 1
    assert only(report)["error"]["code"] == "fetch-failed"


def test_strategy_is_read_from_the_clone_s_own_git_config(world, run, monkeypatch):
    calls = []
    module = types.ModuleType("qqsync.project.strategies.pinned")
    module.apply = lambda clone, branch: calls.append((clone.name, branch)) or 7
    monkeypatch.setitem(sys.modules, "qqsync.project.strategies.pinned", module)
    git(world.app, "config", "qqsync.strategy", "pinned")
    world.push("a")
    code, report = run("--root", str(world.app))
    assert code == 0
    assert (only(report)["strategy"], only(report)["applied"]) == ("pinned", 7)
    assert calls == [("app", "main")]
    assert not (world.app / "a").exists()   # the default strategy did not also run


def test_strategy_flag_wins_once(world, run):
    git(world.app, "config", "qqsync.strategy", "no-such-thing")
    sha = world.push("a")
    code, report = run("--root", str(world.app), "--strategy", "ff-only")
    assert (code, only(report)["head"]) == (0, sha)


def test_unknown_strategy_cannot_run(world, run):
    git(world.app, "config", "qqsync.strategy", "no-such-thing")
    code, report = run("--root", str(world.app))
    assert code == 2
    assert report["error"]["code"] == "unknown-strategy"


def test_strategy_name_is_never_a_module_path(world, run):
    code, report = run("--root", str(world.app), "--strategy", "os.path")
    assert code == 2
    assert report["error"]["code"] == "bad-strategy"


def test_fetch_only_never_loads_a_strategy(world, run):
    git(world.app, "config", "qqsync.strategy", "no-such-thing")
    world.push("a")
    code, report = run("--root", str(world.app), "--fetch-only")
    assert (code, only(report)["state"]) == (0, "fetched")
