"""The "no other parser" check: only qqsync may parse infra/repo.toml.

"Every parser of DEPS is a dependency on DEPS." A second parser drifts from the schema and pins the
format in place, so any repo's presubmit runs `qqsync guard`, which fails when a file both names the
manifest and reads TOML some other way. Tools read the manifest with `qqsync show` (JSON) or the
qqsync library instead.

The rule is deliberately simple and language-blind. Comment lines and `qqsync <command>` lines are
ignored. In what is left, a file is a second parser when it
  - names the manifest (`repo.toml`, or qqsync's `DEFAULT_PATH`), and
  - names anything TOML: an identifier containing "toml" (tomllib, pytoml, smol-toml, BurntSushi/toml,
    Toml.ToModel, TOML.parse), but not a `.toml` filename or the bare word TOML; or reads the manifest with a generic
    structured-data tool on the same line, or imports it directly.
Markdown, plain text and TOML data files are skipped.
TODO(expert): this still misses a parser that reads the file as plain text (sed, grep, regexes).
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

MANIFEST_NAME = "repo.toml"
MANIFEST = re.compile(r"repo\.toml|\bDEFAULT_PATH\b")
TOML_WORD = re.compile(r"[A-Za-z0-9_]*toml[A-Za-z0-9_]*", re.IGNORECASE)
# Reading the manifest itself with a generic data tool, or importing it as a module.
DIRECT_READ = re.compile(r"\b(yq|dasel|taplo|import|require)\b.*repo\.toml|repo\.toml.*\|\s*(yq|dasel|taplo)\b")
QQSYNC_COMMAND = re.compile(r"(^|[\s\"'`;&|(=])qqsync\s+(validate|show|pin|pins|fetch|verify|guard)\b")
COMMENT = re.compile(r"^\s*(#|//|--|;|\*|/\*|<!--|rem\s)", re.IGNORECASE)
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


def _is_toml_reader(line: str) -> bool:
    if DIRECT_READ.search(line):
        return True
    for m in TOML_WORD.finditer(line):
        if m.group().lower() == "toml" and line[m.start() - 1:m.start()] == ".":
            continue  # a filename such as kinds.toml, not a library
        if m.group() == "TOML" and line[m.end():m.end() + 1] not in (".", ":"):
            continue  # the format's name in prose ("not valid TOML"), not TOML.parse or TOML::
        return True
    return False


def scan(root: str | Path, allow: list[str] = (), notes: list[str] | None = None) -> list[Finding]:
    """Every place under `root` that parses the manifest outside qqsync.

    Raises GuardError when `root` cannot be scanned. Files skipped for size are added to `notes`.
    """
    root = Path(root)
    notes = [] if notes is None else notes
    if not root.is_dir():
        raise GuardError(f"{root}: not a directory")
    findings = []
    for rel in tracked_files(root, notes):
        if Path(rel).suffix.lower() in SKIP_SUFFIXES or any(fnmatch.fnmatch(rel, g) for g in allow):
            continue
        path = root / rel
        try:
            if not path.is_file():
                continue
            with open(path, "rb") as f:
                if b"\0" in f.read(8192):
                    continue  # binary
            if path.stat().st_size > MAX_BYTES:
                notes.append(f"{rel}: not scanned, larger than {MAX_BYTES >> 20} MiB")
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\0" in raw:
            continue  # binary
        lines = [(n, line) for n, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1)
                 if not COMMENT.match(line) and not QQSYNC_COMMAND.search(line)]
        if not any(MANIFEST.search(line) for _, line in lines):
            continue
        hit = next(((n, line) for n, line in lines if _is_toml_reader(line)), None)
        if hit:
            findings.append(Finding(rel, *hit))
    return findings
