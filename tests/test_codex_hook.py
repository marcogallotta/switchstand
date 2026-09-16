import json
import os
import subprocess
from pathlib import Path

HOOK = Path(__file__).parents[1] / "scripts" / "codex-hook"


def invoke(command: str, cwd: Path, normal: bool) -> dict:
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(cwd),
    }
    environment = os.environ | ({"SWITCHSTAND_NORMAL_WORK": "1"} if normal else {})
    result = subprocess.run(
        [HOOK],
        input=json.dumps(payload),
        env=environment,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout) if result.stdout else {}


def test_normal_writer_denies_push_and_merge_but_not_add(tmp_path):
    control = tmp_path / "control"
    writer = tmp_path / "writer"
    subprocess.run(["git", "init", "-b", "main", str(control)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(control), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(control), "config", "user.email", "test@example.invalid"], check=True)
    (control / "tracked.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(control), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(control), "commit", "-m", "base"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(control), "worktree", "add", "-b", "v2-writer", str(writer)],
        check=True,
        capture_output=True,
    )
    for command in ("git push origin HEAD", "git merge feature"):
        decision = invoke(command, writer, normal=True)
        output = decision["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert "not authorized by the normal launcher" in output["permissionDecisionReason"]
    assert invoke("git add tracked.txt", writer, normal=True) == {}
    assert invoke("git push origin HEAD", writer, normal=False) == {}
