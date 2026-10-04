import difflib
import json
from pathlib import Path

import pytest

from qqsync.cli import main
from qqsync.errors import ManifestError
from qqsync.manifest import Manifest

FIXTURES = Path(__file__).parent / "fixtures"
A = "sha256:" + "a" * 64
B = "sha256:" + "b" * 64
C = "git:" + "c" * 40

# Deliberately awkward formatting: comments everywhere, odd spacing, inline targets, CRLF.
AWKWARD = (
    '# header comment\r\n'
    'schema   =   "quirq-repo/1"   # trailing\r\n'
    '\r\n'
    'targets = [\r\n'
    '  { name = "server", kind = "python-service" },  # first\r\n'
    '  # a comment between targets\r\n'
    '  { name = "tests", kind = "pytest", deps = ["server"] },\r\n'
    ']\r\n'
    '\r\n'
    '[toolchains.python]   # the interpreter\r\n'
    'version="3.14.8"\r\n'
    f"source = 'https://example.invalid/py.tar'\r\n"
    f'digest = "{A}"  # pinned 2026-10-04\r\n'
)


def changed_lines(before: str, after: str) -> list[str]:
    return [l for l in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
            if l[:1] in "+-" and not l.startswith(("+++", "---"))]


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.repo.toml")), ids=lambda p: p.name)
def test_round_trip_fixture_is_byte_identical(path, tmp_path):
    out = tmp_path / "repo.toml"
    Manifest.read(path).write(out)
    assert out.read_bytes() == path.read_bytes()


def test_round_trip_awkward_is_byte_identical(tmp_path):
    src = tmp_path / "in.toml"
    src.write_bytes(AWKWARD.encode())
    m = Manifest.read(src)
    m.write()
    assert src.read_bytes() == AWKWARD.encode()


def test_set_pin_changes_only_the_digest_line():
    m = Manifest(AWKWARD)
    m.set_pin("toolchains", "python", digest=B)
    assert changed_lines(AWKWARD, m.dumps()) == [
        f'-digest = "{A}"  # pinned 2026-10-04', f'+digest = "{B}"  # pinned 2026-10-04']
    assert m.data["toolchains"]["python"]["digest"] == B


def test_set_pin_version_and_source():
    m = Manifest(AWKWARD)
    m.set_pin("toolchains", "python", digest=B, version="3.15.0", source="https://example.invalid/py315.tar")
    assert m.data["toolchains"]["python"] == {
        "version": "3.15.0", "source": "https://example.invalid/py315.tar", "digest": B}
    assert "# the interpreter" in m.dumps() and "# first" in m.dumps()


def test_add_new_dep_pin():
    m = Manifest(AWKWARD)
    m.set_pin("deps", "recipes", digest=C, source="https://github.com/quirq-ai/recipes")
    assert m.data["deps"] == {"recipes": {"source": "https://github.com/quirq-ai/recipes", "digest": C}}
    assert all(l.startswith("+") for l in changed_lines(AWKWARD, m.dumps()))  # nothing else touched
    assert m.dumps().count("\n") == m.dumps().count("\r\n")  # new lines keep the CRLF endings


def test_new_pin_needs_source():
    m = Manifest(AWKWARD)
    with pytest.raises(ManifestError, match="new pin needs a source"):
        m.set_pin("deps", "recipes", digest=C)


def test_platform_pins():
    path = FIXTURES / "innernet.repo.toml"
    m = Manifest.read(path)
    m.set_pin("toolchains", "node", platform="linux-x86_64", digest=B)
    assert m.data["toolchains"]["node"]["platforms"]["linux-x86_64"]["digest"] == B
    assert m.data["toolchains"]["node"]["platforms"]["macos-arm64"]["digest"] != B
    assert len(changed_lines(path.read_text(), m.dumps())) == 2
    with pytest.raises(ManifestError, match="name the platform"):
        m.set_pin("toolchains", "node", digest=B)
    with pytest.raises(ManifestError, match="drop the platform"):
        Manifest(AWKWARD).set_pin("toolchains", "python", platform="linux-x86_64", digest=B)


def test_invalid_edit_is_rolled_back():
    m = Manifest(AWKWARD)
    with pytest.raises(ManifestError, match="digest"):
        m.set_pin("toolchains", "python", digest="latest")
    assert m.dumps() == AWKWARD
    with pytest.raises(ManifestError, match="'nope' is not a target"):
        m.set_target("tests", "deps", ["nope"])
    assert m.dumps() == AWKWARD


