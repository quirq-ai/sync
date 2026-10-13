from conftest import git


def leftovers(folder) -> list[str]:
    return [p.name for p in folder.iterdir() if p.name.endswith(".cloning")]


def test_add_clones_a_repo_with_no_manifest(world, run):
    code, done = run("add", str(world.origin), "--root", str(world.projects))
    target = world.projects / "origin"
    assert code == 0
    assert done["path"] == str(target)
    assert done["head"] == git(world.seed, "rev-parse", "HEAD") == git(target, "rev-parse", "HEAD")
    assert not (target / "infra").exists()
    assert leftovers(world.projects) == []


def test_existing_folder_is_refused_and_kept(world, run):
    marker = world.app / "README"
    marker.write_text("local edit")
    code, done = run("add", str(world.origin), "app", "--root", str(world.projects))
    assert code == 1
    assert done["error"]["code"] == "exists"
    assert marker.read_text() == "local edit"


def test_failed_clone_leaves_nothing_behind(world, run):
    code, done = run("add", str(world.projects / "gone.git"), "--root", str(world.projects))
    assert code == 1
    assert done["error"]["code"] == "clone-failed"
    assert not (world.projects / "gone").exists()
    assert leftovers(world.projects) == []


def test_a_half_clone_from_an_earlier_run_is_replaced(world, run):
    stale = world.projects / ".copy.cloning"
    stale.mkdir()
    (stale / "junk").write_text("x")
    code, _ = run("add", str(world.origin), "copy", "--root", str(world.projects))
    assert code == 0
    assert leftovers(world.projects) == []


def test_url_that_reads_as_an_option_is_usage(world, run):
    code, done = run("add", "--root", str(world.projects), "--", "--upload-pack=evil")
    assert code == 2
    assert done["error"]["code"] == "bad-url"


def test_name_that_is_a_path_is_usage(world, run):
    code, done = run("add", str(world.origin), "../escape", "--root", str(world.projects))
    assert code == 2
    assert done["error"]["code"] == "bad-name"
