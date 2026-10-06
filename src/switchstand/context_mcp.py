"""Compatibility entrypoint for the historical context MCP command.

The managed context profile now runs the canonical managed MCP implementation with
server-side tool restriction. This module remains only for old direct callers.
"""

import os

from .mcp import main as canonical_main


def main() -> None:
    os.environ["SWITCHSTAND_MANAGED_PROFILE"] = "context"
    canonical_main()


if __name__ == "__main__":
    main()
