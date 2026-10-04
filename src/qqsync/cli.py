"""qqsync: the quirq infra manifest tool. The only program that reads or edits infra/repo.toml.

    qqsync validate [PATH ...] [--kind KIND ...]   check manifests against their schema.
    qqsync show [PATH]                             print a manifest as JSON, for tools in any
                                                   language: read it through qqsync, never parse it.
    qqsync pin SECTION NAME --digest D [--source S] [--version V] [--platform P] [--manifest PATH]
                                                   set one toolchain or dependency pin, keeping the
                                                   rest of the file byte for byte.
    qqsync pins [PATH] [--strict]                  list every pin; --strict also rejects
                                                   placeholder (all-zero) digests.
    qqsync fetch SECTION NAME --dest FILE [--platform P] [--manifest PATH]
                                                   download a pinned artifact; fails, leaving
                                                   nothing behind, unless it matches its pin.
    qqsync verify SECTION NAME PATH [--platform P] [--manifest PATH]
                                                   check a fetched file or checkout against its pin.
    qqsync guard [ROOT]                            fail if any file under ROOT parses repo.toml
                                                   outside qqsync (a presubmit for every repo).

PATH defaults to infra/repo.toml. Exit 1 on any problem, with every problem printed.
"""
from __future__ import annotations

import argparse
import json
import sys

from qqsync import __version__
from qqsync.errors import ManifestError
from qqsync.guard import GuardError, scan
from qqsync.manifest import DEFAULT_PATH, PIN_SECTIONS, Manifest
from qqsync.pins import PinError, fetch, find_pin, iter_pins, placeholders, verify_checkout, verify_file


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
    # TOML dates and times have no JSON type; they come out as ISO 8601 strings.
    print(json.dumps(Manifest.read(args.path).data, indent=2, sort_keys=True, default=lambda o: o.isoformat()))
    return 0


def _pin(args: argparse.Namespace) -> int:
    manifest = Manifest.read(args.manifest)
    manifest.set_pin(args.section, args.name, digest=args.digest, source=args.source,
                     version=args.version, platform=args.platform)
    manifest.write()
    print(f"{args.manifest}: {args.section}.{args.name} pinned to {args.digest}")
    return 0


def _pins(args: argparse.Namespace) -> int:
    data = Manifest.read(args.path).data
    for pin in iter_pins(data):
        print(f"{pin.label}\t{pin.digest}\t{pin.source}")
    problems = placeholders(data) if args.strict else []
    for problem in problems:
        print(f"{args.path}: {problem}", file=sys.stderr)
    return 1 if problems else 0


def _fetch(args: argparse.Namespace) -> int:
    pin = find_pin(Manifest.read(args.manifest).data, args.section, args.name, args.platform)
    fetch(pin, args.dest)
    print(f"{pin.label}: {args.dest} matches {pin.digest}")
    return 0


def _verify(args: argparse.Namespace) -> int:
    pin = find_pin(Manifest.read(args.manifest).data, args.section, args.name, args.platform)
    (verify_checkout if pin.algorithm == "git" else verify_file)(pin, args.path)
    print(f"{pin.label}: {args.path} matches {pin.digest}")
    return 0


def _guard(args: argparse.Namespace) -> int:
    notes: list[str] = []
    try:
        findings = scan(args.root, args.allow or [], notes)
    except GuardError as e:
        print(e, file=sys.stderr)
        return 1
    for note in notes:
        print(f"note: {note}", file=sys.stderr)
    for finding in findings:
        print(finding, file=sys.stderr)
    if not findings:
        print(f"{args.root}: PASS (no parser of repo.toml outside qqsync)")
    return 1 if findings else 0


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

    ps = sub.add_parser("pins", help="list every pin")
    ps.add_argument("path", nargs="?", default=DEFAULT_PATH)
    ps.add_argument("--strict", action="store_true", help="reject placeholder (all-zero) digests")
    ps.set_defaults(run=_pins)

    f = sub.add_parser("fetch", help="download a pinned artifact and verify it")
    f.add_argument("section", choices=PIN_SECTIONS)
    f.add_argument("name")
    f.add_argument("--dest", required=True)
    f.add_argument("--platform", help="for per-platform pins (default: this machine)")
    f.add_argument("--manifest", default=DEFAULT_PATH)
    f.set_defaults(run=_fetch)

    vf = sub.add_parser("verify", help="check a fetched file or checkout against its pin")
    vf.add_argument("section", choices=PIN_SECTIONS)
    vf.add_argument("name")
    vf.add_argument("path")
    vf.add_argument("--platform", help="for per-platform pins (default: this machine)")
    vf.add_argument("--manifest", default=DEFAULT_PATH)
    vf.set_defaults(run=_verify)

    g = sub.add_parser("guard", help="fail if anything parses repo.toml outside qqsync")
    g.add_argument("root", nargs="?", default=".")
    g.add_argument("--allow", action="append", metavar="GLOB",
                   help="skip these paths; only quirq-ai/sync itself uses this, for its own library")
    g.set_defaults(run=_guard)

    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except PinError as e:
        print(e, file=sys.stderr)
        return 1
    except ManifestError as e:
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
