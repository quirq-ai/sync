# Agent guide

Read `README.md` first.

- Every change is a pull request against `main`, titled with its item id (for example `V0-SYN-02`).
  It lands only with the `presubmit` check green.
- This is a core repo: no file names a language or a build tool, and target kinds stay opaque strings.
- The manifest has one parser, `qqsync`. Never add another, here or anywhere else.
- A schema change is a policy change. `quirq-repo/1` only grows in backward compatible ways;
  anything else is a new schema version.
- `.github/CODEOWNERS` names suraj (`@sharmasuraj0123`) as owner of the policy and trust paths;
  owner names are his call, so never change them. Leave any other `owners` list empty.
- Mark a decision you cannot make with a one-line `TODO(suraj):` or `TODO(expert):`.
- Never commit secrets, tokens or internal hostnames. This repo is public.
