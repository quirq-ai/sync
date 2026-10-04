import hashlib
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from qqsync.cli import main
from qqsync.manifest import Manifest
from qqsync.pins import (Pin, PinError, PinMismatch, current_platform, fetch, file_digest, find_pin,
                         iter_pins, oci_parts, placeholders, verify_checkout, verify_file)

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
    with pytest.raises(PinError, match="sources must be https://, oci:// or file://"):
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
    data = Manifest.read(path).data
    assert placeholders(data) == []
    for pin in iter_pins(data):  # toolchains pins: a manifest in the source, a layer as the digest
        _, _, manifest = oci_parts(pin.source)
        assert manifest and manifest != pin.digest


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


def test_fetched_file_has_normal_permissions(tmp_path, artifact):
    out = fetch(Pin("toolchains", "tc", None, artifact.as_uri(), sha(b"the real toolchain")), tmp_path / "o")
    assert out.stat().st_mode & 0o044 == 0o044  # group and others can read (umask 022 or 002)


def test_fetch_bad_destinations(tmp_path, artifact):
    pin = Pin("toolchains", "tc", None, artifact.as_uri(), sha(b"the real toolchain"))
    with pytest.raises(PinError, match="is a directory"):
        fetch(pin, tmp_path)
    with pytest.raises(PinError, match="failed"):
        fetch(pin, artifact / "under-a-file")
    with pytest.raises(PinError, match="failed"):
        fetch(Pin("deps", "d", None, "https://example.invalid:abc/x", sha(b"x")), tmp_path / "x")


def test_verify_missing_file(tmp_path):
    with pytest.raises(PinError, match="cannot read"):
        verify_file(Pin("deps", "d", None, "s", sha(b"x")), tmp_path / "nope")


def test_redirects_only_to_https():
    import urllib.error
    import urllib.request
    from qqsync.pins import _HttpsOnlyRedirects
    req = urllib.request.Request("https://example.invalid/a")
    with pytest.raises(urllib.error.URLError, match="only https"):
        _HttpsOnlyRedirects().redirect_request(req, None, 302, "Found", {}, "http://example.invalid/b")
    assert _HttpsOnlyRedirects().redirect_request(req, None, 302, "Found", {}, "https://example.invalid/b")


# --- oci:// pins -----------------------------------------------------------------------------

class Registry(BaseHTTPRequestHandler):
    """A minimal OCI registry: manifests and blobs by digest, optionally behind an anonymous token."""
    manifests: dict = {}
    blobs: dict = {}
    want_token = False
    realm = None  # default: this server's /token
    token_body = json.dumps({"token": "anon"}).encode()

    def do_GET(self):
        if self.path.startswith("/token"):
            return self._send(200, self.token_body)
        if self.want_token and self.headers.get("Authorization") != "Bearer anon":
            self.send_response(401)
            realm = self.realm or f"http://{self.headers['Host']}/token"
            self.send_header("WWW-Authenticate", f'Bearer realm="{realm}",service="reg",scope="pull"')
            self.end_headers()
            return
        kind, _, digest = self.path.rpartition("/")
        store = self.manifests if kind.endswith("/manifests") else self.blobs
        if digest in store:
            return self._send(200, store[digest])
        self._send(404, b"not found")

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def registry(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Registry)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("QQ_OCI_SCHEME", "http")
    Registry.want_token, Registry.realm = False, None
    Registry.token_body = json.dumps({"token": "anon"}).encode()
    layer, other = b"toolchain layer bytes", b"some other artifact"
    manifest = json.dumps({"schemaVersion": 2, "layers": [{"digest": sha(layer)}]}).encode()
    Registry.manifests = {sha(manifest): manifest}
    Registry.blobs = {sha(layer): layer, sha(other): other}
    yield {"host": f"127.0.0.1:{server.server_address[1]}", "layer": layer, "other": other,
           "manifest": sha(manifest)}
    server.shutdown()


def oci_pin(reg, digest, with_manifest=True):
    source = f"oci://{reg['host']}/quirq-ai/toolchains/tc" + (f"@{reg['manifest']}" if with_manifest else "")
    return Pin("toolchains", "tc", "linux-x86_64", source, digest)


def test_oci_fetch_checks_manifest_and_layer(tmp_path, registry):
    out = fetch(oci_pin(registry, sha(registry["layer"])), tmp_path / "tc.tar")
    assert out.read_bytes() == registry["layer"]
    Registry.want_token = True  # an anonymous bearer token is fetched and used
    assert fetch(oci_pin(registry, sha(registry["layer"])), tmp_path / "again.tar").exists()


def test_oci_layer_not_in_manifest_is_refused(tmp_path, registry):
    with pytest.raises(PinMismatch, match="has no layer"):
        fetch(oci_pin(registry, sha(registry["other"])), tmp_path / "x")
    assert list(tmp_path.iterdir()) == []
    # Without a manifest in the source, the layer is only checked against its own digest.
    assert fetch(oci_pin(registry, sha(registry["other"]), with_manifest=False), tmp_path / "y").exists()


