from __future__ import annotations

import argparse
from collections.abc import Sequence

from . import __version__
from .server import create_server, log_resolved_binaries
from .tools import BinaryResolver


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-toolkit-mcp",
        description="Expose system-janitor and system-updater as stdio MCP tools.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the agent-toolkit-mcp package version and exit",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"agent-toolkit-mcp {__version__}")
        return 0

    resolver = BinaryResolver()
    log_resolved_binaries(resolver)
    create_server(resolver).run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
