import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
INSTALLER = ROOT / "scripts/install-disk-pressure-monitor"


def executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)


def test_installer_renders_bounded_low_priority_units(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "canonical repo"
    runner = repo / ".venv/bin/switchstand-disk-pressure"
    executable(runner, "#!/bin/sh\nexit 0\n")
    tools = tmp_path / "tools"
    executable(
        tools / "systemctl",
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$HOME/systemctl-calls"\n',
    )
    env = os.environ | {"HOME": str(home), "PATH": f"{tools}:{os.environ['PATH']}"}

    subprocess.run([INSTALLER, repo], env=env, check=True)

    unit_dir = home / ".config/systemd/user"
    service = (unit_dir / "switchstand-disk-pressure.service").read_text()
    timer = (unit_dir / "switchstand-disk-pressure.timer").read_text()
    assert f'ExecStart="{runner}" check' in service
    assert "TimeoutStartSec=30s" in service
    assert "Nice=10" in service and "IOSchedulingClass=idle" in service
    assert "OnUnitInactiveSec=15min" in timer
    assert "enable --now switchstand-disk-pressure.timer" in (
        home / "systemctl-calls"
    ).read_text()
