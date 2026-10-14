"""A small world of real git repos: a bare `origin`, a `seed` clone that pushes to it, and a
`projects` folder holding the working clone `app` that the commands act on."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from qqsync.cli import main


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, name: str, text: str = "x") -> str:
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", f"add {name}")
    return git(repo, "rev-parse", "HEAD")


@dataclass
class World:
    origin: Path
    seed: Path
    projects: Path
    app: Path

    def push(self, name: str, text: str = "x") -> str:
        """A new commit on origin's main, made somewhere else."""
        sha = commit(self.seed, name, text)
        git(self.seed, "push", "-q", "origin", "main")
        return sha

    def clone(self, name: str) -> Path:
        git(self.projects, "clone", "-q", str(self.origin), name)
        return self.projects / name


@pytest.fixture(autouse=True)
def git_identity(tmp_path_factory, monkeypatch):
    """The same git settings for the tests and for the commands, whatever this machine has configured."""
    empty = tmp_path_factory.mktemp("gitconfig") / "config"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "qqsync tests")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "tests@example.invalid")


@pytest.fixture
def world(tmp_path: Path) -> World:
    origin, seed, projects = tmp_path / "origin.git", tmp_path / "seed", tmp_path / "projects"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "init", "-q", "-b", "main", str(seed))
    git(seed, "remote", "add", "origin", str(origin))
    commit(seed, "README")
    git(seed, "push", "-q", "-u", "origin", "main")
    projects.mkdir()
    w = World(origin, seed, projects, projects / "app")
    w.clone("app")
    return w


@pytest.fixture
def run(capsys):
    """Run `qqsync project ARGV --json`; returns (exit code, the JSON document it printed)."""
    def call(*argv: str) -> tuple[int, dict]:
        code = main(["project", "--json", *argv])
        return code, json.loads(capsys.readouterr().out)
    return call