def test_oci_tampered_manifest_and_blob(tmp_path, registry):
    digest = registry["manifest"]
    Registry.manifests[digest] = b'{"layers": []}'
    with pytest.raises(PinMismatch, match="does not hash to"):
        fetch(oci_pin(registry, sha(registry["layer"])), tmp_path / "x")
    Registry.manifests[digest] = json.dumps({"schemaVersion": 2, "layers": [{"digest": sha(registry["layer"])}]}).encode()
    assert sha(Registry.manifests[digest]) == digest
    Registry.blobs[sha(registry["layer"])] = b"tampered"
    with pytest.raises(PinMismatch, match="refusing to use it"):
        fetch(oci_pin(registry, sha(registry["layer"])), tmp_path / "x")
    assert not (tmp_path / "x").exists()


def test_oci_errors(tmp_path, registry):
    with pytest.raises(PinError, match="HTTP 404"):
        fetch(oci_pin(registry, sha(b"missing"), with_manifest=False), tmp_path / "x")
    with pytest.raises(PinError, match="is not oci://"):
        fetch(Pin("toolchains", "tc", None, "oci://no-repository", sha(b"x")), tmp_path / "x")
    assert list(tmp_path.iterdir()) == []


def test_oci_token_realm_must_be_https(tmp_path, registry):
    secret = tmp_path / "creds.json"
    secret.write_text('{"token": "SECRET-LOCAL"}')
    Registry.want_token = True
    for realm in (f"{secret.as_uri()}#", secret.as_uri(), "http://internal.example/token", "ftp://x/token",
                  f"http://{registry['host']}/token#"):
        Registry.realm = realm
        with pytest.raises(PinError, match="refusing token realm"):
            fetch(oci_pin(registry, sha(registry["layer"])), tmp_path / "x")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["creds.json"]


@pytest.mark.parametrize("realm", ["https://127.0.0.1:8443/token", "https://169.254.169.254/token",
                                   "https://[::ffff:10.0.0.1]/t", "https://localhost./t", "https://[::1]/t"])
def test_oci_token_realm_not_internal(monkeypatch, realm):
    from qqsync.pins import _anonymous_token
    monkeypatch.delenv("QQ_OCI_SCHEME", raising=False)
    with pytest.raises(PinError, match="internal address"):
        _anonymous_token(f'Bearer realm="{realm}",service="reg"')


def test_oci_bad_token_body(tmp_path, registry):
    Registry.want_token = True
    for body in (b'["anon"]', b'"anon"', b'{"token": 7}', b'{}', b'not json'):
        Registry.token_body = body
        with pytest.raises(PinError):
            fetch(oci_pin(registry, sha(registry["layer"])), tmp_path / "x")


def test_oci_index_and_odd_manifests(tmp_path, registry):
    for doc in ({"mediaType": "application/vnd.oci.image.index.v1+json", "manifests": []},
                {"layers": "nope"}, ["not", "a", "manifest"]):
        raw = json.dumps(doc).encode()
        Registry.manifests[sha(raw)] = raw
        pin = Pin("toolchains", "tc", None, f"oci://{registry['host']}/quirq-ai/tc@{sha(raw)}",
                  sha(registry["layer"]))
        with pytest.raises(PinError, match="image index" if "manifests" in doc else "not an image manifest"):
            fetch(pin, tmp_path / "x")


@pytest.mark.parametrize("source", ["oci://r.example/a/../../x", "oci://r.example/r?x#", "oci://r.example/r b",
                                    "oci://r.example/Upper", "oci://r.example/r/", "oci://r .example/r"])
def test_oci_source_names_are_checked(tmp_path, source):
    with pytest.raises(PinError, match="is not oci://"):
        fetch(Pin("toolchains", "tc", None, source, sha(b"x")), tmp_path / "x")


def test_failed_fetches_do_not_leak_descriptors(tmp_path, registry):
    fds = Path("/proc/self/fd")
    if not fds.exists():
        pytest.skip("needs /proc")
    before = len(list(fds.iterdir()))
    for _ in range(20):
        with pytest.raises(PinError):
            fetch(oci_pin(registry, sha(b"missing"), with_manifest=False), tmp_path / "x")
    assert len(list(fds.iterdir())) <= before + 2


def test_oci_plain_http_only_for_this_machine(monkeypatch, tmp_path):
    monkeypatch.setenv("QQ_OCI_SCHEME", "http")
    seen = []
    monkeypatch.setattr("qqsync.pins._oci_open", lambda url, *a: seen.append(url) or (_ for _ in ()).throw(PinError("stop")))
    with pytest.raises(PinError):
        fetch(Pin("toolchains", "tc", None, "oci://registry.example/r", sha(b"x")), tmp_path / "x")
    assert seen == ["https://registry.example/v2/r/blobs/" + sha(b"x")]


def test_cli_fetch_oci_fixture_shape(tmp_path, registry):
    path = tmp_path / "repo.toml"
    path.write_text('schema = "quirq-repo/1"\n[toolchains.tc]\n'
                    f'platforms.linux-x86_64 = {{ source = "oci://{registry["host"]}/t/tc@{registry["manifest"]}", '
                    f'digest = "{sha(registry["layer"])}" }}\n[[targets]]\nname = "a"\nkind = "k"\n')
    assert main(["fetch", "toolchains", "tc", "--platform", "linux-x86_64", "--dest", str(tmp_path / "tc"),
                 "--manifest", str(path)]) == 0
    assert (tmp_path / "tc").read_bytes() == registry["layer"]
