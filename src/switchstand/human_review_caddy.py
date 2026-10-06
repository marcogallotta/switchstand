"""Render an inert Caddy route fragment for the Human Review shell."""

from __future__ import annotations

import argparse
import json
import re
from typing import Any

INTERNAL_PATH = "/human-review"
PUBLIC_PATH = re.compile(r"(?:/[A-Za-z0-9._~-]+)+")


def caddy_route(public_path: str, bind_port: int) -> dict[str, Any]:
    if (
        PUBLIC_PATH.fullmatch(public_path) is None
        or not public_path.endswith(INTERNAL_PATH)
        or any(segment in {".", ".."} for segment in public_path.split("/"))
    ):
        raise ValueError("public path must be a literal path ending in /human-review")
    if not 1 <= bind_port <= 65535:
        raise ValueError("Human Review bind port must be between 1 and 65535")
    strip_prefix = public_path.removesuffix(INTERNAL_PATH)
    handle: list[dict[str, Any]] = []
    if strip_prefix:
        handle.append({"handler": "rewrite", "strip_path_prefix": strip_prefix})
    handle.append(
        {
            "@id": "switchstand_human_review_upstream",
            "handler": "reverse_proxy",
            "upstreams": [{"dial": f"127.0.0.1:{bind_port}"}],
        }
    )
    return {
        "@id": "switchstand_human_review_route",
        "match": [{"path": [public_path, f"{public_path}/*"]}],
        "handle": handle,
        "terminal": True,
    }


def run() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-path", required=True)
    parser.add_argument("--bind-port", required=True, type=int)
    arguments = parser.parse_args()
    print(json.dumps(caddy_route(arguments.public_path, arguments.bind_port), sort_keys=True))


if __name__ == "__main__":
    run()
