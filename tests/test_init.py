import errno
import os
import stat
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
    assert [(t["name"], t["kind"], t["cacheable"]) for t in made["targets"]] == [(k, k, False) for k in sorted(kinds)]
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


def _promoted_with(tmp_path, old, new):
    p = tmp_path / "promoted-edited.toml"
    text = PROMOTED.read_text()
    assert old in text
    p.write_text(text.replace(old, new, 1))
    return p


def _kinds_with(tmp_path, extra):
    p = tmp_path / "kinds-edited.toml"
    p.write_text(KINDS.read_text() + extra)
    return p


def test_refuses_input_not_utf8(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_bytes(b"\xff\xfe")
    with pytest.raises(InitError, match="not UTF-8"):
        _init(tmp_path, ["node-app"], kinds_file=bad)
    with pytest.raises(InitError, match="not UTF-8"):
        _init(tmp_path, ["node-app"], promoted=bad)


def test_refuses_kind_listed_twice(tmp_path):
    kinds = _kinds_with(tmp_path, '\n[[kind]]\nname = "node-app"\n')
    with pytest.raises(InitError, match="'node-app' is listed twice"):
        _init(tmp_path, ["node-app"], kinds_file=kinds)


@pytest.mark.parametrize("toolchain", ['""', '["node"]', "1"])
def test_refuses_toolchain_that_is_not_a_name(tmp_path, toolchain):
    kinds = tmp_path / "kinds.toml"
    kinds.write_text(f'[[kind]]\nname = "app"\ntoolchain = {toolchain}\n')
    with pytest.raises(InitError, match="not a name"):
        _init(tmp_path, ["app"], kinds_file=kinds)


def test_refuses_promoted_field_of_wrong_type(tmp_path):
    promoted = _promoted_with(tmp_path, 'version = "24.21.0"', "version = 24")
    with pytest.raises(InitError, match="version must be a non-empty string"):
        _init(tmp_path, ["node-app"], promoted=promoted)


def test_refuses_layer_that_is_not_a_hash(tmp_path):
    promoted = _promoted_with(tmp_path, 'layer_sha256 = "', 'layer_sha256 = "sha256:')
    with pytest.raises(InitError, match="layer_sha256 must be 64 lowercase hex"):
        _init(tmp_path, ["node-app"], promoted=promoted)


def test_writes_any_unicode_source(tmp_path):
    promoted = _promoted_with(tmp_path, 'ref = "oci://ghcr.io/quirq-ai/toolchains/node@sha256:a84e067c',
                              'ref = "https://example.test/\U0001F600\\"/node@sha256:a84e067c')
    made = Manifest.read(_init(tmp_path, ["node-app"], promoted=promoted)).data
    assert made["toolchains"]["node"]["platforms"]["linux-x86_64"]["source"].startswith(
        'https://example.test/\U0001F600"/node')


def test_more_platforms_in_order(tmp_path):
    text = PROMOTED.read_text()
    start = text.index("[[toolchain]]", text.index('name = "node"') - 40)
    end = text.find("[[toolchain]]", start + 1)
    node = text[start:end if end != -1 else len(text)]
    promoted = tmp_path / "promoted.toml"
    promoted.write_text(text + "\n" + node.replace('"linux-x86_64"', '"darwin-arm64"'))
    made = _init(tmp_path, ["node-app"], promoted=promoted).read_text()
    assert made.index("platforms.darwin-arm64") < made.index("platforms.linux-x86_64")


def test_refuses_directory_at_manifest_path(tmp_path):
    (tmp_path / "infra" / "repo.toml").mkdir(parents=True)
    with pytest.raises(InitError, match="a directory, not a manifest"):
        _init(tmp_path, ["node-app"])


def test_write_failure_is_an_error_and_leaves_nothing(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr("qqsync.init._write_new", fail)
    with pytest.raises(InitError, match="cannot write: Permission denied"):
        _init(tmp_path, ["node-app"])
    assert not (tmp_path / "infra").exists()


def test_manifest_appearing_before_the_link_is_kept(tmp_path, monkeypatch):
    real_link = os.link

    def racing_link(src, dst):
        Path(dst).write_text("theirs\n")
        return real_link(src, dst)
    monkeypatch.setattr("qqsync.init.os.link", racing_link)
    with pytest.raises(InitError, match="already exists"):
        _init(tmp_path, ["node-app"])
    assert (tmp_path / "infra" / "repo.toml").read_text() == "theirs\n"
    assert _leftovers(tmp_path) == []


def test_no_hard_links_falls_back_to_exclusive_create(tmp_path, monkeypatch):
    def no_link(src, dst):
        raise PermissionError(errno.EPERM, "Operation not permitted")
    monkeypatch.setattr("qqsync.init.os.link", no_link)
    dest = _init(tmp_path, ["node-app"])
    assert dest.read_text() == render(["node-app"], KINDS, PROMOTED, "0.1.0")
    assert _leftovers(tmp_path) == []


def test_mode_follows_umask(tmp_path):
    old = os.umask(0o027)
    try:
        dest = _init(tmp_path, ["node-app"])
    finally:
        os.umask(old)
    assert stat.S_IMODE(dest.stat().st_mode) == 0o640


def test_kind_order_does_not_change_the_bytes():
    assert render(["pytest", "python-service"], KINDS, PROMOTED, "0.1.0") == \
        render(["python-service", "pytest"], KINDS, PROMOTED, "0.1.0")


def test_pins_note_only_with_pins():
    assert "promoted" in render(["node-app"], KINDS, PROMOTED, "0.1.0")
    assert "promoted" not in render(["static-docs"], KINDS, PROMOTED, "0.1.0")


def test_interrupt_leaves_nothing(tmp_path, monkeypatch):
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr("qqsync.init._write_new", interrupt)
    with pytest.raises(KeyboardInterrupt):
        _init(tmp_path, ["node-app"])
    assert not (tmp_path / "infra").exists()


def test_failed_temp_cleanup_after_link_is_still_success(tmp_path, monkeypatch):
    real_unlink = os.unlink

    def unlink(path, *args, **kwargs):
        if ".part" in str(path):
            real_unlink(path)
            raise FileNotFoundError(errno.ENOENT, "No such file or directory")
        return real_unlink(path, *args, **kwargs)
    monkeypatch.setattr("qqsync.init.os.unlink", unlink)
    dest = _init(tmp_path, ["node-app"])
    assert dest.read_text() == render(["node-app"], KINDS, PROMOTED, "0.1.0")


def test_infra_made_meanwhile_is_used(tmp_path, monkeypatch):
    real_mkdir = Path.mkdir

    def racing_mkdir(self, *args, **kwargs):
        real_mkdir(self)
        raise FileExistsError(errno.EEXIST, "File exists")
    monkeypatch.setattr(Path, "mkdir", racing_mkdir)
    assert _init(tmp_path, ["node-app"]).is_file()


def test_refuses_kinds_file_without_kinds(tmp_path):
    with pytest.raises(InitError, match="lists no \\[\\[kind\\]\\] entries"):
        _init(tmp_path, ["node-app"], kinds_file=PROMOTED)


def _node_block():
    text = PROMOTED.read_text()
    start = text.index("[[toolchain]]", text.index('name = "node"') - 40)
    end = text.find("[[toolchain]]", start + 1)
    return text, text[start:end if end != -1 else len(text)]


def test_refuses_toolchain_promoted_at_two_versions(tmp_path):
    text, node = _node_block()
    promoted = tmp_path / "promoted.toml"
    promoted.write_text(text + "\n" + node.replace('version = "24.21.0"', 'version = "25.0.0"')
                        .replace('"linux-x86_64"', '"darwin-arm64"'))
    with pytest.raises(InitError, match="promoted at two versions"):
        _init(tmp_path, ["node-app"], promoted=promoted)


def test_refuses_two_pins_for_one_platform(tmp_path):
    text, node = _node_block()
    promoted = tmp_path / "promoted.toml"
    promoted.write_text(text + "\n" + node)
    with pytest.raises(InitError, match="two pins for linux-x86_64"):
        _init(tmp_path, ["node-app"], promoted=promoted)


def test_other_link_errors_are_reported(tmp_path, monkeypatch):
    def broken_link(src, dst):
        raise OSError(errno.EIO, "Input/output error")
    monkeypatch.setattr("qqsync.init.os.link", broken_link)
    with pytest.raises(InitError, match="cannot write: Input/output error"):
        _init(tmp_path, ["node-app"])
    assert not (tmp_path / "infra").exists()


def test_fallback_refuses_a_manifest_that_appeared(tmp_path, monkeypatch):
    def no_link(src, dst):
        Path(dst).write_text("theirs\n")
        raise PermissionError(errno.EPERM, "Operation not permitted")
    monkeypatch.setattr("qqsync.init.os.link", no_link)
    with pytest.raises(InitError, match="already exists"):
        _init(tmp_path, ["node-app"])
    assert (tmp_path / "infra" / "repo.toml").read_text() == "theirs\n"
    assert _leftovers(tmp_path) == []


def test_fallback_write_failure_removes_the_half_file(tmp_path, monkeypatch):
    def no_link(src, dst):
        raise PermissionError(errno.EPERM, "Operation not permitted")
    real_fdopen = os.fdopen
    calls = []

    def fdopen(fd, *args, **kwargs):
        calls.append(fd)
        f = real_fdopen(fd, *args, **kwargs)
        if len(calls) == 2:  # the fallback's write
            def full(_text):
                raise OSError(errno.ENOSPC, "No space left on device")
            f.write = full
        return f
    monkeypatch.setattr("qqsync.init.os.link", no_link)
    monkeypatch.setattr("qqsync.init.os.fdopen", fdopen)
    with pytest.raises(InitError, match="No space left"):
        _init(tmp_path, ["node-app"])
    assert not (tmp_path / "infra").exists()
