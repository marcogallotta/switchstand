"""Direct cgroup evidence must work without a systemd user bus."""
import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/agent-resource-admission"
loader = importlib.machinery.SourceFileLoader("admission", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
admission = importlib.util.module_from_spec(spec)
loader.exec_module(admission)


def cgroup(path, *, limit="max", current=0, tasks=0):
    path.mkdir(parents=True, exist_ok=True)
    for name, value in {"memory.max": limit, "memory.current": current, "pids.current": tasks}.items():
        (path / name).write_text(str(value))
    return path


def test_direct_evidence_without_user_bus(tmp_path, monkeypatch):
    monkeypatch.setattr(admission, "CGROUP_ROOT", tmp_path)
    parent = cgroup(tmp_path / "user.slice", limit=8192)
    root = cgroup(parent / "agents.slice", limit=16384, current=512, tasks=2)
    monkeypatch.setattr(admission, "agents_cgroup", lambda: root)
    monkeypatch.setattr(admission, "systemctl", lambda *args: pytest.fail("user bus required"))
    props, jobs = admission.slice_measurements("ss-codex-*.scope")
    assert props == {"TasksCurrent": "2", "MemoryCurrent": "512", "MemoryMax": "8192", "AncestorHeadroom": "8192"}
    assert jobs == ([], [], None, 0)


def test_direct_jobs_preserve_self_and_leftover(tmp_path, monkeypatch):
    monkeypatch.setattr(admission, "CGROUP_ROOT", tmp_path)
    root = cgroup(tmp_path / "agents.slice")
    cgroup(root / "ss-codex-self.scope", current=50, tasks=1)
    cgroup(root / "ss-codex-other.scope", current=70, tasks=1)
    cgroup(root / "ss-codex-left.scope", tasks=1)
    cgroup(root / "ss-codex-empty.scope", tasks=0)
    monkeypatch.setattr(admission, "caller_scope", lambda pattern: "ss-codex-self.scope")
    monkeypatch.setattr(admission, "has_codex_exec", lambda path: not path.endswith("left.scope"))
    assert admission.direct_memory_jobs(root, "ss-codex-*.scope") == (
        ["ss-codex-other.scope"], [("ss-codex-left.scope", "unknown")], "ss-codex-self.scope", 120)


def test_missing_cgroup_falls_back_as_one_measurement(tmp_path, monkeypatch):
    monkeypatch.setattr(admission, "agents_cgroup", lambda: tmp_path / "missing")
    props = {"TasksCurrent": "0", "MemoryCurrent": "100", "MemoryMax": "infinity"}
    jobs = ([], [], None, 0)
    monkeypatch.setattr(admission, "show_properties", lambda *args: props)
    monkeypatch.setattr(admission, "memory_jobs", lambda pattern: jobs)
    assert admission.slice_measurements("ss-codex-*.scope") == (props, jobs)


def test_both_measurement_sources_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(admission, "agents_cgroup", lambda: tmp_path / "missing")
    def unavailable(*args):
        raise OSError("no user bus")
    monkeypatch.setattr(admission, "show_properties", unavailable)
    with pytest.raises(OSError, match="no user bus"):
        admission.slice_measurements("ss-codex-*.scope")


def test_finite_ancestor_uses_own_current_including_siblings(tmp_path, monkeypatch):
    monkeypatch.setattr(admission, "CGROUP_ROOT", tmp_path)
    parent = cgroup(tmp_path / "user.slice", limit=1024, current=950)
    root = cgroup(parent / "agents.slice", limit=2048, current=100, tasks=1)
    props = admission.direct_slice_properties(root)
    assert props["AncestorHeadroom"] == "74"
    # Even an idle own slice must respect memory held by its siblings.
    (root / "pids.current").write_text("0")
    assert admission.direct_slice_properties(root)["AncestorHeadroom"] == "74"


@pytest.mark.parametrize('tasks', [0, 1])
def test_main_waits_when_siblings_exhaust_ancestor(tmp_path, monkeypatch, capsys, tasks):
    from types import SimpleNamespace
    mib = 1024 * 1024
    monkeypatch.setattr(admission, 'CGROUP_ROOT', tmp_path)
    parent = cgroup(tmp_path / 'user.slice', limit=1024 * mib, current=950 * mib)
    root = cgroup(parent / 'agents.slice', limit=2048 * mib, current=100 * mib, tasks=tasks)
    monkeypatch.setattr(admission, 'agents_cgroup', lambda: root)
    monkeypatch.setattr(admission, 'parse_args', lambda: SimpleNamespace(
        estimate_mib=100, config=tmp_path, scope_pattern='ss-codex-*.scope'))
    monkeypatch.setattr(admission, 'read_config', lambda _: {
        'reserve_mib': 0, 'swap_window_seconds': 0, 'max_swap_growth_kib': 0,
        'psi_avg10_max': 2, 'max_memory_jobs': 0})
    monkeypatch.setattr(admission, 'read_mem_available_mib', lambda: 4096)
    monkeypatch.setattr(admission, 'read_psi_avg10', lambda: 0)
    monkeypatch.setattr(admission, 'read_swap_pages', lambda: (0, 0))
    assert admission.main() == 1
    assert 'WAIT reason=ancestor_capacity ' in capsys.readouterr().out
