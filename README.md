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
| V0-SYN-01 | Manifest schema `quirq-repo/1` | | not started |
| V0-SYN-02 | Parser and editor library | | not started |
| V0-SYN-03 | Pin check | | not started |
| V0-SYN-04 | "No other parser" check | | not started |

Plan and every v0 item: `quirq-ai/infra-config`, `docs/plan.md` and `docs/v0.md`.
