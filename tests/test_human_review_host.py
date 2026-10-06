import hashlib
import os
from pathlib import Path

import pytest

from switchstand.human_review_host import (
    SERVICE_NAME,
    HumanReviewHostAssets,
    render_service,
    systemd_unit,
)


def _assets(tmp_path: Path) -> HumanReviewHostAssets:
    runtime_root = tmp_path / "runtime"
    (runtime_root / "src/switchstand").mkdir(parents=True)
    runtime_python = tmp_path / "python"
    runtime_python.write_text("")
    runtime_python.chmod(0o755)
    environment = tmp_path / "human-review.env"
    environment.write_text("DATABASE_URL=redacted\n")
    environment.chmod(0o600)
    return HumanReviewHostAssets(runtime_python, runtime_root, environment, 8792)


def test_rendered_service_is_loopback_only_create_new_and_inert(tmp_path: Path) -> None:
    assets = _assets(tmp_path)
    output = tmp_path / "rendered"
    output.mkdir()

    receipt = render_service(assets, output)
    target = output / SERVICE_NAME
    content = target.read_text()

    assert content == systemd_unit(assets)
    assert "SWITCHSTAND_HUMAN_REVIEW_BIND_HOST=127.0.0.1" in content
    assert "SWITCHSTAND_HUMAN_REVIEW_BIND_PORT=8792" in content
    assert f"EnvironmentFile={assets.environment_file}" in content
    assert "switchstand.human_review_runtime" in content
    assert "systemctl" not in content
    assert receipt == {SERVICE_NAME: hashlib.sha256(content.encode()).hexdigest()}
    assert target.stat().st_mode & 0o777 == 0o644
    with pytest.raises(ValueError, match="refusing to replace"):
        render_service(assets, output)


def test_renderer_rejects_unsafe_paths_and_environment_file(tmp_path: Path) -> None:
    assets = _assets(tmp_path)
    output = tmp_path / "rendered"
    output.mkdir()
    assets.environment_file.chmod(0o644)
    with pytest.raises(ValueError, match="mode-0600"):
        render_service(assets, output)

    assets.environment_file.chmod(0o600)
    link = tmp_path / "linked.env"
    link.symlink_to(assets.environment_file)
    linked = HumanReviewHostAssets(
        assets.runtime_python, assets.runtime_root, link, assets.bind_port
    )
    with pytest.raises(ValueError, match="mode-0600"):
        render_service(linked, output)

    with pytest.raises(ValueError, match="absolute"):
        HumanReviewHostAssets(Path("python"), assets.runtime_root, assets.environment_file, 8792)
    with pytest.raises(ValueError, match="between"):
        HumanReviewHostAssets(
            assets.runtime_python, assets.runtime_root, assets.environment_file, 0
        )


def test_renderer_refuses_symlink_output_directory(tmp_path: Path) -> None:
    assets = _assets(tmp_path)
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)

    with pytest.raises(ValueError, match="absolute real directory"):
        render_service(assets, linked)

    assert os.listdir(actual) == []


@pytest.mark.parametrize("failure", ["write", "fsync"])
def test_renderer_cleans_staging_after_prepublication_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    assets = _assets(tmp_path)
    output = tmp_path / "rendered"
    output.mkdir()
    original = getattr(os, failure)
    if failure == "write":
        monkeypatch.setattr(os, failure, lambda *_args: 0)
    else:
        monkeypatch.setattr(os, failure, lambda *_args: (_ for _ in ()).throw(OSError("fault")))

    with pytest.raises(OSError):
        render_service(assets, output)
    assert os.listdir(output) == []

    monkeypatch.setattr(os, failure, original)
    render_service(assets, output)
    assert os.listdir(output) == [SERVICE_NAME]


def test_renderer_rejects_non_executable_python_and_systemd_metacharacters(
    tmp_path: Path,
) -> None:
    assets = _assets(tmp_path)
    output = tmp_path / "rendered"
    output.mkdir()
    assets.runtime_python.chmod(0o644)
    with pytest.raises(ValueError, match="real executable"):
        render_service(assets, output)

    for character in ('%', '"', '\\'):
        with pytest.raises(ValueError, match="unsupported systemd"):
            HumanReviewHostAssets(
                assets.runtime_python,
                Path(f"/runtime{character}unsafe"),
                assets.environment_file,
                assets.bind_port,
            )
