# SPDX-License-Identifier: Apache-2.0
"""Command-line entry point. Scaffold stub — the MVP `check` command lands next."""

from __future__ import annotations

import argparse

from costgate import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="costgate",
        description="BigQuery cost gate for dbt pull requests.",
    )
    parser.add_argument("--version", action="version", version=f"costgate {__version__}")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "check", help="Estimate the cost impact of changed dbt models (not yet implemented)."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "check":
        parser.exit(2, "costgate check is not implemented yet (pre-MVP scaffold).\n")
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
