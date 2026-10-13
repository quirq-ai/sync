"""How `project` hangs under a parser: qqsync's own, and one built the way `qq sync` builds its."""
import argparse
import json

from qqsync.cli import main
from qqsync.project import cli


def qq_sync_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qq")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sync = sub.add_parser("sync")
    sync.set_defaults(run=lambda args: "pins")   # bare `qq sync`: the manifest's pins, as before
    cli.register(sync.add_subparsers(dest="sync_command", metavar="COMMAND"), prefix="qq")
    return parser


def test_bare_sync_is_left_alone():
    args = qq_sync_parser().parse_args(["sync"])
    assert args.run(args) == "pins"


def test_qq_sync_project_check(world, capsys):
    args = qq_sync_parser().parse_args(["sync", "project", "check", "--root", str(world.app), "--json"])
    assert args.run(args) == 0
    assert json.loads(capsys.readouterr().out)["repos"][0]["state"] == "current"


def test_options_before_the_subcommand_are_kept(world, capsys):
    world.push("a")
    assert main(["project", "--root", str(world.app), "--json", "check"]) == cli.UPDATES_AVAILABLE
    assert json.loads(capsys.readouterr().out)["repos"][0]["name"] == "app"


def test_update_options_on_a_subcommand_are_usage(world, capsys):
    assert main(["project", "--fetch-only", "check", "--root", str(world.app)]) == 2
    assert "--fetch-only" in capsys.readouterr().err


def test_errors_go_to_stderr_with_the_caller_s_prefix(world, capsys):
    args = qq_sync_parser().parse_args(["sync", "project", "--root", str(world.projects / "missing")])
    assert args.run(args) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("qq: ")