def test_targets_inline_style():
    m = Manifest(AWKWARD)
    m.add_target({"name": "lint", "kind": "pytest", "deps": ["server"]})
    m.set_target("server", "srcs", ["server.py"])
    names = [t["name"] for t in m.data["targets"]]
    assert names == ["server", "tests", "lint"]
    assert m.data["targets"][0]["srcs"] == ["server.py"]
    m.remove_target("lint")
    assert [t["name"] for t in m.data["targets"]] == ["server", "tests"]
    assert "# a comment between targets" in m.dumps()


def test_targets_table_style():
    m = Manifest.read(FIXTURES / "xo-space.repo.toml")
    m.add_target({"name": "lint", "kind": "pytest"})
    assert "[[targets]]\nname = \"lint\"" in m.dumps()
    with pytest.raises(ManifestError, match="'server' is not a target"):
        m.remove_target("server")  # tests depends on it
    with pytest.raises(ManifestError, match="no target named"):
        m.remove_target("ghost")


def test_qq_version():
    m = Manifest.read(FIXTURES / "xo-space.repo.toml")
    m.set_qq_version("0.2.0")
    assert m.data["qq"] == {"version": "0.2.0"}


def test_data_is_a_copy():
    m = Manifest(AWKWARD)
    m.data["toolchains"]["python"]["digest"] = "tampered"
    assert m.data["toolchains"]["python"]["digest"] == A


def test_rejects_unparseable_and_invalid():
    with pytest.raises(ManifestError, match="not valid TOML"):
        Manifest("schema = ")
    with pytest.raises(ManifestError, match="required property"):
        Manifest('schema = "quirq-repo/1"\n')


def test_cli_show_and_pin(tmp_path, capsys):
    path = tmp_path / "repo.toml"
    path.write_bytes(AWKWARD.encode())
    assert main(["show", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["toolchains"]["python"]["digest"] == A
    assert main(["pin", "toolchains", "python", "--digest", B, "--manifest", str(path)]) == 0
    assert Manifest.read(path).data["toolchains"]["python"]["digest"] == B
    assert main(["pin", "toolchains", "python", "--digest", "bad", "--manifest", str(path)]) == 1
    assert "digest" in capsys.readouterr().err
    assert Manifest.read(path).data["toolchains"]["python"]["digest"] == B


def test_write_follows_symlink_and_keeps_mode(tmp_path):
    real = tmp_path / "real.toml"
    real.write_bytes(AWKWARD.encode())
    real.chmod(0o644)
    (tmp_path / "infra").mkdir()
    link = tmp_path / "infra" / "repo.toml"
    link.symlink_to(real)
    m = Manifest.read(link)
    m.set_pin("toolchains", "python", digest=B)
    m.write()
    assert link.is_symlink()
    assert Manifest.read(real).data["toolchains"]["python"]["digest"] == B
    assert real.stat().st_mode & 0o777 == 0o644


def test_write_error_is_a_manifest_error(tmp_path):
    with pytest.raises(ManifestError, match="cannot write"):
        Manifest(AWKWARD).write(tmp_path / "missing-dir" / "repo.toml")


def test_inline_toolchains_table():
    text = ('schema = "quirq-repo/1"\n'
            f'toolchains = {{ py = {{ source = "s", digest = "{A}" }} }}\n'
            'targets = [{ name = "a", kind = "k" }]\n')
    m = Manifest(text)
    m.set_pin("toolchains", "rb", digest=B, source="x")
    m.set_pin("toolchains", "py", digest=B)
    assert m.data["toolchains"] == {"py": {"source": "s", "digest": B}, "rb": {"source": "x", "digest": B}}


def test_unsupported_value_is_a_manifest_error():
    with pytest.raises(ManifestError, match="cannot make this edit"):
        Manifest(AWKWARD).set_target("server", "params", {"x": object()})


def test_reading_does_not_need_the_editor():
    # Valid, but the editor cannot write this table order back byte for byte: readable, not editable.
    text = (f'schema = "quirq-repo/1"\n[toolchains.a]\nsource = "s"\ndigest = "{A}"\n'
            '[[targets]]\nname = "t"\nkind = "k"\n'
            f'[toolchains.b]\nsource = "s"\ndigest = "{A}"\n')
    m = Manifest(text)
    assert set(m.data["toolchains"]) == {"a", "b"}
    try:
        m.set_pin("toolchains", "a", digest=B)
    except ManifestError as e:
        assert "cannot be edited safely" in str(e)
    else:
        assert m.data["toolchains"]["a"]["digest"] == B


def test_cli_show_dates(tmp_path, capsys):
    path = tmp_path / "repo.toml"
    path.write_text('schema = "quirq-repo/1"\ntargets = [{ name = "a", kind = "k", params = { d = 2020-01-01 } }]\n')
    assert main(["show", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["targets"][0]["params"]["d"] == "2020-01-01"
