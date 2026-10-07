import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
CONTROL = ROOT / "scripts/switchstand-wakeful-root-service"
ASSET = ROOT / "packaging/systemd/user/switchstand-wakeful-root.service"


@pytest.fixture
def host(tmp_path, monkeypatch):
    home = tmp_path / "home"
    tools = tmp_path / "tools"
    home.mkdir()
    tools.mkdir()
    systemctl = tools / "systemctl"
    systemctl.write_text("""#!/bin/sh
set -eu
printf '%s\n' "$*" >> "$HOME/systemctl-calls"
case "$*" in
  *--property=ActiveState*) printf '%s\n' inactive ;;
  *--no-pager*) printf '%s\n' 'LoadState=loaded' 'ActiveState=inactive' ;;
esac
""")
    systemctl.chmod(0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    return home


def invoke(*arguments):
    return subprocess.run([CONTROL, *arguments], text=True, capture_output=True,
                          check=False, env=os.environ)


def private_config(path):
    path.write_text("{}\n")
    path.chmod(0o600)
    return path


def test_install_is_inert_exact_and_idempotent(host, tmp_path):
    source = private_config(tmp_path / "wakeful.json")
    assert invoke("install", "--config", str(source)).returncode == 0
    unit = host / ".config/systemd/user/switchstand-wakeful-root.service"
    config = host / ".config/switchstand/wakeful-root.json"
    assert unit.read_bytes() == ASSET.read_bytes()
    assert config.read_bytes() == source.read_bytes()
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert invoke("install", "--config", str(source)).returncode == 0
    calls = (host / "systemctl-calls").read_text().splitlines()
    assert calls == ["--user daemon-reload"]
    assert not any(action in " ".join(calls) for action in ("enable", "start", "restart"))


def test_disable_uninstall_preserves_config(host, tmp_path):
    source = private_config(tmp_path / "wakeful.json")
    assert invoke("install", "--config", str(source)).returncode == 0
    assert invoke("disable").returncode == 0
    assert invoke("uninstall").returncode == 0
    assert not (host / ".config/systemd/user/switchstand-wakeful-root.service").exists()
    assert (host / ".config/switchstand/wakeful-root.json").exists()


@pytest.mark.parametrize("kind", ["mode", "symlink"])
def test_install_rejects_untrusted_config(host, tmp_path, kind):
    source = private_config(tmp_path / "wakeful.json")
    if kind == "mode":
        source.chmod(0o644)
    else:
        target = private_config(tmp_path / "target")
        source.unlink()
        source.symlink_to(target)
    assert invoke("install", "--config", str(source)).returncode != 0
