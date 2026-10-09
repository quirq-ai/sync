from pathlib import Path

import pytest

from qqsync.cli import main
from qqsync.init import InitError, init, render
from qqsync.manifest import Manifest

FIXTURES = Path(__file__).parent / "fixtures"
KINDS = FIXTURES / "kinds.toml"
PROMOTED = FIXTURES / "promoted.toml"


def _init(root, kinds, kinds_file=KINDS, promoted=PROMOTED, qq_version="0.1.0"):
    return init(root, kinds, kinds_file, promoted, qq_version)


def _leftovers(root):
    return sorted(p.name for p in (root / "infra").glob(".repo.toml.*"))


@pytest.mark.parametrize("kinds, fixture, toolchain", [
    (["python-service", "pytest"], "xo-space.repo.toml", "python"),
    (["node-app"], "innernet.repo.toml", "node"),
])
def test_init_then_validate_passes(tmp_path, capsys, kinds, fixture, toolchain):
    args = [a for k in kinds for a in ("--kind", k)]
    assert main(["init", str(tmp_path), *args, "--kinds", str(KINDS), "--promoted", str(PROMOTED),
                 "--qq-version", "0.1.0"]) == 0
    dest = tmp_path / "infra" / "repo.toml"
    assert f"{dest}: written" in capsys.readouterr().out
    assert main(["validate", str(dest), *args]) == 0
    assert main(["pins", str(dest), "--strict"]) == 0

    made = Manifest.read(dest).data
    onboarded = Manifest.read(FIXTURES / fixture).data
    assert list(made["toolchains"]) == [toolchain]
    assert made["toolchains"] == {toolchain: onboarded["toolchains"][toolchain]}
    assert made["qq"] == {"version": "0.1.0"}
    assert [(t["name"], t["kind"]) for t in made["targets"]] == [(k, k) for k in kinds]
    assert _leftovers(tmp_path) == []


def test_same_inputs_same_bytes(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = _init(tmp_path / "a", ["pytest", "node-app"])
    b = _init(tmp_path / "b", ["pytest", "node-app"])
    assert a.read_bytes() == b.read_bytes()
    assert a.read_bytes() == render(["pytest", "node-app"], KINDS, PROMOTED, "0.1.0").encode()


def test_kind_without_toolchain_pins_none(tmp_path):
    made = Manifest.read(_init(tmp_path, ["static-docs"])).data
    assert "toolchains" not in made or made["toolchains"] == {}


def test_two_kinds_one_toolchain_pin_it_once():
    text = render(["python-service", "pytest"], KINDS, PROMOTED, "0.1.0")
    assert text.count("[toolchains.") == 1


def test_refuses_existing_manifest(tmp_path, capsys):
    (tmp_path / "infra").mkdir()
    dest = tmp_path / "infra" / "repo.toml"
    dest.write_text("mine\n")
    with pytest.raises(InitError, match="already exists"):
        _init(tmp_path, ["node-app"])
    assert main(["init", str(tmp_path), "--kind", "node-app", "--kinds", str(KINDS),
                 "--promoted", str(PROMOTED), "--qq-version", "0.1.0"]) == 1
    assert "qqsync init:" in capsys.readouterr().err
    assert dest.read_text() == "mine\n"
    assert _leftovers(tmp_path) == []


def test_refuses_dangling_manifest_link(tmp_path):
    (tmp_path / "infra").mkdir()
    (tmp_path / "infra" / "repo.toml").symlink_to(tmp_path / "elsewhere.toml")
    with pytest.raises(InitError, match="already exists"):
        _init(tmp_path, ["node-app"])
    assert not (tmp_path / "elsewhere.toml").exists()


def test_refuses_linked_infra(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "infra").symlink_to(outside)
    with pytest.raises(InitError, match="not a link"):
        _init(repo, ["node-app"])
    assert list(outside.iterdir()) == []


def test_refuses_infra_file(tmp_path):
    (tmp_path / "infra").write_text("")
    with pytest.raises(InitError, match="not a link or a file"):
        _init(tmp_path, ["node-app"])


def test_refuses_missing_root(tmp_path):
    with pytest.raises(InitError, match="not a directory"):
        _init(tmp_path / "nope", ["node-app"])


@pytest.mark.parametrize("kinds, message", [
    ([], "at least one --kind"),
    (["node-app", "node-app"], "more than once"),
    (["rust-binary"], "not a kind"),
])
def test_refuses_bad_kinds(tmp_path, kinds, message):
    with pytest.raises(InitError, match=message):
        _init(tmp_path, kinds)
    assert not (tmp_path / "infra").exists()


def test_refuses_bad_qq_version(tmp_path):
    with pytest.raises(InitError, match="valid manifest"):
        _init(tmp_path, ["node-app"], qq_version="")
    assert not (tmp_path / "infra").exists()


def test_refuses_wrong_promoted_schema(tmp_path):
    bad = tmp_path / "promoted.toml"
    bad.write_text('schema = "something/1"\n')
    with pytest.raises(InitError, match="quirq-toolchains-promoted/1"):
        _init(tmp_path, ["node-app"], promoted=bad)


def test_refuses_toolchain_not_promoted(tmp_path):
    only_python = tmp_path / "promoted.toml"
    text = PROMOTED.read_text()
    only_python.write_text(text[:text.index("[[toolchain]]")] + "".join(
        "[[toolchain]]" + part for part in text.split("[[toolchain]]")[1:] if 'name = "node"' not in part))
    with pytest.raises(InitError, match="no promoted pin for toolchain 'node'"):
        _init(tmp_path, ["node-app"], promoted=only_python)
    render(["pytest"], KINDS, only_python, "0.1.0")  # python is still there


def test_refuses_unreadable_inputs(tmp_path):
    with pytest.raises(InitError, match="cannot read the kinds list"):
        _init(tmp_path, ["node-app"], kinds_file=tmp_path / "missing.toml")
    broken = tmp_path / "broken.toml"
    broken.write_text("[[kind]\n")
    with pytest.raises(InitError, match="not valid TOML"):
        _init(tmp_path, ["node-app"], kinds_file=broken)
