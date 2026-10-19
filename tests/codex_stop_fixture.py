from __future__ import annotations

import json
from pathlib import Path


def native_source(path: Path, session: str, turn: str, *messages: str) -> str:
    records = [
        {"type": "session_meta", "payload": {
            "id": session, "cwd": str(path.parent), "runtime_workspace_roots": [str(path.parent)],
        }},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}},
    ]
    for index, text in enumerate(messages):
        records.append({
            "type": "response_item", "payload": {
                "role": "user", "id": f"message-{index}",
                "content": [{"type": "input_text", "text": text}],
                "internal_chat_message_metadata_passthrough": {
                    "turn_id": turn, "content_item_kinds": ["user.text"],
                },
            },
            "metadata": {"retained_source": {"complete": True}},
        })
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return str(path)
