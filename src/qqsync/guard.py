"""The "no other parser" check: only qqsync may parse infra/repo.toml.

"Every parser of DEPS is a dependency on DEPS." A second parser drifts from the schema and pins the
format in place, so any repo's presubmit runs `qqsync guard`, which fails when code reads the
manifest with anything but qqsync. Tools read it with `qqsync show` (JSON) or the qqsync library.

The rule is a language-blind heuristic over each tracked file's lines:
  - A line *names the manifest* when it has `repo.toml` (also split as "repo" ".toml" or globbed as
    repo.t*), qqsync's `DEFAULT_PATH`, or a name assigned one of those at the start of a line:
    UPPER_CASE constants in every file that does not assign the name something else, `self.x` /
    `this.x` attributes in their own file, other names from the assignment until the next
    reassignment or function. Argument lists and qqsync calls do not assign a path.
  - A line *reads TOML* when it has an identifier containing "toml" (tomllib, pytoml, smol-toml,
    BurntSushi/toml, Toml.ToModel, TOML.parse), a generic data tool (yq, dasel, taplo), a name an
    import of a TOML library binds (Python, Rust, JS, Go forms; see _import_bindings), a dynamic
    import on a line naming the manifest, or imports the manifest file itself. The import line,
    a `.toml` filename and the bare word TOML in prose are not readers.
  - A read belongs to the nearest .toml path. A finding is a TOML-reading line that names the
    manifest, or has a line naming it within WINDOW lines and nearer than any line naming another
    .toml file (so reading your own pyproject.toml next to a docstring that mentions the manifest
    is fine).
Whole-line comments, the `qqsync <command> ...` span of a line, and lines marked
`qqsync-guard: allow <reason>` are skipped (each exemption is reported as a note). Markdown, plain
text, TOML and JSON data files are skipped. A repo exempts a reviewed path with --allow in its
presubmit, or a reviewed line with the marker. Every pattern is anchored, bounded or possessive,
so a scan is linear in file size.
TODO(expert): not caught are plain-text parsers (sed, grep, regexes, a hand-written parser), paths
or library names built at run time, paths under dict/config keys or argparse defaults, a path
passed to another function, and reads farther than WINDOW lines from the path without a name.
"""
from __future__ import annotations

import bisect
import fnmatch
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

MANIFEST_NAME = "repo.toml"
# Possessive quantifiers keep the split form ("repo" + ".toml") linear on long runs of spaces.
MANIFEST_PATH = re.compile(r"""repo(\.toml\b|["'`]\s*+\+?\s*+["'`]\.toml\b|\.t[*?\[])|\bDEFAULT_PATH\b""")
# An assignment at the start of a line (`MANIFEST = ...`, `const m: string = ...`, `pub const P: &str
# = ...`, `self.path = ...`). Anchored, with a bounded type; only run on lines up to MAX_LINE.
ASSIGNED = re.compile(r"""^\s*(?:(?:export|pub|const|let|var|static|final|readonly)\s+)*"""
                      r"""((?:self|this|cls)\.)?([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=\n]{0,80})?:?=(?!=)\s*(.*)$""")
