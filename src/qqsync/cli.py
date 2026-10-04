"""qqsync: the quirq infra manifest tool.

    qqsync validate [PATH ...] [--kind KIND ...]   check manifests against their schema
                                                   (default infra/repo.toml). Exit 1 on any problem.
"""
from __future__ import annotations

import argparse
import sys

from qqsync import __version__
from qqsync.errors import ManifestError
from qqsync.manifest import DEFAULT_PATH, load


def _validate(args: argparse.Namespace) -> int:
    failed = False
    for path in args.paths or [DEFAULT_PATH]:
        try:
            load(path, known_kinds=args.kind or None)
        except ManifestError as e:
            print(e, file=sys.stderr)
            failed = True
        else:
            print(f"{path}: PASS")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qqsync", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"qqsync {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    v = sub.add_parser("validate", help="check manifests against their schema")
    v.add_argument("paths", nargs="*", help=f"manifest files (default {DEFAULT_PATH})")
    v.add_argument("--kind", action="append",
                   help="a known target kind; repeat it. Given at least once, any other kind is an error")
    v.set_defaults(run=_validate)

    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
