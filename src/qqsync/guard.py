"""The "no other parser" check: only qqsync may parse infra/repo.toml.

"Every parser of DEPS is a dependency on DEPS." A second parser drifts from the schema and pins the
format in place, so any repo's presubmit runs `qqsync guard`, which fails when code reads the
manifest with anything but qqsync. Tools read it with `qqsync show` (JSON) or the qqsync library.

The rule is a language-blind heuristic over each tracked file's lines:
  - A line *names the manifest* when it has `repo.toml` (also split as "repo" ".toml" or globbed as
    repo.t*), qqsync's `DEFAULT_PATH`, or a name assigned one of those at the start of a line:
    UPPER_CASE constants count in every file (a path kept in another module), other names only in
    their own file. Argument lists and qqsync calls do not assign a path.
  - A line *reads TOML* when it has an identifier containing "toml" (tomllib, pytoml, smol-toml,
    BurntSushi/toml, Toml.ToModel, TOML.parse), a generic data tool (yq, dasel, taplo), a dynamic
    import, a name a plain import of a TOML library binds, or imports the manifest file itself. The
    plain import line, a `.toml` filename and the bare word TOML in prose are not readers.
  - A read belongs to the nearest .toml path. A finding is a TOML-reading line that names the
    manifest, or has a line naming it within WINDOW lines and nearer than any line naming another
    .toml file (so reading your own pyproject.toml next to a docstring that mentions the manifest
    is fine).
Whole-line comments, the `qqsync <command> ...` span of a line, and lines marked
`qqsync-guard: allow <reason>` are skipped (each exemption is reported as a note). Markdown, plain
text, TOML and JSON data files are skipped. A repo exempts a reviewed path with --allow in its
presubmit, or a reviewed line with the marker. Matching is linear in line length.
TODO(expert): not caught are plain-text parsers (sed, grep, regexes, a hand-written parser), paths
or library names built at run time, paths under dict/config keys or argparse defaults, and reads
farther than WINDOW lines from the path or in another file through a lower-case name.
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
# An assignment at the start of a line (`MANIFEST = ...`, `const m: string = ...`, `pub const P: &str
# = ...`). Anchored, with a bounded type, so long lines cost linear time.
ASSIGNED = re.compile(r"""^\s*(?:(?:export|pub|const|let|var|static|final|readonly)\s+)*"""
                      r"""([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=\n]{0,80})?:?=(?!=)\s*(.*)$""")
TOML = "toml"
GENERIC_READER = re.compile(r"\b(yq|dasel|taplo|import_module|__import__)\b")
OTHER_TOML_FILE = re.compile(r"""(?<![\w.-])(?!repo\.toml)[\w.-]+\.toml\b""")
# Whole-line comments: #, //, /*, a block comment's " * " continuation, <!--, and "-- ".
# (`*rest = ...` and `;stmt` are code, not comments.)
COMMENT = re.compile(r"^\s*(#|//|/\*|\*(\s|/|$)|<!--|--\s)")
# A plain import of a TOML library reads nothing by itself; the names it binds are readers.
PLAIN_IMPORT = re.compile(r"^\s*(import|from|use)\s[\w.,:{}()* ]{0,300};?\s*$")
BOUND = re.compile(r"""(?:\bimport\s+|\bas\s+|,\s*|\{\s*|::)(\w+)""")
MARKER = re.compile(r"qqsync-guard:\s*allow\b\W*(\w.*)?")
# A bundler or loader importing the manifest file itself (import m from "../infra/repo.toml").
IMPORTS_MANIFEST = re.compile(r"""\b(import|require)\b.*repo\.toml\b""")
# `qqsync show infra/repo.toml` and friends read the manifest the sanctioned way: that span of a
# line does not count as naming the manifest (the rest of the line still does).
QQSYNC_INVOCATION = re.compile(r"""\bqqsync\s+(validate|show|pin|pins|fetch|verify|guard)\b[^"'`;&|)\n]*""")
CONSTANT = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")  # followed across files: MANIFEST, REPO_TOML
WINDOW = 5
# Prose, TOML data, and JSON (package.json, editor settings: data, which cannot parse anything).
SKIP_SUFFIXES = frozenset({".md", ".markdown", ".txt", ".rst", ".toml", ".json", ".lock"})
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


def _toml_words(line: str):
    """Identifiers that contain "toml", found without backtracking (lines can be megabytes long)."""
    lower, start = line.lower(), 0
    while (i := lower.find(TOML, start)) >= 0:
        a, b = i, i + len(TOML)
        while a and (line[a - 1].isalnum() or line[a - 1] == "_"):
            a -= 1
        while b < len(line) and (line[b].isalnum() or line[b] == "_"):
            b += 1
        yield a, b, line[a:b]
        start = b


def _reads_toml(line: str) -> bool:
    if GENERIC_READER.search(line):
        return True
    for a, b, word in _toml_words(line):
        if word.lower() == TOML and line[a - 1:a] == ".":
            continue  # a filename such as kinds.toml, not a library
        if word == "TOML" and line[b:b + 1] not in (".", ":"):
            continue  # the format's name in prose ("not valid TOML"), not TOML.parse or TOML::
        return True
    return False


def _imported_readers(line: str) -> set[str]:
    """Names a plain import of a TOML library binds (`from tomllib import load`, `import tomli as T`,
    `use toml::from_str;`); empty when the line is not such an import."""
    if len(line) > 400 or not PLAIN_IMPORT.match(line) or not _reads_toml(line):
        return set()
    return {m.group(1) for m in BOUND.finditer(line)} - {"import", "as", "self", "super", "crate"}


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
        if text is None:
            continue
        kept = []
        for n, line in enumerate(text.splitlines(), 1):
            if COMMENT.match(line):
                continue
            if marker := MARKER.search(line):
                if marker.group(1):
                    notes.append(f"{rel}:{n}: exempted by qqsync-guard: {marker.group(1).strip()[:80]}")
                    continue
                notes.append(f"{rel}:{n}: qqsync-guard: allow needs a reason after it; not exempted")
            kept.append((n, QQSYNC_INVOCATION.sub(" ", line)))
        files[rel] = kept

    # Names assigned the manifest's path name it too: UPPER_CASE constants anywhere in the repo,
    # other names only in the file that assigns them. An argument list or a qqsync call is not a path.
    local: dict[str, set[str]] = {}
    shared: set[str] = set()
    for rel, lines in files.items():
        for _, line in lines:
            if len(line) > 2000 or not MANIFEST_PATH.search(line):
                continue
            m = ASSIGNED.match(line)
            if not m or m.group(1) == "DEFAULT_PATH" or "qqsync" in m.group(2) or m.group(2)[:1] in "[{":
                continue
            (shared if CONSTANT.match(m.group(1)) else local.setdefault(rel, set())).add(m.group(1))

    def names_pattern(names: set[str]) -> re.Pattern:
        if not names:
            return MANIFEST_PATH
        return re.compile(MANIFEST_PATH.pattern + r"|\b(" + "|".join(sorted(map(re.escape, names))) + r")\b")

    findings = []
    for rel, lines in files.items():
        names_manifest = names_pattern(shared | local.get(rel, set()))
        manifest = [n for n, line in lines if names_manifest.search(line)]
        if not manifest:
            continue
        others = [n for n, line in lines if OTHER_TOML_FILE.search(line)]
        readers = set().union(*(_imported_readers(line) for _, line in lines))
        bound = re.compile(r"\b(" + "|".join(sorted(map(re.escape, readers))) + r")\b") if readers else None
        for n, line in lines:
            if IMPORTS_MANIFEST.search(line):
                findings.append(Finding(rel, n, line))
                break
            if _imported_readers(line) or not (_reads_toml(line) or (bound and bound.search(line))):
                continue
            to_manifest = min(abs(n - m) for m in manifest)
            to_other = min((abs(n - o) for o in others), default=WINDOW + 1)
            # The read belongs to the nearest .toml path: a line naming the manifest wins outright,
            # otherwise the manifest must be in the window and nearer than any other .toml file.
            if to_manifest == 0 or (to_manifest <= WINDOW and to_manifest < to_other):
                findings.append(Finding(rel, n, line))
                break
    return findings