FUNCTION = re.compile(r"^\s*(?:(?:async|pub|export|static)\s+)*(?:def|function|fn|func|sub)\b")
TOML = re.compile("toml", re.IGNORECASE)
GENERIC_READER = re.compile(r"\b(yq|dasel|taplo)\b")
DYNAMIC_IMPORT = re.compile(r"\b(import_module|__import__)\s*\(")
OTHER_TOML_FILE = re.compile(r"""(?<![\w.-])(?!repo\.toml)[\w.-]+\.toml\b""")
# Whole-line comments: #, //, /*, a block comment's " * " continuation, <!--, and "-- ".
# (`*rest = ...` and `;stmt` are code, not comments.)
COMMENT = re.compile(r"^\s*(#|//|/\*|\*(\s|/|$)|<!--|--\s)")
# Imports that bind names. A plain import of a TOML library reads nothing by itself; the names it
# binds are readers. Only lines up to MAX_IMPORT characters are imports.
PY_FROM = re.compile(r"^\s*from\s+([\w.]+)\s+import\s+\(?([\w\s,]*?)\)?\s*$")
OPEN_PAREN_IMPORT = re.compile(r"^\s*from\s+[\w.]+\s+import\s*\(\s*$")
PY_IMPORT = re.compile(r"^\s*import\s+([\w.]+(?:\s+as\s+\w+)?(?:\s*,\s*[\w.]+(?:\s+as\s+\w+)?)*)\s*;?\s*$")
RUST_USE = re.compile(r"^\s*(?:pub\s+)?use\s+([\w:]+?)(?:::\{([\w\s,:]*)\})?(?:\s+as\s+(\w+))?\s*;\s*$")
JS_IMPORT = re.compile(r"""^\s*import\s+([\w\s,{}*$]+?)\s+from\s+["']([^"']+)["']\s*;?\s*$""")
JS_REQUIRE = re.compile(r"""^\s*(?:const|let|var)\s+(\{[\w\s,:$]*\}|[\w$]+)\s*=\s*require\(\s*["']([^"']+)["']\s*\)""")
GO_IMPORT = re.compile(r"""^\s*(?:import\s+)?(\w+\s+)?"([^"\s]+)"\s*\)?\s*$""")
DYNAMIC_BIND = re.compile(r"^\s*(?:(?:const|let|var)\s+)?(\w+)\s*=.*\b(?:import_module|__import__)\s*\(")
MARKER = re.compile(r"qqsync-guard:\s*allow\b\W*(\w.*)?")
IMPORT_WORD = re.compile(r"\b(import|require)\b")
# `qqsync show infra/repo.toml` and ["qqsync", "show", ...] read the manifest the sanctioned way:
# that span of a line does not count as naming the manifest (the rest of the line still does).
QQSYNC_INVOCATION = re.compile(r"""\bqqsync\s+(validate|show|pin|pins|fetch|verify|guard)\b[^"'`;&|)\n]*"""
                               r"""|\[\s*["']qqsync["']\s*,\s*["'](validate|show|pin|pins|fetch|verify|guard)["']"""
                               r"""[^\]\n]{0,500}\]""")
CONSTANT = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")  # followed across files: MANIFEST, REPO_TOML
WINDOW = 5
MAX_LINE = 2000
MAX_IMPORT = 400
# Prose, TOML data, and JSON (package.json, editor settings: data, which cannot parse anything).
SKIP_SUFFIXES = frozenset({".md", ".markdown", ".txt", ".rst", ".toml", ".json", ".lock", ".code-workspace"})
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
    start = 0
    for m in TOML.finditer(line):
        if m.start() < start:
            continue
        a, b = m.start(), m.end()
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
        if word.lower() == "toml" and line[a - 1:a] == ".":
            continue  # a filename such as kinds.toml, not a library
        if word == "TOML" and line[b:b + 1] not in (".", ":"):
            continue  # the format's name in prose ("not valid TOML"), not TOML.parse or TOML::
        return True
    return False


def _names(items: str) -> set[str]:
    """`a, b as c` / `{ a, b: c }` / `a::{b, c as d}` -> the names bound: a, c / a, c / b, d."""
    out = set()
    for item in re.split(r",", items.strip(" {}()")):
        words = re.findall(r"[\w$]+", item.split("::")[-1])
        if words and words[-1] not in ("self", "_"):
            out.add(words[-1])  # `x as y` and `x: y` bind y
    return out


def _import_bindings(line: str) -> set[str] | None:
    """The names an import of a TOML library binds (`from tomllib import load`, `import tomli as T`,
    `use toml::from_str;`, `import { parse } from "smol-toml"`, `t "github.com/pelletier/go-toml"`,
    `lib = importlib.import_module(...)`). None when the line is not an import of a TOML library."""
    if len(line) > MAX_IMPORT:
        return None
    if m := PY_FROM.match(line):
        return _names(m.group(2)) if TOML.search(m.group(1)) else None
    if m := PY_IMPORT.match(line):
        bound = set()
        for item in m.group(1).split(","):
            words = item.split()
            if TOML.search(words[0]):
                bound.add(words[-1] if len(words) == 3 else words[0].split(".")[0])
        return bound or None
    if m := RUST_USE.match(line):
        if not TOML.search(m.group(1)):
            return None
        if m.group(3):
            return {m.group(3)}
        return _names(m.group(2)) if m.group(2) is not None else {m.group(1).split("::")[-1]}
    if (m := JS_IMPORT.match(line)) or (m := JS_REQUIRE.match(line)):
        return _names(m.group(1).replace("* as", "")) if TOML.search(m.group(2)) else None
    if (m := GO_IMPORT.match(line)) and TOML.search(m.group(2)):
        return {(m.group(1) or "").strip() or m.group(2).rstrip("/").split("/")[-1]}
    if m := DYNAMIC_BIND.match(line):
        return {m.group(1)}
    return None


