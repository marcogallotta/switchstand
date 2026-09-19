import concurrent.futures
import subprocess
import sys
from pathlib import Path

import pytest

from switchstand.check_environment import prepare, verify


def fixture(tmp_path):
    repo = tmp_path / "writer"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("project\n")
    (repo / "uv.lock").write_text("lock\n")
    uv = tmp_path / "uv"
    uv.write_text(f'''#!{sys.executable}
import os, pathlib, subprocess, sys, time
if sys.argv[1:] == ['--version']:
    print('uv 0.12.10')
    sys.exit()
p = pathlib.Path(os.environ['UV_PROJECT_ENVIRONMENT'])
with (p.parent / 'creations').open('a') as f:
    f.write('created\\n')
subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(p)], check=True)
for tool in ('ruff', 'pyright', 'pytest'):
    target = p / 'bin' / tool
    target.write_text('#!/bin/sh\\nexit 0\\n')
    target.chmod(0o755)
''')
    uv.chmod(0o755)
    return repo, uv


def test_private_generation_survives_shared_environment_replacement(tmp_path):
    repo, uv = fixture(tmp_path)
    primary = tmp_path / "primary"
    shared = primary / ".venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(shared)], check=True)
    generation, receipt = prepare(repo, uv)
    python = Path(generation) / "bin/python"
    before = python.stat()
    shared.rename(primary / "old-environment")
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(shared)], check=True)
    (repo / "candidate.py").write_text("VALUE = 'dirty candidate'\n")
    result = subprocess.check_output(
        [str(python), "-c", "import sys,candidate; print(sys.prefix); print(candidate.VALUE)"],
        cwd=repo, text=True,
    ).splitlines()
    assert result == [generation, "dirty candidate"]
    assert python.stat() == before
    verify(repo, Path(generation), receipt)
    assert prepare(repo, uv) == (generation, receipt)
    assert (repo / ".switchstand-check/creations").read_text() == "created\n"


def test_one_creator_and_new_generation_for_changed_manifest(tmp_path):
    repo, uv = fixture(tmp_path)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: prepare(repo, uv), range(2)))
    assert results[0] == results[1]
    generation, receipt = results[0]
    (repo / "uv.lock").write_text("new lock\n")
    with pytest.raises(ValueError, match="changed"):
        verify(repo, Path(generation), receipt)
    new = prepare(repo, uv)
    assert new != results[0]
    assert (Path(generation) / "complete.json").exists()
    assert (repo / ".switchstand-check/creations").read_text() == "created\ncreated\n"


def test_incomplete_generation_retries_but_invalid_completed_receipt_fails(tmp_path):
    repo, uv = fixture(tmp_path)
    generation, receipt = prepare(repo, uv)
    completed = Path(generation) / "complete.json"
    completed.unlink()
    assert prepare(repo, uv) == (generation, receipt)
    completed.write_text("{}\n")
    with pytest.raises(ValueError, match="receipt changed"):
        prepare(repo, uv)
    assert completed.read_text() == "{}\n"


def test_check_never_accepts_legacy_shared_binding(tmp_path):
    repo, uv = fixture(tmp_path)
    generation, receipt = prepare(repo, uv)
    with pytest.raises(ValueError, match="writer-private"):
        verify(repo, tmp_path / "primary/.venv", receipt)
    (Path(generation) / "bin/pytest").unlink()
    with pytest.raises(ValueError, match="incomplete"):
        verify(repo, Path(generation), receipt)


def test_real_locked_uv_generation(tmp_path):
    import shutil

    local = Path(__file__).parents[1] / '.switchstand-check/uv-0.12.10'
    uv = str(local) if local.is_file() else shutil.which('uv')
    if uv is None:
        pytest.skip('pinned uv unavailable; required in the quality image')
    repo = tmp_path / 'candidate'
    repo.mkdir()
    source = Path(__file__).parents[1]
    for name in ('pyproject.toml', 'uv.lock', 'README.md'):
        shutil.copyfile(source / name, repo / name)
    cache = source / '.switchstand-check/cache'
    if cache.is_dir():
        shutil.copytree(cache, repo / '.switchstand-check/cache', symlinks=True)
    shared = tmp_path / 'primary/.venv'
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(shared)], check=True)
    generation, receipt = prepare(repo, Path(uv))
    command = [str(Path(generation) / 'bin/python'), '-I', '-c',
               'import sys,mcp,sqlalchemy,httpx,pytest; print(sys.prefix); print(mcp.__file__)']
    before = subprocess.check_output(command, text=True)
    shared.rename(shared.with_name('old-environment'))
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(shared)], check=True)
    assert subprocess.check_output(command, text=True) == before
    assert all(line.startswith(generation) for line in before.splitlines())
    verify(repo, Path(generation), receipt)
    assert prepare(repo) == (generation, receipt)
