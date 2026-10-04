import hashlib
import subprocess
from pathlib import Path

import pytest

from qqsync.cli import main
from qqsync.manifest import Manifest
from qqsync.pins import (Pin, PinError, PinMismatch, current_platform, fetch, file_digest, find_pin,
                         iter_pins, placeholders, verify_checkout, verify_file)

FIXTURES = Path(__file__).parent / "fixtures"


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@pytest.fixture
def artifact(tmp_path):
    path = tmp_path / "toolchain.tar"
    path.write_bytes(b"the real toolchain")
    return path


def manifest_for(tmp_path, source: str, digest: str) -> Path:
    path = tmp_path / "repo.toml"
    path.write_text('schema = "quirq-repo/1"\n\n[toolchains.tc]\nversion = "1"\n'
                    f'source = "{source}"\ndigest = "{digest}"\n\n[[targets]]\nname = "a"\nkind = "k"\n')
    return path


def test_fetch_matching_pin(tmp_path, artifact):
    pin = Pin("toolchains", "tc", None, artifact.as_uri(), sha(b"the real toolchain"))
    out = fetch(pin, tmp_path / "out" / "tc.tar")
    assert out.read_bytes() == b"the real toolchain"


def test_fetch_mismatch_fails_and_leaves_nothing(tmp_path, artifact):
    pin = Pin("toolchains", "tc", None, artifact.as_uri(), sha(b"what we pinned"))
    dest = tmp_path / "out" / "tc.tar"
    with pytest.raises(PinMismatch, match="refusing to use it"):
        fetch(pin, dest)
    assert list(dest.parent.iterdir()) == []


def test_fetch_mismatch_keeps_an_existing_file(tmp_path, artifact):
    dest = tmp_path / "tc.tar"
    dest.write_bytes(b"previous good copy")
    with pytest.raises(PinMismatch):
        fetch(Pin("toolchains", "tc", None, artifact.as_uri(), sha(b"other")), dest)
    assert dest.read_bytes() == b"previous good copy"


def test_fetch_refuses_other_schemes_and_commit_pins(tmp_path):
    with pytest.raises(PinError, match="https:// or file://"):
        fetch(Pin("deps", "d", None, "http://example.invalid/x", sha(b"x")), tmp_path / "x")
    with pytest.raises(PinError, match="only sha256 pins"):
        fetch(Pin("deps", "d", None, "https://example.invalid/x", "git:" + "a" * 40), tmp_path / "x")


def test_fetch_missing_source(tmp_path):
    with pytest.raises(PinError, match="failed"):
        fetch(Pin("deps", "d", None, (tmp_path / "nope").as_uri(), sha(b"x")), tmp_path / "x")
    assert not (tmp_path / "x").exists()


def test_verify_file(artifact):
    verify_file(Pin("deps", "d", None, "s", sha(b"the real toolchain")), artifact)
    with pytest.raises(PinMismatch):
        verify_file(Pin("deps", "d", None, "s", sha(b"x")), artifact)
    assert file_digest(artifact) == sha(b"the real toolchain")


def test_verify_checkout(tmp_path):
    repo = tmp_path / "dep"
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True)
    repo.mkdir()
    run("init", "-q")
    run("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "x")
    head = run("rev-parse", "HEAD").stdout.strip()
    verify_checkout(Pin("deps", "d", None, "s", f"git:{head}"), repo)
    with pytest.raises(PinMismatch, match="is at commit"):
        verify_checkout(Pin("deps", "d", None, "s", "git:" + "1" * len(head)), repo)
    with pytest.raises(PinError, match="cannot read the checkout"):
        verify_checkout(Pin("deps", "d", None, "s", f"git:{head}"), tmp_path)
    (repo / "f").write_text("tracked\n")
    run("add", "f")
    run("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "f")
    head = run("rev-parse", "HEAD").stdout.strip()
    pin = Pin("deps", "d", None, "s", f"git:{head}")
    (repo / "untracked").write_text("build output\n")
    verify_checkout(pin, repo)  # untracked files are fine
    (repo / "f").write_text("tampered\n")
    with pytest.raises(PinMismatch, match="tracked files were changed"):
        verify_checkout(pin, repo)
    (repo / "sub").mkdir()
    with pytest.raises(PinError, match="not the root of a checkout"):
        verify_checkout(pin, repo / "sub")


ZERO = "sha256:" + "0" * 64
PINS = ('schema = "quirq-repo/1"\n'
        '[toolchains.node]\n'
        f'platforms.linux-x86_64 = {{ source = "https://example.invalid/node-linux.tar", digest = "{sha(b"l")}" }}\n'
        f'platforms.macos-arm64 = {{ source = "https://example.invalid/node-macos.tar", digest = "{ZERO}" }}\n'
        '[toolchains.python]\n'
        f'source = "https://example.invalid/py.tar"\ndigest = "{sha(b"p")}"\n'
        '[[targets]]\nname = "a"\nkind = "k"\n')


def test_iter_and_find_pins():
    data = Manifest(PINS).data
    labels = [p.label for p in iter_pins(data)]
    assert labels == ["toolchains.node.platforms.linux-x86_64", "toolchains.node.platforms.macos-arm64",
                      "toolchains.python"]
    assert find_pin(data, "toolchains", "node", "macos-arm64").source.endswith("node-macos.tar")
    with pytest.raises(PinError, match="no pin for platform windows-x86_64"):
        find_pin(data, "toolchains", "node", "windows-x86_64")
    with pytest.raises(PinError, match="no such pin"):
        find_pin(data, "deps", "node")
    assert find_pin(data, "toolchains", "python").platform is None
    with pytest.raises(PinError, match="drop the platform"):
        find_pin(data, "toolchains", "python", "linux-x86_64")


def test_placeholders():
    assert placeholders(Manifest(PINS).data) == [
        "toolchains.node.platforms.macos-arm64: digest is a placeholder (all zeros); pin the real digest"]


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.repo.toml")), ids=lambda p: p.name)
def test_product_fixtures_pin_real_digests(path):
    assert placeholders(Manifest.read(path).data) == []


def test_current_platform_shape():
    os_name, arch = current_platform().split("-", 1)
    assert os_name and arch


# V0-SYN-03 done-when: a build whose fetched artifact does not match its pin fails.
def test_cli_fetch_mismatch_fails(tmp_path, artifact, capsys):
    good = manifest_for(tmp_path, artifact.as_uri(), sha(b"the real toolchain"))
    assert main(["fetch", "toolchains", "tc", "--dest", str(tmp_path / "ok.tar"), "--manifest", str(good)]) == 0
    assert main(["verify", "toolchains", "tc", str(tmp_path / "ok.tar"), "--manifest", str(good)]) == 0
    artifact.write_bytes(b"tampered upstream")
    assert main(["fetch", "toolchains", "tc", "--dest", str(tmp_path / "bad.tar"), "--manifest", str(good)]) == 1
    assert "refusing to use it" in capsys.readouterr().err
    assert not (tmp_path / "bad.tar").exists()
    assert main(["verify", "toolchains", "tc", str(artifact), "--manifest", str(good)]) == 1


def test_cli_pins(tmp_path, capsys):
    path = tmp_path / "repo.toml"
    path.write_text(PINS)
    assert main(["pins", str(path)]) == 0
    assert "toolchains.python\t" + sha(b"p") in capsys.readouterr().out
    assert main(["pins", "--strict", str(path)]) == 1
    assert "placeholder" in capsys.readouterr().err
    assert main(["pins", "--strict", str(FIXTURES / "xo-space.repo.toml")]) == 0
