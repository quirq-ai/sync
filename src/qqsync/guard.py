"""The "no other parser" check: only qqsync may parse infra/repo.toml.

"Every parser of DEPS is a dependency on DEPS." A second parser drifts from the schema and pins the
format in place, so any repo's presubmit runs `qqsync guard`, which fails when a file both names the
manifest and uses a TOML parser. Tools read the manifest with `qqsync show` (JSON) or the qqsync
library instead.

The rule is deliberately simple and language-blind: a file that mentions `repo.toml` and also names
a TOML library (any lowercase identifier starting with "toml", such as an import, a require, a
crate or a module, or `TOML.parse`) is a second parser. Filenames like `kinds.toml` and the word
"TOML" in prose don't count. Markdown, plain text and TOML data files are skipped.
TODO(expert): this misses parsers that read the file without a TOML library (sed, grep, regexes).
"""
from __future__ import annotations

import fnmatch
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

MANIFEST_NAME = "repo.toml"
TOML_PARSER = re.compile(r"(?<![.\w])toml|\bTOML\s*\.\s*\w")  # tomllib, tomlkit, @iarna/toml, TOML.parse
SKIP_SUFFIXES = frozenset({".md", ".markdown", ".txt", ".rst", ".toml"})
MAX_BYTES = 1 << 20


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    text: str

    def __str__(self) -> str:
        return (f"{self.path}:{self.line}: parses {MANIFEST_NAME} outside qqsync: {self.text.strip()[:120]}\n"
                f"    read the manifest with `qqsync show` or the qqsync library instead")


def tracked_files(root: Path) -> list[str]:
    """Files git tracks under `root`, or every file when `root` is not a checkout."""
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True).stdout
        return sorted(p for p in out.decode().split("\0") if p)
    except (OSError, subprocess.CalledProcessError):
        return sorted(str(p.relative_to(root)) for p in root.rglob("*")
                      if p.is_file() and ".git" not in p.relative_to(root).parts)


def scan(root: str | Path, allow: list[str] = ()) -> list[Finding]:
    """Every place under `root` that parses the manifest outside qqsync."""
    root = Path(root)
    findings = []
    for rel in tracked_files(root):
        if Path(rel).suffix.lower() in SKIP_SUFFIXES or any(fnmatch.fnmatch(rel, g) for g in allow):
            continue
        path = root / rel
        try:
            if not path.is_file() or path.stat().st_size > MAX_BYTES:
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\0" in raw:
            continue  # binary
        text = raw.decode("utf-8", errors="replace")
        if MANIFEST_NAME not in text:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if TOML_PARSER.search(line):
                findings.append(Finding(rel, number, line))
                break  # one finding per file is enough to act on
    return findings
