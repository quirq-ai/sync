# sync

`sync` owns the quirq infra (`qq`) repo manifest, `infra/repo.toml`: its versioned schema, the only
library that reads and edits it (`qqsync`), and the checks that keep pins honest.

Every repo that quirq infra builds keeps one manifest. It lists the repo's pinned toolchains and
dependencies and its typed targets. A target's `kind` is an opaque string; what a kind means lives in
`quirq-ai/recipes` and the list of kinds in `quirq-ai/infra-config` (`config/kinds.toml`). This repo
names no language and no tool.

## Chromium counterpart

`gclient` and `DEPS`. Like `DEPS`, the manifest is parsed and never executed, and "every parser of DEPS
is a dependency on DEPS": so there is exactly one parser, this one, and a presubmit that rejects any
other.

## The manifest: `quirq-repo/1`

```toml
schema = "quirq-repo/1"

[qq]                       # the qq CLI version this repo runs; pinned by version until
version = "0.1.0"          # depot publishes releases, then also by source and digest

[toolchains.python]        # names match the toolchains in infra-config kinds.toml
version = "3.14.8"         # a label; the digest is what is verified
source = "https://..."     # opaque to sync; the fetcher interprets the scheme
digest = "sha256:<64 hex>" # or git:<commit>

[toolchains.node]          # or one source and digest per platform
platforms.linux-x86_64 = { source = "https://...", digest = "sha256:<64 hex>" }

[deps.something]           # anything else fetched before a build, pinned the same way
source = "https://..."
digest = "git:<40 hex>"

[[targets]]
name = "server"
kind = "python-service"    # opaque; recipes gives it meaning
srcs = ["server.py", "services/**"]

[[targets]]
name = "tests"
kind = "pytest"
deps = ["server"]          # other targets in this manifest
params = { shards = "auto" } # kind-specific, passed to the adapter as is
```

Paths in `srcs` and `outs` are relative to the repo root (no leading `/`, no `..`). Targets may
also declare `outs` (outputs) and `cacheable = false` (runs, never reused). The full
schema is [`src/qqsync/schema/quirq-repo-1.schema.json`](src/qqsync/schema/quirq-repo-1.schema.json).
Unknown keys are errors. On top of the schema, target names must be unique, target `deps` must name
targets in the same manifest, and the target graph must have no cycle. `quirq-repo/1` only grows in
backward compatible ways; anything else is `quirq-repo/2`.

```sh
qqsync validate infra/repo.toml                 # exit 1 and every problem, or PASS
qqsync validate infra/repo.toml --kind pytest   # also require each kind to be one you list
```

## Read and edit it: the only parser

`qqsync` is the one library that reads or edits a manifest. Tools in other languages call
`qqsync show` and read JSON; they never parse the TOML themselves.

```python
from qqsync.manifest import Manifest, load

data = load("infra/repo.toml")             # validated plain data, or ManifestError with every problem

m = Manifest.read("infra/repo.toml")
m.set_pin("toolchains", "python", digest="sha256:<64 hex>", version="3.15.0")
m.set_pin("toolchains", "node", platform="linux-x86_64", digest="sha256:<64 hex>")
m.set_qq_version("0.2.0")
m.add_target({"name": "lint", "kind": "pytest"}) # also set_target, remove_target
m.write()                                   # atomic; untouched lines stay byte for byte
```

```sh
qqsync show infra/repo.toml                                  # the manifest as JSON
qqsync pin toolchains python --digest sha256:<64 hex> --version 3.15.0
```

Reading a manifest and writing it back unchanged gives the same bytes, CRLF and comments included.
An edit changes only the lines it must, and `write` follows symlinks and keeps the file mode. Before
editing, the file is read by both the standard TOML parser and the editor; if they disagree, or the
editor cannot reproduce the file exactly, the edit is refused (reading still works). Every edit is validated, and an edit that would make the manifest
invalid is undone and raises `ManifestError`.

## Pins: checked on every fetch

Every toolchain and dependency is pinned by digest (the schema requires it). `qqsync.pins` checks
fetched content against the pin: `fetch` downloads to a temporary file, hashes while it downloads,
and moves the file into place only when the digest matches. A mismatch raises `PinMismatch`, leaves
nothing at the destination (an older good copy stays), and the build stops.

```sh
qqsync pins [--strict] infra/repo.toml                     # list pins; --strict rejects all-zero placeholders
qqsync fetch toolchains python --dest .qq/python.tar.zst   # https://, oci:// or file:// sources, sha256 pins
qqsync verify toolchains python .qq/python.tar.zst         # a file against a sha256 pin
qqsync verify deps recipes .qq/recipes                     # a checkout: pinned commit, tracked files unchanged
```

Redirects are followed only to `https://`.

| Source | Digest | Fetched and checked as |
|---|---|---|
| `https://…`, `file://…` | `sha256:` | the bytes at that URL |
| `oci://REGISTRY/REPO@sha256:<manifest>` | `sha256:<layer>` | the manifest must hash to its digest and list the layer; the layer's bytes must hash to the pin (how quirq-ai/toolchains publishes) |
| a git URL | `git:<commit>` | checked out by `qq`, then `verify` |

An `oci://` pin always names the manifest and pins a different value, the layer. `validate`,
`pins --strict` and `fetch` reject an `oci://` source without `@sha256:<manifest>`, and a digest
equal to the manifest digest, since either leaves the fetched bytes unpinned.

Registry packages must be public: `fetch` asks the registry for an anonymous token and never sends
credentials. Per-platform pins default to this machine's platform (`linux-x86_64`, `macos-arm64`, ...);
pass `--platform` to pick another. Commit pins are checked out by the `qq` CLI and verified with
`verify`.

