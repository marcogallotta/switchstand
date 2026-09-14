import os
import shutil
import subprocess
from pathlib import Path

import pytest

SELECTOR_SOURCE = Path(__file__).parents[1] / "scripts" / "switchstand-selector"


def run(*args, cwd=None, env=None, check=True):
    return subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, check=check)


def git(repo: Path, *args: str) -> str:
    return run('git', *args, cwd=repo).stdout.strip()


@pytest.fixture
def selector_fixture(tmp_path: Path):
    if shutil.which('git') is None:
        pytest.skip('git required')
    home = tmp_path / 'home'
    bin_dir = home / '.local' / 'bin'
    selector_root = home / '.local' / 'state' / 'switchstand' / 'control'
    controls = selector_root / 'controls'
    bin_dir.mkdir(parents=True)
    controls.mkdir(parents=True)
    wrapper = bin_dir / 'switchstand-start'
    wrapper.write_bytes(SELECTOR_SOURCE.read_bytes())
    wrapper.chmod(0o755)

    origin = tmp_path / 'origin'
    origin.mkdir()
    git(origin, 'init', '-q')
    git(origin, 'config', 'user.email', 'test@example.com')
    git(origin, 'config', 'user.name', 'Test')
    (origin / 'scripts').mkdir()
    internal = origin / 'scripts' / 'switchstand-start'
    internal.write_text(
        '#!/bin/sh\n'
        'set -eu\n'
        'printf "%s\\n" "$SWITCHSTAND_CONTROL_SHA" > "$CONTROL_RECEIPT.sha"\n'
        'printf "%s\\n" "$SWITCHSTAND_CONTROL_PATH" > "$CONTROL_RECEIPT.path"\n'
        'printf "%s\\n" "$SWITCHSTAND_CONTROL_COMMON" > "$CONTROL_RECEIPT.common"\n'
        'pwd > "$CONTROL_RECEIPT.cwd"\n'
        'printf "%s\\n" "$@" > "$CONTROL_RECEIPT.args"\n'
    )
    internal.chmod(0o755)
    git(origin, 'add', 'scripts/switchstand-start')
    git(origin, 'commit', '-qm', 'control')
    sha = git(origin, 'rev-parse', 'HEAD')
    git(origin, 'remote', 'add', 'origin', 'https://github.com/marcogallotta/switchstand.git')

    control = controls / sha
    run('git', 'clone', '-q', '--no-hardlinks', str(origin), str(control))
    git(control, 'remote', 'set-url', 'origin', 'https://github.com/marcogallotta/switchstand.git')
    git(control, 'checkout', '-q', '--detach', sha)

    manifest = selector_root / 'manifest'
    receipt = tmp_path / 'receipt'
    env = os.environ | {'HOME': str(home), 'CONTROL_RECEIPT': str(receipt)}
    return wrapper, manifest, control, sha, receipt, env, controls


def paused(manifest: Path):
    manifest.write_text('state=PAUSED\n')


def active(manifest: Path, sha: str, path: Path, repository='marcogallotta/switchstand'):
    manifest.write_text(
        f'state=ACTIVE\nrepository={repository}\ncontrol_sha={sha}\ncontrol_path={path}\n'
    )


def test_paused_fails_before_git_or_control_execution(selector_fixture, tmp_path):
    wrapper, manifest, _control, _sha, receipt, env, _controls = selector_fixture
    paused(manifest)
    fake = tmp_path / 'fake-bin'
    fake.mkdir()
    marker = tmp_path / 'git-called'
    fake_git = fake / 'git'
    fake_git.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 99\n')
    fake_git.chmod(0o755)
    env['PATH'] = f"{fake}:{env['PATH']}"
    result = run(str(wrapper), '--active', '123', cwd=tmp_path, env=env, check=False)
    assert result.returncode == 1
    assert 'selector is PAUSED' in result.stderr
    assert not marker.exists()
    assert not receipt.with_suffix('.sha').exists()


def test_active_ignores_caller_path_git(selector_fixture, tmp_path):
    wrapper, manifest, control, sha, receipt, env, _controls = selector_fixture
    active(manifest, sha, control)
    fake = tmp_path / 'fake-bin'
    fake.mkdir()
    marker = tmp_path / 'git-called'
    fake_git = fake / 'git'
    fake_git.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 99\n')
    fake_git.chmod(0o755)
    env['PATH'] = f"{fake}:{env['PATH']}"
    result = run(str(wrapper), '--active', '123', cwd=tmp_path, env=env, check=False)
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert receipt.with_suffix('.sha').read_text().strip() == sha


