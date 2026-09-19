import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize('layout', ['primary', 'linked', 'standalone'])
def test_controller_python_resolution_is_explicit_and_verified(tmp_path, layout):
    import shutil

    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', str(repo)], check=True, capture_output=True)
    script = repo / 'scripts/switchstand-python'
    script.parent.mkdir()
    script.write_bytes((Path(__file__).parents[1] / 'scripts/switchstand-python').read_bytes())
    script.chmod(0o755)
    # Imports are isolated fixtures; interpreter selection and venv semantics are real.
    python = repo / '.venv/bin/python'
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(python.parent.parent)], check=True)
    site = next((repo / '.venv/lib').glob('python*/site-packages'))
    for module in ('mcp', 'sqlalchemy', 'httpx'):
        (site / f'{module}.py').touch()
    if layout == 'linked':
        subprocess.run(['git', '-C', str(repo), 'add', 'scripts'], check=True)
        subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c',
                        'user.email=test@example.invalid', 'commit', '-m', 'fixture'],
                       check=True, capture_output=True)
        linked = tmp_path / 'linked'
        subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '--detach', str(linked)],
                       check=True, capture_output=True)
        script = linked / 'scripts/switchstand-python'
    import os

    environment = dict(os.environ)
    environment.pop('SWITCHSTAND_CONTROLLER_PYTHON', None)
    if layout == 'standalone':
        destination = tmp_path / 'explicit-runtime'
        shutil.move(repo / '.venv', destination)
        python = destination / 'bin/python'
        environment['SWITCHSTAND_CONTROLLER_PYTHON'] = str(python)
    result = subprocess.run([script], env=environment, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(python)
    result = subprocess.run([script], env=environment | {'SWITCHSTAND_CONTROLLER_PYTHON': '/bin/false'},
                            capture_output=True, text=True, check=False)
    assert result.returncode != 0 and 'controller Python' in result.stderr
