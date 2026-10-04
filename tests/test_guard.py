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
    "tools/vendored.py": 'import pytoml\nm = pytoml.load(open("infra/repo.toml"))\n',
    "tools/via_constant.py": ('from qqsync.manifest import DEFAULT_PATH\nimport tomllib\n'
                              'm = tomllib.load(open(DEFAULT_PATH, "rb"))\n'),
    "src/Read.java": 'Toml m = new Toml().read(new File("infra/repo.toml"));\n',
    "ci/pins.sh": "yq -oy infra/repo.toml\n",
    "web/manifest.ts": 'import manifest from "../infra/repo.toml";\n',
    # The audit's bypasses of the first version (wave1-sync-toolchains.md, S-2).
    "tools/trailing_note.py": ('import tomllib\n'
                               'm = tomllib.load(open("infra/repo.toml", "rb"))  # see qqsync show\n'),
    "web/semi.js": 'const TOML = require("@iarna/toml")\n;TOML.parse(fs.readFileSync("infra/repo.toml", "utf8"))\n',
    "tools/star.py": 'import tomllib\n*rest, = tomllib.load(open("infra/repo.toml", "rb")).items()\n',
    "tools/split.py": 'import tomllib\nm = tomllib.load(open("infra/repo" ".toml", "rb"))\n',
    "tools/glob.py": ('import glob, tomllib\n'
                      'm = [tomllib.load(open(p, "rb")) for p in glob.glob("infra/repo.t*")]\n'),
    "tools/dynamic.py": ('import importlib\nlib = importlib.import_module("tom" + "llib")\n'
                         'm = lib.load(open("infra/repo.toml", "rb"))\n'),
    # Review of the second version: names bound by a plain import still read.
    "tools/from_import.py": 'from tomllib import load\nm = load(open("infra/repo.toml", "rb"))\n',
    "tools/alias.py": 'import tomli as T\nm = T.load(open("infra/repo.toml", "rb"))\n',
    "src/lib.rs": 'use toml::from_str;\nlet m: M = from_str(&fs::read_to_string("infra/repo.toml")?)?;\n',
    "tools/far.py": ('import tomllib\nfrom pathlib import Path\np = Path("infra") / "repo.toml"\n'
                     + "x = 1\n" * 6 + "m = tomllib.loads(p.read_text())\n"),
}

# A path constant in one module, read with a TOML library in another (also an audit bypass).
CROSS_MODULE = {
    "tools/paths.py": 'MANIFEST = "infra/repo.toml"\n',
    "tools/read.py": 'import tomllib\nfrom tools.paths import MANIFEST\n\n\ndef load():\n'
                     '    return tomllib.load(open(MANIFEST, "rb"))\n',
}

