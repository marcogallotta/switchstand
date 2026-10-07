"""Compatibility entrypoint for the historical context MCP command.

The managed context profile now runs the canonical managed MCP implementation with
server-side tool restriction. This module remains only for old direct callers.
"""

import os

from .mcp import main as canonical_main


def main() -> None:
    previous = os.environ.get("SWITCHSTAND_MANAGED_PROFILE")
    os.environ["SWITCHSTAND_MANAGED_PROFILE"] = "context"
    try:
        canonical_main()
    finally:
        if previous is None:
            os.environ.pop("SWITCHSTAND_MANAGED_PROFILE", None)
        else:
            os.environ["SWITCHSTAND_MANAGED_PROFILE"] = previous


if __name__ == "__main__":
    main()
