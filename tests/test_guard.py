import subprocess
from pathlib import Path

import pytest

from qqsync.cli import main
from qqsync.guard import scan

REPO = Path(__file__).resolve().parent.parent

# Second parsers in several languages, each planted in an otherwise clean repo.
PLANTED = {
    "scripts/pins.py": 'import tomllib\nwith open("infra/repo.toml", "rb") as f:\n    pins = tomllib.load(f)\n',
    "tools/read.mjs": 'import { parse } from "smol-toml";\nconst m = parse(fs.readFileSync("infra/repo.toml", "utf8"));\n',
    "lib/manifest.js": 'const TOML = require("@iarna/toml");\nconst m = TOML.parse(read("infra/repo.toml"));\n',
    "src/main.rs": 'let m: Manifest = toml::from_str(&fs::read_to_string("infra/repo.toml")?)?;\n',
    "ci/check.sh": 'python -c "import tomli; print(tomli.load(open(\'infra/repo.toml\', \'rb\')))"\n',
}

CLEAN = {
    "README.md": "We use tomllib to read infra/repo.toml.\n",  # prose is skipped
    "infra/repo.toml": 'schema = "quirq-repo/1"\n',             # the manifest itself
    "ci/build.sh": "qqsync show infra/repo.toml | jq .targets\n", # reading through qqsync is the rule
    "config/load.py": 'import tomllib\nkinds = tomllib.load(open("config/kinds.toml", "rb"))\n',  # another file
    "docs/notes.py": "# The manifest repo.toml is TOML, parsed only by qqsync.\n",
}


def make_repo(root: Path, files: dict[str, str], git: bool = True) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    if git:
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    return root


def test_clean_repo_passes(tmp_path):
    assert scan(make_repo(tmp_path, CLEAN)) == []


@pytest.mark.parametrize("rel", sorted(PLANTED))
def test_planted_parser_is_found(tmp_path, rel):
    root = make_repo(tmp_path, {**CLEAN, rel: PLANTED[rel]})
    assert [f.path for f in scan(root)] == [rel]


def test_untracked_files_are_ignored_in_a_checkout(tmp_path):
    root = make_repo(tmp_path, CLEAN)
    (root / "scratch.py").write_text(PLANTED["scripts/pins.py"])
    assert scan(root) == []


def test_works_without_git(tmp_path):
    root = make_repo(tmp_path, {**CLEAN, "scripts/pins.py": PLANTED["scripts/pins.py"]}, git=False)
    assert [f.path for f in scan(root)] == ["scripts/pins.py"]


def test_allow(tmp_path):
    root = make_repo(tmp_path, {**CLEAN, "scripts/pins.py": PLANTED["scripts/pins.py"]})
    assert scan(root, ["scripts/*"]) == []


# V0-SYN-04 done-when: a planted second parser fails presubmit.
def test_cli_guard(tmp_path, capsys):
    clean = make_repo(tmp_path / "clean", CLEAN)
    assert main(["guard", str(clean)]) == 0
    planted = make_repo(tmp_path / "planted", {**CLEAN, "scripts/pins.py": PLANTED["scripts/pins.py"]})
    assert main(["guard", str(planted)]) == 1
    assert "scripts/pins.py:1: parses repo.toml outside qqsync" in capsys.readouterr().err


def test_this_repo_has_one_parser():
    # The library itself, and the planted samples above, are the only allowed exceptions.
    assert scan(REPO, ["src/qqsync/*", "tests/test_guard.py"]) == []
