import json
import os
import sys
from pathlib import Path

from switchstand.codex_runtime import PROFILE, readback


def test_real_stdio_app_server_boundary_returns_managed_profile(tmp_path: Path) -> None:
    control = tmp_path / "control"
    candidate = tmp_path / "candidate"
    bindir = tmp_path / "bin"
    control.mkdir()
    candidate.mkdir()
    bindir.mkdir()

    codex = bindir / "codex"
    codex.write_text(
        f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

for line in sys.stdin:
    request = json.loads(line)
    request_id = request.get("id")
    if request_id is None:
        continue
    method = request["method"]
    if method == "initialize":
        result = {{"serverInfo": {{"name": "fixture", "version": "1"}}}}
    elif method == "permissionProfile/list":
        result = {{"data": [{{"id": "{PROFILE}", "allowed": True}}]}}
    elif method == "thread/start":
        roots = request["params"]["runtimeWorkspaceRoots"]
        result = {{
            "activePermissionProfile": {{"id": "{PROFILE}"}},
            "sandbox": {{
                "type": "workspaceWrite",
                "networkAccess": True,
                "writableRoots": [roots[1]],
            }},
            "runtimeWorkspaceRoots": roots,
            "approvalPolicy": "never",
            "instructionSources": [
                str(Path.home() / ".codex/AGENTS.md"),
                str(Path(roots[0]) / "AGENTS.md"),
            ],
        }}
    else:
        raise SystemExit(f"unexpected method: {{method}}")
    print(json.dumps({{"jsonrpc": "2.0", "id": request_id, "result": result}}), flush=True)
"""
    )
    codex.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")

    result = readback(control, candidate, env)

    assert result.profile == PROFILE
    assert result.sandbox == "workspaceWrite"
    assert set(result.instruction_sources) == {
        str(Path.home() / ".codex/AGENTS.md"),
        str(control / "AGENTS.md"),
    }
