import os
import shutil
import subprocess
from pathlib import Path

import pytest

from switchstand import control_release

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
    assert result.returncode == 1
    assert not marker.exists()
    assert not receipt.with_suffix('.sha').exists()


def test_active_rejects_local_clean_filter_helper(selector_fixture, tmp_path):
    wrapper, manifest, control, sha, receipt, env, _controls = selector_fixture
    active(manifest, sha, control)
    marker = tmp_path / 'clean-filter-called'
    helper = tmp_path / 'clean-filter-helper'
    helper.write_text(f'#!/bin/sh\ntouch "{marker}"\n/bin/cat\n')
    helper.chmod(0o755)
    attributes = tmp_path / 'attributes'
    attributes.write_text('scripts/switchstand-start filter=evil\n')
    git(control, 'config', 'core.attributesFile', str(attributes))
    git(control, 'config', 'filter.evil.clean', str(helper))
    tracked = control / 'scripts' / 'switchstand-start'
    stat = tracked.stat()
    os.utime(tracked, (stat.st_atime, stat.st_mtime + 5))
    result = run(str(wrapper), '--active', '123', cwd=tmp_path, env=env, check=False)
    assert result.returncode == 1
    assert not marker.exists()
    assert not receipt.with_suffix('.sha').exists()


def test_active_rejects_local_core_worktree_redirection(selector_fixture, tmp_path):
    wrapper, manifest, control, sha, receipt, env, _controls = selector_fixture
    active(manifest, sha, control)
    alternate = tmp_path / 'alternate-worktree'
    (alternate / 'scripts').mkdir(parents=True)
    shutil.copy2(control / 'scripts' / 'switchstand-start', alternate / 'scripts' / 'switchstand-start')
    git(control, 'config', 'core.worktree', str(alternate))
    marker = tmp_path / 'redirected-control-executed'
    (control / 'scripts' / 'switchstand-start').write_text(
        f'#!/bin/sh\ntouch "{marker}"\nexit 0\n'
    )
    result = run(str(wrapper), '--active', '123', cwd=tmp_path, env=env, check=False)
    assert result.returncode == 1
    assert not marker.exists()
    assert not receipt.with_suffix('.sha').exists()


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


def test_control_release_stages_selects_and_preserves_legacy_rollback(
    selector_fixture, tmp_path, monkeypatch
):
    wrapper, manifest, control_a, sha_a, receipt, env, controls = selector_fixture
    monkeypatch.setenv('HOME', env['HOME'])
    origin = tmp_path / 'origin'
    git(origin, 'branch', '-M', 'main')
    active(manifest, sha_a, control_a)
    internal = origin / 'scripts/switchstand-start'
    internal.write_text(internal.read_text() + '# release B\n')
    git(origin, 'add', 'scripts/switchstand-start')
    git(origin, 'commit', '-qm', 'release B')
    sha_b = git(origin, 'rev-parse', 'HEAD')
    monkeypatch.setattr(control_release, 'REMOTE', str(origin))
    canonical = 'https://github.com/marcogallotta/switchstand.git'

    marker = tmp_path / 'legacy-selector-executed'
    installed = wrapper.read_bytes()
    wrapper.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
    with pytest.raises(RuntimeError, match='installed selector does not match'):
        control_release.activate(sha_b)
    assert not marker.exists()
    wrapper.write_bytes(installed)

    redirected = tmp_path / 'redirected-controls'
    controls.rename(redirected)
    controls.symlink_to(redirected, target_is_directory=True)
    with pytest.raises(RuntimeError, match='CONTROL root is missing, redirected'):
        control_release.activate(sha_b)
    assert not tuple(redirected.glob('.stage-*'))
    controls.unlink()
    redirected.rename(controls)

    invalid = manifest.parent / '.invalid'
    invalid.write_text('invalid\n')
    invalid.chmod(0o600)
    mode = control_release.MODE
    assert run(str(wrapper), mode, str(invalid), env=env, check=False).returncode == 1
    assert run(str(wrapper), mode, str(manifest), 'extra', env=env, check=False).returncode == 1
    assert not receipt.with_suffix('.sha').exists()

    assert control_release.activate(sha_b) == 'APPLIED'
    control_b = controls / sha_b
    assert git(control_b, 'branch', '--show-current') == ''
    assert git(control_b, 'remote', 'get-url', 'origin') == canonical
    assert not receipt.with_suffix('.sha').exists()
    assert run(str(wrapper), env=env).returncode == 0
    assert receipt.with_suffix('.sha').read_text().strip() == sha_b
    active(manifest, sha_a, control_a)
    assert run(str(wrapper), env=env).returncode == 0
    assert receipt.with_suffix('.sha').read_text().strip() == sha_a

    manifest.chmod(0o600)
    real_replace = control_release.atomic_replace_bytes

    def replace_then_fail(path, body):
        real_replace(path, body)
        raise OSError('parent fsync outcome unavailable')

    monkeypatch.setattr(control_release, 'atomic_replace_bytes', replace_then_fail)
    with pytest.raises(RuntimeError, match='APPLIED_WITH_DURABILITY_UNKNOWN'):
        control_release.activate(sha_b)
    active(manifest, sha_a, control_a)
    manifest.chmod(0o600)
    monkeypatch.setattr(control_release, 'atomic_replace_bytes', real_replace)
    real_read = control_release.read_private_bytes

    def unreadable_readback(_path):
        raise ValueError('unreadable readback')

    monkeypatch.setattr(control_release, 'read_private_bytes', unreadable_readback)
    with pytest.raises(RuntimeError, match='UNKNOWN'):
        control_release.activate(sha_b)
    monkeypatch.setattr(control_release, 'read_private_bytes', real_read)
    active(manifest, sha_a, control_a)
    real_unlink = Path.unlink

    def reject_prospective_cleanup(path, *args, **kwargs):
        if path.name.startswith('.prospective-'):
            raise OSError('cleanup failed')
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'unlink', reject_prospective_cleanup)
    with pytest.raises(OSError, match='cleanup failed'):
        control_release.activate(sha_b)
    assert manifest.read_text().startswith(f'state=ACTIVE\nrepository=marcogallotta/switchstand\ncontrol_sha={sha_a}\n')
    monkeypatch.setattr(Path, 'unlink', real_unlink)

    def cleanup_then_move(path, *args, **kwargs):
        internal.write_text(internal.read_text() + '# release C\n')
        git(origin, 'add', 'scripts/switchstand-start')
        git(origin, 'commit', '-qm', 'release C')
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'unlink', cleanup_then_move)
    with pytest.raises(RuntimeError, match='remote main moved before selection'):
        control_release.activate(sha_b)
    assert manifest.read_text().startswith(
        f'state=ACTIVE\nrepository=marcogallotta/switchstand\ncontrol_sha={sha_a}\n'
    )
