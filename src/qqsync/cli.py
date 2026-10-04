"""Command line entry point: `qqsync <command>`."""
from __future__ import annotations

import argparse
import sys

from qqsync import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qqsync", description=__doc__)
    parser.add_argument("--version", action="version", version=f"qqsync {__version__}")
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