def _nearest(sorted_lines: list[int], n: int) -> int:
    """Distance from line n to the nearest line in sorted_lines (a large number when empty)."""
    i = bisect.bisect_left(sorted_lines, n)
    return min((abs(n - sorted_lines[j]) for j in (i - 1, i) if 0 <= j < len(sorted_lines)), default=1 << 30)


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
        # `from x import (` ... `)` across lines is read as one import line.
        for i, (n, line) in enumerate(kept):
            if OPEN_PAREN_IMPORT.match(line):
                tail = [t for _, t in kept[i + 1:i + 50]]
                end = next((j for j, t in enumerate(tail) if ")" in t), None)
                if end is not None:
                    kept[i] = (n, " ".join([line, *tail[:end + 1]]))
        files[rel] = kept

    # Names assigned the manifest's path name it too: UPPER_CASE constants anywhere in the repo
    # (unless a file assigns the same name something else), `self.x`/`this.x` attributes in their
    # own file, and other names in their own file from the assignment until they are reassigned or
    # a new function starts. An argument list or a qqsync call is not a path.
    shared: set[str] = set()
    assigned: dict[str, list[tuple[int, str, bool, bool]]] = {}  # rel -> (line, name, attr, manifest)
    for rel, lines in files.items():
        for n, line in lines:
            if len(line) > MAX_LINE or not (m := ASSIGNED.match(line)):
                continue
            attr, name, rhs = bool(m.group(1)), m.group(2), m.group(3)
            names_it = (bool(MANIFEST_PATH.search(rhs)) and name != "DEFAULT_PATH" and "qqsync" not in rhs
                        and rhs[:1] not in "[{")
            assigned.setdefault(rel, []).append((n, name, attr, names_it))
            if names_it and not attr and CONSTANT.match(name):
                shared.add(name)

    findings = []
    for rel, lines in files.items():
        mine = assigned.get(rel, [])
        shadowed = {name for _, name, attr, names_it in mine if not names_it and not attr}
        constants = shared - shadowed | {name for _, name, attr, names_it in mine
                                         if names_it and not attr and CONSTANT.match(name)}
        attrs = {name for _, name, attr, names_it in mine if names_it and attr}
        pattern = MANIFEST_PATH.pattern
        if constants:
            pattern += r"|\b(" + "|".join(sorted(map(re.escape, constants))) + r")\b"
        if attrs:
            pattern += r"|\b(self|this|cls)\.(" + "|".join(sorted(map(re.escape, attrs))) + r")\b"
        names_manifest = re.compile(pattern)
        manifest_lines = {n for n, line in lines if names_manifest.search(line)}
        # Lower-case names, scoped from their assignment to the next reassignment or function.
        starts = {n: name for n, name, attr, names_it in mine
                  if names_it and not attr and not CONSTANT.match(name)}
        if starts:
            rebinds = {(n, name) for n, name, attr, _ in mine if not attr}
            live: set[str] = set()
            for n, line in lines:
                if FUNCTION.match(line):
                    live.clear()
                live -= {name for name in live if (n, name) in rebinds}
                if n in starts:
                    live.add(starts[n])
                if live and n not in manifest_lines and re.search(
                        r"(?<![\w.])(" + "|".join(map(re.escape, sorted(live))) + r")\b", line):
                    manifest_lines.add(n)
        if not manifest_lines:
            continue
        manifest = sorted(manifest_lines)
        others = [n for n, line in lines if OTHER_TOML_FILE.search(line)]
        bindings = {n: b for n, line in lines if (b := _import_bindings(line)) is not None}
        readers = set().union(*bindings.values())
        bound = re.compile(r"(?<![\w.$])(" + "|".join(sorted(map(re.escape, readers))) + r")\b") if readers else None
        for n, line in lines:
            if (m := IMPORT_WORD.search(line)) and "repo.toml" in line[m.end():]:
                findings.append(Finding(rel, n, line))  # a loader importing the manifest file itself
                break
            if n in bindings:
                continue
            reads = (_reads_toml(line) or (bound is not None and bound.search(line) is not None)
                     or (n in manifest_lines and DYNAMIC_IMPORT.search(line) is not None))
            if not reads:
                continue
            to_manifest = _nearest(manifest, n)
            to_other = _nearest(others, n)
            # The read belongs to the nearest .toml path: a line naming the manifest wins outright,
            # otherwise the manifest must be in the window and nearer than any other .toml file.
            if to_manifest == 0 or (to_manifest <= WINDOW and to_manifest < to_other):
                findings.append(Finding(rel, n, line))
                break
    return findings
