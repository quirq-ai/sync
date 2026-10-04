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
| `oci://REGISTRY/REPO` | `sha256:<layer>` | that layer's bytes |
| a git URL | `git:<commit>` | checked out by `qq`, then `verify` |

Registry packages must be public: `fetch` asks the registry for an anonymous token and never sends
credentials. Per-platform pins default to this machine's platform (`linux-x86_64`, `macos-arm64`, ...);
pass `--platform` to pick another. Commit pins are checked out by the `qq` CLI and verified with
`verify`.

## No other parser: `qqsync guard`

Any repo's presubmit can run `qqsync guard`. It fails when a tracked file reads TOML near a place
that names the manifest:

- **Names the manifest:** `repo.toml` (also split as `"infra/repo" ".toml"` or globbed as
  `repo.t*`), qqsync's `DEFAULT_PATH`, or a constant that any file in the repo assigns from one of
  those, so a path kept in another module still counts.
- **Reads TOML:** an identifier containing `toml` (tomllib, pytoml, smol-toml, `Toml`,
  `TOML.parse`), a data tool such as yq, a dynamic import (`import_module`), or importing the
  manifest file directly. A plain `import tomllib` line is not a read by itself.
- **Finding:** a reading line that names the manifest, or that has a line naming it within 5 lines
  and nearer than any line naming another `.toml` file. So a module that mentions the manifest in a
  docstring and reads its own `pyproject.toml` passes.

Whole-line comments, the `qqsync <command> ...` part of a line, `.toml` filenames and the word TOML
in prose don't count. Markdown, plain text and TOML data files are skipped. The fix is always the
same: read the manifest with `qqsync show` (JSON) or the qqsync library.

```sh
python -m pip install "qqsync @ git+https://github.com/quirq-ai/sync@<commit>"
qqsync guard .                          # exit 1 with file:line for each second parser, or PASS
qqsync guard . --allow 'vendor/*'       # skip reviewed paths (keep the list in your presubmit)
```

A reviewed line can be exempted with a `qqsync-guard: allow` comment on it. Both escapes belong in
code review, like any other presubmit exemption.

What it does not catch (`TODO(expert)` in `src/qqsync/guard.py`): a parser that reads the file as
plain text (sed, grep, regexes, a hand-written parser), and a path or library name built at run time
beyond the forms above. It is a heuristic that keeps honest code honest; a determined bypass needs
review to catch.

## Use it

Other qq repos depend on `sync` by pinned commit, never by copying code:

```sh
python -m pip install "qqsync @ git+https://github.com/quirq-ai/sync@<commit>"
qqsync --version
```

## Develop

```sh
python -m pip install -e ".[test]"
python -m pytest -q
```

Python 3.14 in CI (the org pin in infra-config); the library itself needs 3.11 or newer.

## v0 status

| Item | What | PR | State |
|---|---|---|---|
| V0-SYN-01 | Manifest schema `quirq-repo/1` | #2 | merged; xo-space and innernet fixtures validate in presubmit (onboarding lands the real manifests) |
| V0-SYN-02 | Parser and editor library | #3 | merged; done-when waits on V0-DEP-02 and V0-ROL-01 adopting it |
| V0-SYN-03 | Pin check | #4 | merged; done-when shown by a presubmit step |
| V0-SYN-04 | "No other parser" check | #5 | merged; done-when shown by a presubmit step |

Plan and every v0 item: `quirq-ai/infra-config`, `docs/plan.md` and `docs/v0.md`.
