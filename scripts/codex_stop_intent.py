"""Share a top-level user's stop decision between Codex prompt and Stop hooks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

# Match a direct instruction to end this work, rather than a mention of a stop
# command inside a longer message. Both Stop hooks consume this same decision.
COMMAND = re.compile(
    r"\A\s*(?:(?:please\s+)?(?:stop|kill|cancel|pause)"
    r"(?:\s+(?:working|the task|this task|the work|this work|what you are doing))?"
    r"(?:\s+now)?(?:,\s*please)?"
    r"|i\s+want\s+you\s+to\s+(?:stop|pause)(?:\s+working)?(?:\s+now)?)"
    r"\s*[.!]?\s*\Z",
    re.IGNORECASE,
)


def _path(session_id: str) -> Path:
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("missing session_id")
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return state / "switchstand/codex/stop-intent" / hashlib.sha256(session_id.encode()).hexdigest()


def record(payload: dict) -> None:
    prompt = payload.get("prompt")
    turn_id = payload.get("turn_id")
    if not isinstance(prompt, str):
        raise TypeError("UserPromptSubmit prompt must be a string")
    if not isinstance(turn_id, str) or not turn_id:
        raise ValueError("missing turn_id")
    target = _path(payload.get("session_id"))
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            json.dump({"turn_id": turn_id, "stop": bool(COMMAND.fullmatch(prompt))}, stream)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def requested(payload: dict) -> bool:
    turn_id = payload.get("turn_id")
    if not isinstance(turn_id, str) or not turn_id:
        return False
    try:
        with _path(payload.get("session_id")).open() as stream:
            decision = json.load(stream)
    except (OSError, ValueError, TypeError):
        return False
    return (isinstance(decision, dict) and decision.get("stop") is True
            and decision.get("turn_id") == turn_id)