## No other parser: `qqsync guard`

Any repo's presubmit can run `qqsync guard`. It fails when a tracked file reads TOML near a place
that names the manifest:

- **Names the manifest:** `repo.toml` (also split as `"infra/repo" ".toml"` or globbed as
  `repo.t*`), qqsync's `DEFAULT_PATH`, or a name assigned one of those: an UPPER_CASE constant
  (`MANIFEST`, `_MANIFEST`) anywhere in the repo, so a path kept in another module still counts,
  unless some file assigns the same name something else; any name assigned at the left margin, in
  its whole file; an indented name from its assignment until it is reassigned or a function starts
  at its indent, and as `self.x`/`this.x`/`cls.x` in its file. A function whose signature names the
  manifest (a default argument) names it throughout its body.
- **Reads TOML:** an identifier containing `toml` (tomllib, pytoml, smol-toml, `Toml`,
  `TOML.parse`), a data tool (yq, dasel, taplo) or a dynamic import on the line that names the
  manifest, or importing the manifest file directly. An import of a TOML library is not a read by itself, but
  the names it binds are: Python `from tomllib import load` (also across lines in parentheses) and
  `import tomli as T`, Rust `use toml::from_str;`, JS `import { parse } from "smol-toml"` and
  `const { parse } = require("@iarna/toml")`, Go `t "github.com/pelletier/go-toml/v2"` (in a
  `.go` import), and
  `lib = importlib.import_module(...)`.
- **Finding:** a reading line that names the manifest, or that has a line naming it within 5 lines
  and nearer than any line naming another `.toml` file. So a module that mentions the manifest in a
  docstring and reads its own `pyproject.toml` passes.

Whole-line comments (`#`, `//`, `/*`, ` * `, `<!--`, `-- `), the `qqsync <command> ...` part of a
line (or a `["qqsync", "show", ...]` argument list), `.toml` filenames and the word TOML in prose
don't count. Markdown, plain text, TOML and JSON data files are skipped. Matching is linear in the
size of each file. The fix is always the
same: read the manifest with `qqsync show` (JSON) or the qqsync library.

```sh
python -m pip install "qqsync @ git+https://github.com/quirq-ai/sync@<commit>"
qqsync guard .                          # exit 1 with file:line for each second parser, or PASS
qqsync guard . --allow 'vendor/*'       # skip reviewed paths (keep the list in your presubmit)
```

A reviewed line can be exempted with a `qqsync-guard: allow <reason>` comment on it; the guard
prints every exempted line with its reason, and ignores a marker with no reason. Both escapes
belong in code review, like any other presubmit exemption.

What it does not catch (`TODO(expert)` in `src/qqsync/guard.py`): a parser that reads the file as
plain text (sed, grep, regexes, a hand-written parser); a path or library name built at run time
(`f"infra/{name}.toml"`, `glob("infra/*.toml")`); a path kept under a dict or config key or as an
argparse default (`args.manifest`); a path passed to another function and read there; a read more
than 5 lines from the path that does not use a name assigned it; and import forms beyond those
listed. A pre-commit `check-toml` hook within 5 lines of the manifest's name is flagged and needs
the marker. It is a heuristic that keeps honest code honest; a determined bypass needs
review to catch.

## Use it

Other qq repos depend on `sync` by pinned commit, never by copying code:

```sh
python -m pip install "qqsync @ git+https://github.com/quirq-ai/sync@<commit>"
qqsync --version
```

To install `qqsync` next to `qq` on your own machine, follow the qq guide:
https://docs.quirq.dev/docs/qq.

## Start a new repo: `qqsync init`

```sh
qqsync init . --kind python-service --kind pytest \
  --kinds infra-config/config/kinds.toml \
  --promoted toolchains/promoted.toml \
  --qq-version 0.1.0
qqsync validate infra/repo.toml --kind python-service --kind pytest
```

It writes `infra/repo.toml` with one target per kind (named after the kind), the toolchain pins
those kinds need, and `[qq] version`. It never replaces an existing manifest; use `qqsync pin`
to change one. The same inputs always give the same bytes.

sync names no language or toolchain, so the data comes in as files:
- `--kinds`: infra-config's `config/kinds.toml`, which says which toolchain each kind needs;
- `--promoted`: quirq-ai/toolchains' `promoted.toml`, the pins it promoted;
- `--qq-version`: the qq version the repo pins.

The new targets have no `srcs`, `outs` or `deps` yet; add them by hand.

## Develop

```sh
python -m pip install -e ".[test]"
python -m pytest -q
```

Python 3.14 in CI (the org pin in infra-config); the library itself needs 3.11 or newer.

## v0 status

| Item | What | PR | State |
|---|---|---|---|
| V0-SYN-01 | Manifest schema `quirq-repo/1` | #2 | merged; xo-space and innernet fixtures validate in presubmit. Both repos now carry their own `infra/repo.toml`, which presubmit does not read |
| V0-SYN-02 | Parser and editor library | #3 | merged; depot (`qq sync`, version pins) and rollers (`qqroll`) read and edit manifests through it, and `tests/test_manifest.py` checks the byte-identical round trip |
| V0-SYN-03 | Pin check | #4 | merged; done-when shown by a presubmit step |
| V0-SYN-04 | "No other parser" check | #5 | merged; done-when shown by a presubmit step |

Plan and every v0 item: `quirq-ai/infra-config`, `docs/plan.md` and `docs/v0.md`.

## Licence

Apache-2.0; see [LICENSE](LICENSE).
