"""Command-line entry point for the bounded local worker."""

from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import sys
import time

from .protocol import ProtocolError
from .supervisor import Worker, WorkerConfig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the bounded local Codex worker")
    parser.add_argument("--coordinator-url", required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--worker-id")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    key = os.environ.pop("SWITCHSTAND_WORKER_KEY", None) or getpass.getpass("Worker bearer key: ")
    try:
        try:
            config = WorkerConfig.create(args.coordinator_url, key, args.state_root, worker_id=args.worker_id)
        finally:
            key = ""
        worker = Worker(config)
        while True:
            worked = worker.run_once()
            if args.once:
                return 0
            if not worked:
                time.sleep(2)
    except ProtocolError as exc:
        print(exc.code.upper(), file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(
            "HOST_PREREQUISITE_MISSING" if str(exc) == "host_prerequisite_missing" else "WORKER_FAILURE",
            file=sys.stderr,
        )
        return 1
    except Exception:
        print("WORKER_FAILURE", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
