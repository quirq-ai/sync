"""qqsync: the quirq infra manifest tool. The only program that reads or edits infra/repo.toml.

    qqsync validate [PATH ...] [--kind KIND ...]   check manifests against their schema.
    qqsync show [PATH]                             print a manifest as JSON, for tools in any
                                                   language: read it through qqsync, never parse it.
    qqsync pin SECTION NAME --digest D [--source S] [--version V] [--platform P] [--manifest PATH]
                                                   set one toolchain or dependency pin, keeping the
                                                   rest of the file byte for byte.

PATH defaults to infra/repo.toml. Exit 1 on any problem, with every problem printed.
"""
from __future__ import annotations

import argparse
import json
import sys

from qqsync import __version__
from qqsync.errors import ManifestError
from qqsync.manifest import DEFAULT_PATH, PIN_SECTIONS, Manifest


def _validate(args: argparse.Namespace) -> int:
    failed = False
    for path in args.paths or [DEFAULT_PATH]:
        try:
            Manifest.read(path, known_kinds=args.kind or None)
        except ManifestError as e:
            print(e, file=sys.stderr)
            failed = True
        else:
            print(f"{path}: PASS")
    return 1 if failed else 0


def _show(args: argparse.Namespace) -> int:
    print(json.dumps(Manifest.read(args.path).data, indent=2, sort_keys=True))
    return 0


def _pin(args: argparse.Namespace) -> int:
    manifest = Manifest.read(args.manifest)
    manifest.set_pin(args.section, args.name, digest=args.digest, source=args.source,
                     version=args.version, platform=args.platform)
    manifest.write()
    print(f"{args.manifest}: {args.section}.{args.name} pinned to {args.digest}")
    return 0


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

    s = sub.add_parser("show", help="print a manifest as JSON")
    s.add_argument("path", nargs="?", default=DEFAULT_PATH)
    s.set_defaults(run=_show)

    p = sub.add_parser("pin", help="set one toolchain or dependency pin")
    p.add_argument("section", choices=PIN_SECTIONS)
    p.add_argument("name")
    p.add_argument("--digest", required=True)
    p.add_argument("--source")
    p.add_argument("--version", dest="version")
    p.add_argument("--platform")
    p.add_argument("--manifest", default=DEFAULT_PATH)
    p.set_defaults(run=_pin)

    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except ManifestError as e:
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