def test_active_ignores_hostile_git_config(selector_fixture, tmp_path):
    wrapper, manifest, control, sha, receipt, env, _controls = selector_fixture
    active(manifest, sha, control)
    marker = tmp_path / 'fsmonitor-called'
    helper = tmp_path / 'fsmonitor-helper'
    helper.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 0\n')
    helper.chmod(0o755)
    git(control, 'config', 'core.fsmonitor', str(helper))
    env |= {
        'GIT_CONFIG_COUNT': '1',
        'GIT_CONFIG_KEY_0': 'core.fsmonitor',
        'GIT_CONFIG_VALUE_0': str(helper),
    }
    result = run(str(wrapper), '--active', '123', cwd=tmp_path, env=env, check=False)
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert receipt.with_suffix('.sha').read_text().strip() == sha


def test_active_selects_exact_control_from_arbitrary_cwd(selector_fixture, tmp_path):
    wrapper, manifest, control, sha, receipt, env, _controls = selector_fixture
    active(manifest, sha, control)
    stale = tmp_path / 'stale-primary'
    stale.mkdir()
    (stale / 'switchstand-start').write_text('candidate sentinel')
    result = run(
        str(wrapper),
        '--active',
        '123',
        '--commit',
        'a' * 40,
        cwd=stale,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert receipt.with_suffix('.sha').read_text().strip() == sha
    assert receipt.with_suffix('.path').read_text().strip() == str(control)
    assert receipt.with_suffix('.common').read_text().strip() == str(control / '.git')
    assert receipt.with_suffix('.cwd').read_text().strip() == str(stale)
    assert receipt.with_suffix('.args').read_text().splitlines() == [
        '--active',
        '123',
        '--commit',
        'a' * 40,
    ]
    assert f'SWITCHSTAND_CONTROL_SHA={sha}' in result.stderr
    assert f'SWITCHSTAND_CONTROL_PATH={control}' in result.stderr


@pytest.mark.parametrize(
    "problem",
    ["malformed", "path-sha-mismatch", "wrong-head", "dirty", "outside-root", "wrong-repository"],
)
def test_active_fails_closed_for_invalid_control(selector_fixture, tmp_path, problem):
    wrapper, manifest, control, sha, receipt, env, controls = selector_fixture
    if problem == 'malformed':
        manifest.write_text(
            'state=ACTIVE\n'
            'repository=marcogallotta/switchstand\n'
            f'control_sha={sha}\n'
            f'control_path={control}\n'
            'extra=no\n'
        )
    elif problem == "path-sha-mismatch":
        active(manifest, "b" * 40, control)
    elif problem == "wrong-head":
        wrong_sha = "b" * 40
        wrong_control = controls / wrong_sha
        control.rename(wrong_control)
        active(manifest, wrong_sha, wrong_control)
    elif problem == 'dirty':
        active(manifest, sha, control)
        (control / 'dirty').write_text('x')
    elif problem == 'outside-root':
        outside = tmp_path / 'outside'
        run('git', 'clone', '-q', '--no-hardlinks', str(control), str(outside))
        git(
            outside,
            'remote',
            'set-url',
            'origin',
            'https://github.com/marcogallotta/switchstand.git',
        )
        git(outside, 'checkout', '-q', '--detach', sha)
        active(manifest, sha, outside)
    else:
        active(manifest, sha, control, repository='someone/else')
    result = run(str(wrapper), '--active', '123', cwd=tmp_path, env=env, check=False)
    assert result.returncode == 1
    assert not receipt.with_suffix('.sha').exists()


def test_paused_manifest_is_closed(selector_fixture, tmp_path):
    wrapper, manifest, control, sha, receipt, env, _controls = selector_fixture
    manifest.write_text(
        'state=PAUSED\n'
        'repository=marcogallotta/switchstand\n'
        f'control_sha={sha}\n'
        f'control_path={control}\n'
    )
    result = run(str(wrapper), cwd=tmp_path, env=env, check=False)
    assert result.returncode == 1
    assert 'PAUSED CONTROL manifest must contain only state' in result.stderr
    assert not receipt.with_suffix('.sha').exists()