CLEAN = {
    "README.md": "We use tomllib to read infra/repo.toml.\n",  # prose is skipped
    "infra/repo.toml": 'schema = "quirq-repo/1"\n',             # the manifest itself
    "ci/build.sh": "qqsync show infra/repo.toml | jq .targets\n", # reading through qqsync is the rule
    "config/load.py": 'import tomllib\nkinds = tomllib.load(open("config/kinds.toml", "rb"))\n',  # another file
    "docs/notes.py": "# The manifest repo.toml is TOML, parsed only by qqsync.\n",
    ".github/workflows/ci.yml": "      - run: pip install tomli qqsync\n      - run: qqsync validate infra/repo.toml\n",
    "package.json": '{"scripts": {"pins": "qqsync show infra/repo.toml"}, "dependencies": {"smol-toml": "1"}}\n',
    "tools/note.py": "# don't parse repo.toml yourself; toml readers drift\nURL = 'https://toml.io'\n",
    # The audit's false positive: mentions the manifest, reads its own pyproject.toml.
    "tools/version.py": ('"""Prints the version.\n\nThe pins live in infra/repo.toml; read them with qqsync.\n"""\n'
                         'import tomllib\n\n\ndef version():\n'
                         '    with open("pyproject.toml", "rb") as f:\n'
                         '        return tomllib.load(f)["project"]["version"]\n'),
    # Lines that name the manifest without assigning it a path name nothing else.
    "tests/test_cli.py": ('import subprocess\nresult = subprocess.run(["qqsync", "show", "infra/repo.toml"])\n'
                          'cmd = ["qqsync", "show", "infra/repo.toml"]\n'
                          'print(manifest.load(path="infra/repo.toml"))\n'),
    "tools/fmt.py": ('import tomllib\n\n\ndef read(path):\n    with open(path, "rb") as f:\n'
                     '        result = tomllib.load(f)\n    cmd = result\n    return cmd\n'),
    "build.rs": ('fn main() {\n    println!("cargo:rerun-if-changed=../infra/repo.toml");\n}\n'),
    "src/cargo.rs": 'let cargo: toml::Value = toml::from_str(&read("Cargo.toml"))?;\n',
    "web/doc.js": ('/**\n * Do not read infra/repo.toml with smol-toml; call qqsync show.\n */\n'
                   'export const x = 1;\n'),
    ".vscode/settings.json": '{"evenBetterToml.schema.associations": {"infra/repo.toml": "x"}}\n',
    "tools/marked.py": ('import tomllib\n'
                        'm = tomllib.load(open("infra/repo.toml", "rb"))  # qqsync-guard: allow (reviewed)\n'),
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


def test_path_constant_in_another_module(tmp_path):
    root = make_repo(tmp_path, {**CLEAN, **CROSS_MODULE})
    assert [(f.path, f.line) for f in scan(root)] == [("tools/read.py", 6)]


def test_marker_needs_a_reason(tmp_path):
    root = make_repo(tmp_path, {**CLEAN, "tools/m.py": 'import tomllib\n'
                                'm = tomllib.load(open("infra/repo.toml", "rb"))  # qqsync-guard: allow\n'})
    notes = []
    assert [f.path for f in scan(root, notes=notes)] == ["tools/m.py"]
    assert any("needs a reason" in n for n in notes)
    assert any("exempted by qqsync-guard: reviewed" in n for n in notes)


def test_long_lines_stay_fast(tmp_path):
    import time
    blob = "a" * 200_000 + ":a" * 50_000 + ' "infra/repo.toml"\n'
    root = make_repo(tmp_path, {**CLEAN, "web/bundle.js": "x = " + blob + "toml" * 50_000 + "\n"})
    start = time.monotonic()
    scan(root)
    assert time.monotonic() - start < 5


def test_untracked_files_are_ignored_in_a_checkout(tmp_path):
    root = make_repo(tmp_path, CLEAN)
    (root / "scratch.py").write_text(PLANTED["scripts/pins.py"])
    assert scan(root) == []


def test_works_without_git(tmp_path):
    root = make_repo(tmp_path, {**CLEAN, "scripts/pins.py": PLANTED["scripts/pins.py"]}, git=False)
    assert [f.path for f in scan(root)] == ["scripts/pins.py"]


def test_root_must_be_a_directory(tmp_path, capsys):
    assert main(["guard", str(tmp_path / "nope")]) == 1
    assert "not a directory" in capsys.readouterr().err


def test_non_utf8_filename(tmp_path):
    root = make_repo(tmp_path, CLEAN)
    (root / "caf\udce9.py".encode("utf-8", "surrogateescape").decode("utf-8", "surrogateescape")).write_text("x = 1\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    assert scan(root) == []


def test_allow(tmp_path):
    root = make_repo(tmp_path, {**CLEAN, "scripts/pins.py": PLANTED["scripts/pins.py"]})
    assert scan(root, ["scripts/*"]) == []


# V0-SYN-04 done-when: a planted second parser fails presubmit.
def test_cli_guard(tmp_path, capsys):
    clean = make_repo(tmp_path / "clean", CLEAN)
    assert main(["guard", str(clean)]) == 0
    planted = make_repo(tmp_path / "planted", {**CLEAN, "scripts/pins.py": PLANTED["scripts/pins.py"]})
    assert main(["guard", str(planted)]) == 1
    assert "scripts/pins.py:3: parses repo.toml outside qqsync" in capsys.readouterr().err


def test_this_repo_has_one_parser():
    # The library itself, and the planted samples above, are the only allowed exceptions.
    assert scan(REPO, ["src/qqsync/*", "tests/test_guard.py"]) == []
