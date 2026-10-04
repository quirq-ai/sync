"""The "no other parser" check: only qqsync may parse infra/repo.toml.

"Every parser of DEPS is a dependency on DEPS." A second parser drifts from the schema and pins the
format in place, so any repo's presubmit runs `qqsync guard`, which fails when code reads the
manifest with anything but qqsync. Tools read it with `qqsync show` (JSON) or the qqsync library.

The rule is a language-blind heuristic over each tracked file's lines:
  - A line *names the manifest* when it has `repo.toml` (also split as "repo" ".toml" or globbed as
    repo.t*), qqsync's `DEFAULT_PATH`, or a constant that some file in the repo assigns from one of
    those (so a path kept in another module still counts).
  - A line *reads TOML* when it has an identifier containing "toml" (tomllib, pytoml, smol-toml,
    BurntSushi/toml, Toml.ToModel, TOML.parse), a generic data tool (yq, dasel, taplo) or a dynamic
    import, or imports the manifest file itself. A plain `import tomllib` line, a `.toml` filename and the bare word TOML in prose are
    not readers.
  - A read belongs to the nearest .toml path. A finding is a TOML-reading line that names the
    manifest, or has a line naming it within WINDOW lines and nearer than any line naming another
    .toml file (so reading your own pyproject.toml next to a docstring that mentions the manifest
    is fine).
Whole-line comments (#, //, /*, <!--), the `qqsync <command> ...` span of a line, and lines marked
`qqsync-guard: allow` are skipped. Markdown, plain text and TOML data files are skipped. A repo
exempts a reviewed path with --allow in its presubmit, or a reviewed line with the marker.
TODO(expert): reading the file as plain text (sed, grep, regexes, a hand-written parser) and
building the path or the library name at run time beyond the forms above are not caught.
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

MANIFEST_NAME = "repo.toml"
MANIFEST_PATH = re.compile(r"""repo(\.toml\b|["'`]\s*\+?\s*["'`]\.toml\b|\.t[*?\[])|\bDEFAULT_PATH\b""")
ASSIGNED = re.compile(r"""\b([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]*)?:?=(?!=)""")
TOML_WORD = re.compile(r"[A-Za-z0-9_]*toml[A-Za-z0-9_]*", re.IGNORECASE)
GENERIC_READER = re.compile(r"\b(yq|dasel|taplo|import_module|__import__)\b")
OTHER_TOML_FILE = re.compile(r"""(?<![\w.-])(?!repo\.toml)[\w.-]+\.toml\b""")
COMMENT = re.compile(r"^\s*(#|//|/\*|<!--)")
# A plain import names a library; it reads nothing by itself (the call that uses it does).
PLAIN_IMPORT = re.compile(r"^\s*(import\s+[\w., ]+|from\s+[\w.]+\s+import\s+[\w., ()*]+|use\s+[\w:{}, ]+;)\s*$")
MARKER = "qqsync-guard: allow"
# A bundler or loader importing the manifest file itself (import m from "../infra/repo.toml").
IMPORTS_MANIFEST = re.compile(r"""\b(import|require)\b.*repo\.toml\b""")
# `qqsync show infra/repo.toml` and friends read the manifest the sanctioned way: that span of a
# line does not count as naming the manifest (the rest of the line still does).
QQSYNC_INVOCATION = re.compile(r"""\bqqsync\s+(validate|show|pin|pins|fetch|verify|guard)\b[^"'`;&|)\n]*""")
MIN_CONSTANT = 3  # shorter assigned names (p, m) are too common to follow across files
WINDOW = 5
SKIP_SUFFIXES = frozenset({".md", ".markdown", ".txt", ".rst", ".toml"})
MAX_BYTES = 4 << 20


class GuardError(Exception):
    """The guard could not run, so it cannot say the repo is clean."""


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    text: str

    def __str__(self) -> str:
        return (f"{self.path}:{self.line}: parses {MANIFEST_NAME} outside qqsync: {self.text.strip()[:120]}\n"
                f"    read the manifest with `qqsync show` or the qqsync library instead")


def tracked_files(root: Path, notes: list[str]) -> list[str]:
    """Files git tracks under `root`; every file only when `root` is not in a git checkout."""
    try:
        probe = subprocess.run(["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
                               capture_output=True, text=True)
    except OSError:
        probe = None
    if probe is None or (probe.returncode != 0 and "not a git repository" in probe.stderr):
        notes.append(f"{root}: not a git checkout; scanning every file")
        return sorted(str(p.relative_to(root)) for p in root.rglob("*")
                      if p.is_file() and ".git" not in p.relative_to(root).parts)
    if probe.returncode != 0:
        raise GuardError(f"{root}: git cannot list the files: {probe.stderr.strip()}")
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True)
    if out.returncode != 0:
        raise GuardError(f"{root}: git cannot list the files: {os.fsdecode(out.stderr).strip()}")
    return sorted(os.fsdecode(p) for p in out.stdout.split(b"\0") if p)


def _reads_toml(line: str) -> bool:
    if PLAIN_IMPORT.match(line):
        return False
    if GENERIC_READER.search(line):
        return True
    for m in TOML_WORD.finditer(line):
        if m.group().lower() == "toml" and line[m.start() - 1:m.start()] == ".":
            continue  # a filename such as kinds.toml, not a library
        if m.group() == "TOML" and line[m.end():m.end() + 1] not in (".", ":"):
            continue  # the format's name in prose ("not valid TOML"), not TOML.parse or TOML::
        return True
    return False


def _read_text(root: Path, rel: str, notes: list[str]) -> str | None:
    path = root / rel
    try:
        if not path.is_file():
            return None
        with open(path, "rb") as f:
            if b"\0" in f.read(8192):
                return None  # binary
        if path.stat().st_size > MAX_BYTES:
            notes.append(f"{rel}: not scanned, larger than {MAX_BYTES >> 20} MiB")
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    return None if b"\0" in raw else raw.decode("utf-8", errors="replace")


def scan(root: str | Path, allow: list[str] = (), notes: list[str] | None = None) -> list[Finding]:
    """Every place under `root` that parses the manifest outside qqsync.

    Raises GuardError when `root` cannot be scanned. Files skipped for size are added to `notes`.
    """
    root = Path(root)
    notes = [] if notes is None else notes
    if not root.is_dir():
        raise GuardError(f"{root}: not a directory")
    files: dict[str, list[tuple[int, str]]] = {}
    for rel in tracked_files(root, notes):
        if Path(rel).suffix.lower() in SKIP_SUFFIXES or any(fnmatch.fnmatch(rel, g) for g in allow):
            continue
        text = _read_text(root, rel, notes)
        if text is not None:
            files[rel] = [(n, QQSYNC_INVOCATION.sub(" ", line)) for n, line in enumerate(text.splitlines(), 1)
                          if not COMMENT.match(line) and MARKER not in line]

    # Constants holding the manifest's path, wherever they are defined, name the manifest too.
    names = {m.group(1) for lines in files.values() for _, line in lines if MANIFEST_PATH.search(line)
             for m in [ASSIGNED.search(line[:MANIFEST_PATH.search(line).start()])] if m}
    names = {n for n in names if len(n) >= MIN_CONSTANT and n != "DEFAULT_PATH"}
    names_manifest = MANIFEST_PATH if not names else re.compile(
        MANIFEST_PATH.pattern + "|" + r"\b(" + "|".join(sorted(map(re.escape, names))) + r")\b")

    findings = []
    for rel, lines in files.items():
        manifest = [n for n, line in lines if names_manifest.search(line)]
        if not manifest:
            continue
        others = [n for n, line in lines if OTHER_TOML_FILE.search(line)]
        for n, line in lines:
            if IMPORTS_MANIFEST.search(line):
                findings.append(Finding(rel, n, line))
                break
            if not _reads_toml(line):
                continue
            to_manifest = min(abs(n - m) for m in manifest)
            to_other = min((abs(n - o) for o in others), default=WINDOW + 1)
            # The read belongs to the nearest .toml path: a line naming the manifest wins outright,
            # otherwise the manifest must be in the window and nearer than any other .toml file.
            if to_manifest == 0 or (to_manifest <= WINDOW and to_manifest < to_other):
                findings.append(Finding(rel, n, line))
                break
    return findings
