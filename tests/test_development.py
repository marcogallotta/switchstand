import subprocess

from switchstand import development


def completed(args=(), stdout="", returncode=0):
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")


def tool(name):
    found = development.build_server()._tool_manager.get_tool(name)
    assert found is not None
    return found.fn


def test_development_surface_is_closed():
    server = development.build_server()
    assert set(server._tool_manager._tools) == {
        "quality", "commit_all_current_worktree", "run_status"}
    for item in server._tool_manager._tools.values():
        assert item.parameters.get("additionalProperties") is False


async def test_quality_uses_only_pinned_image_and_read_only_worktree(monkeypatch, tmp_path):
    monkeypatch.setattr(development, "_bound_repo", lambda: (tmp_path, "owned", "a" * 40))
    monkeypatch.setattr(development, "_manifest", lambda repo: "manifest")
    monkeypatch.setattr(development, "_git", lambda repo, *args: completed(stdout=("a" * 40 + "\n")
                                                                        if "rev-parse" in args else ""))
    monkeypatch.setenv("SWITCHSTAND_MANIFEST_SHA256", "manifest")
    monkeypatch.setenv("SWITCHSTAND_QUALITY_IMAGE", "sha256:fixed")
    monkeypatch.setenv("SWITCHSTAND_QUALITY_NETWORK", "isolated")
    captured = {}
    monkeypatch.setattr(development, "_quality",
                        lambda command: captured.setdefault("run", completed(command)))
    result = await tool("quality")("a" * 40)
    command = captured["run"].args
    assert result.status == "ok"
    assert "sha256:fixed" in command and f"{tmp_path}:/workspace:ro" in command
    assert "PYTHONPATH=/workspace/src" in " ".join(command)
    assert "compose.yaml" not in " ".join(command) and "Dockerfile" not in " ".join(command)


def test_development_environment_removes_credentials(monkeypatch):
    monkeypatch.setenv("ASANA_TOKEN", "secret")
    monkeypatch.setenv("GIT_CONFIG", "danger")
    monkeypatch.setenv("PATH", "/bin")
    clean = development._environment()
    assert clean["PATH"] == "/bin"
    assert "ASANA_TOKEN" not in clean and "GIT_CONFIG" not in clean


def test_credential_path_covers_environment_variants():
    assert all(development._credential_path(name)
               for name in (".env", ".env.local", "service.env.production"))
    assert not development._credential_path("environment.md")
    assert not development._credential_path("switchstand-config.example")


async def test_run_status_is_bound_to_owned_worktree(monkeypatch, tmp_path):
    monkeypatch.setattr(development, "_bound_repo", lambda: (tmp_path, "owned", "a" * 40))
    monkeypatch.setattr(
        development, "_git", lambda repo, *args: completed(stdout=str(tmp_path) + "\n")
    )
    captured = {}
    monkeypatch.setattr(
        development,
        "inspect_receipt",
        lambda path, repo, branch: captured.setdefault(
            "call", development.RunStatus(status="stopped")
        ),
    )
    result = await tool("run_status")()
    assert result.status == "stopped"
    assert captured["call"].status == "stopped"
