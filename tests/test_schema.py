from pathlib import Path

import pytest

from qqsync.cli import main
from qqsync.errors import ManifestError
from qqsync.manifest import load, loads

FIXTURES = Path(__file__).parent / "fixtures"
PRODUCT_MANIFESTS = sorted(FIXTURES.glob("*.repo.toml"))
DIGEST = "sha256:" + "a" * 64
# A stand-in for infra-config kinds.toml; CI checks the fixtures against the real list (presubmit.yml).
KINDS = ["python-service", "pytest", "node-app", "static-docs", "container-image"]


def manifest(body: str) -> str:
    return 'schema = "quirq-repo/1"\n' + body


def problems(text: str, **kw) -> list[str]:
    try:
        loads(text, **kw)
    except ManifestError as e:
        return e.problems
    return []


@pytest.mark.parametrize("path", PRODUCT_MANIFESTS, ids=lambda p: p.name)
def test_product_manifests_validate(path):
    data = load(path, known_kinds=KINDS)
    assert data["schema"] == "quirq-repo/1"


def test_product_fixtures_present():
    assert {p.name for p in PRODUCT_MANIFESTS} >= {"xo-space.repo.toml", "innernet.repo.toml"}


def test_minimal_manifest():
    assert problems(manifest('targets = [{ name = "a", kind = "k" }]')) == []


@pytest.mark.parametrize("text, expected", [
    ('targets = [{ name = "a", kind = "k" }]', "schema: missing"),
    ('schema = "quirq-repo/9"\ntargets = [{ name = "a", kind = "k" }]', "not a schema this qqsync knows"),
    (manifest(""), "'targets' is a required property"),
    (manifest("targets = []"), "targets: [] should be non-empty"),
    (manifest('targets = [{ name = "a", kind = "k", sources = [] }]'), "Additional properties"),
    (manifest('targets = [{ name = "A", kind = "k" }]'), "targets[0].name"),
    (manifest('[toolchains.py]\nsource = "x"\ndigest = "sha256:abc"\n[[targets]]\nname = "a"\nkind = "k"'),
     "toolchains.py.digest"),
    (manifest('[toolchains.py]\nsource = "x"\n[[targets]]\nname = "a"\nkind = "k"'), "toolchains.py"),
    (manifest(f'[deps.d]\nsource = "x"\ndigest = "{DIGEST}"\nplatforms.linux-x86_64 = {{ source = "y", digest = "{DIGEST}" }}\n'
              '[[targets]]\nname = "a"\nkind = "k"'), "deps.d"),
    (manifest('[qq]\nversion = "1"\nsource = "x"\n[[targets]]\nname = "a"\nkind = "k"'), "'digest' is a dependency of 'source'"),
    (manifest('targets = [{ name = "a", kind = "k" }, { name = "a", kind = "k" }]'), "already used by targets[0]"),
    (manifest('targets = [{ name = "a", kind = "k", deps = ["b"] }]'), "'b' is not a target"),
    (manifest('targets = [{ name = "a", kind = "k", deps = ["a"] }]'), "depends on itself"),
    (manifest('targets = [{ name = "a", kind = "k", deps = ["b"] }, { name = "b", kind = "k", deps = ["a"] }]'),
     "dependency cycle a -> b -> a"),
    (manifest("targets = ["), "not valid TOML"),
    ('schema = 1\ntargets = [{ name = "a", kind = "k" }]', "must be a string"),
    (manifest('targets = [{ name = "a\\n", kind = "k" }]'), "whitespace"),
    (manifest('targets = [{ name = "a", kind = "k\\n" }]'), "whitespace"),
    (manifest(f'[deps.d]\nsource = "x\\n"\ndigest = "{DIGEST}"\n[[targets]]\nname = "a"\nkind = "k"'), "whitespace"),
    (manifest(f'[deps.d]\nsource = "x"\ndigest = "{DIGEST}\\n"\n[[targets]]\nname = "a"\nkind = "k"'), "whitespace"),
    (manifest('targets = [{ name = "a", kind = "k", srcs = ["/etc/passwd"] }]'), "targets[0].srcs[0]"),
    (manifest('targets = [{ name = "a", kind = "k", srcs = ["src/../../x"] }]'), "no .. segment"),
    (manifest('targets = [{ name = "a", kind = "k", outs = [".."] }]'), "targets[0].outs[0]"),
    (manifest('targets = [{ name = "a", kind = "k", outs = ["out\\n"] }]'), "line breaks"),
])
def test_rejected(text, expected):
    found = problems(text)
    assert found, "expected the manifest to be rejected"
    assert any(expected in p for p in found), found


def test_digest_forms():
    for digest in [DIGEST, "git:" + "f" * 40, "git:" + "f" * 64]:
        text = manifest(f'[deps.d]\nsource = "x"\ndigest = "{digest}"\n[[targets]]\nname = "a"\nkind = "k"')
        assert problems(text) == [], digest
    for digest in ["sha256:" + "A" * 64, "md5:" + "a" * 32, "git:" + "f" * 39, "latest"]:
        text = manifest(f'[deps.d]\nsource = "x"\ndigest = "{digest}"\n[[targets]]\nname = "a"\nkind = "k"')
        assert problems(text), digest


def test_unknown_kind_only_with_kind_list():
    text = manifest('targets = [{ name = "a", kind = "rocket" }]')
    assert problems(text) == []
    assert any("'rocket' is not a kind" in p for p in problems(text, known_kinds=KINDS))


def test_cli_validate(capsys, tmp_path):
    assert main(["validate", *map(str, PRODUCT_MANIFESTS), *[f"--kind={k}" for k in KINDS]]) == 0
    bad = tmp_path / "repo.toml"
    bad.write_text(manifest('targets = [{ name = "a", kind = "k", deps = ["b"] }]'))
    assert main(["validate", str(bad)]) == 1
    assert "'b' is not a target" in capsys.readouterr().err


def test_cli_missing_file(capsys, tmp_path):
    assert main(["validate", str(tmp_path / "nope.toml")]) == 1
    assert "cannot read" in capsys.readouterr().err


def test_relative_paths_allowed():
    text = manifest('targets = [{ name = "a", kind = "k", srcs = ["src/**", "..config", "a/..b/c", "*.ts"], outs = [".next/"] }]')
    assert problems(text) == []


def test_non_table_pin_reports_once():
    found = problems(manifest('[toolchains]\nx = 1\n[[targets]]\nname = "a"\nkind = "k"'))
    assert found == ["toolchains.x: 1 is not of type 'object'"]


def test_problems_ordered_by_index():
    targets = ", ".join(f'{{ name = "t{i}", kind = "{"K" if i in (2, 10) else "k"}" }}' for i in range(12))
    found = problems(manifest(f"targets = [{targets}]"))
    assert [p.split(":")[0] for p in found] == ["targets[2].kind", "targets[10].kind"]
