import os
import stat
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from switchstand.launch import readback
from switchstand.qualification import (
    PROFILE,
    TOOLS,
    QualificationBoundary,
    _inspect_tools,
    boundary_environment,
    cleanup_boundary,
    codex_command,
    parser,
    prepare_boundary,
    qualification_environment,
    validate_candidate,
    validate_request,
)

CANDIDATE = "a" * 40
ACCEPTED = "b" * 40
ACTIVE = "1218433927383387"
APPROVED = ("1218433805924373", "1218431674737116")


def completed(command, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=stderr)


def test_qualification_is_disabled_by_default_and_exposes_only_six_tools():
    config = tomllib.loads((Path(__file__).parents[1] / ".codex/config.toml").read_text())
    profile = config["permissions"][PROFILE]
    assert profile["extends"] == ":read-only"
    assert profile["network"]["enabled"] is False
    server = config["mcp_servers"]["switchstand_qualification"]
    assert server["enabled"] is False and server["required"] is False
    assert set(server["enabled_tools"]) == TOOLS
    assert all(server["tools"][name]["approval_mode"] == "approve" for name in TOOLS)


def test_parser_requires_exact_candidate_active_and_approved_reference():
    parsed = parser().parse_args(
        ["--candidate", CANDIDATE, "--active", ACTIVE, "--approved", APPROVED[0]]
    )
    assert parsed.candidate == CANDIDATE and parsed.active == ACTIVE
    assert parsed.approved == [APPROVED[0]]
    for arguments in (
        ["--candidate", "a" * 39, "--active", ACTIVE, "--approved", APPROVED[0]],
        ["--candidate", CANDIDATE, "--active", ACTIVE],
    ):
        with pytest.raises(SystemExit):
            parser().parse_args(arguments)


def test_request_rejects_duplicate_or_active_approved_references():
    for approved in ((), (ACTIVE,), (APPROVED[0], APPROVED[0]), APPROVED * 5):
        with pytest.raises(ValueError):
            validate_request(CANDIDATE, ACTIVE, approved)


def test_qualification_environment_strips_all_credentials_and_stale_handles():
    source = {
        "PATH": "/bin",
        "HOME": "/home/test",
        "ASANA_TOKEN": "secret",
        "DATABASE_URL": "production",
        "TEST_DATABASE_URL": "test",
        "DOCKER_HOST": "remote",
        "ACTIVE_WORK_ID": "stale",
        "SWITCHSTAND_QUALIFICATION_IMAGE": "stale",
    }
    assert qualification_environment(source) == {"PATH": "/bin", "HOME": "/home/test"}


def test_prepare_pins_detached_candidate_and_disposable_database(monkeypatch, tmp_path):
    primary = tmp_path / "primary"
    primary.mkdir()
    docker_commands = []

    def fake_git(repo, *arguments, env, check=True):
        command = list(arguments)
        if command[:2] == ["worktree", "add"]:
            target = Path(command[-2])
            target.mkdir()
            for name in (
                "scripts/chatgpt-mcp-canary.py",
                "src/switchstand/chatgpt_mcp.py",
                "migrations/versions/0002_grants_and_effects.py",
            ):
                path = target / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("candidate")
            return completed(command)
        if command[:2] == ["rev-parse", "--path-format=absolute"]:
            return completed(command, stdout=f"{primary}\n{ACCEPTED}\n{ACCEPTED}\nmain\n")
        if command == ["rev-parse", "HEAD"]:
            return completed(command, stdout=CANDIDATE + "\n")
        if command == ["branch", "--show-current"]:
            return completed(command, stdout="")
        return completed(command)

    def fake_docker(arguments, env, cwd=None, check=True):
        docker_commands.append((arguments, cwd))
        if arguments[:2] == ["image", "inspect"]:
            return completed(arguments, stdout=f"sha256:image {CANDIDATE}\n")
        return completed(arguments)

    monkeypatch.setattr("switchstand.qualification._git", fake_git)
    monkeypatch.setattr("switchstand.qualification._docker", fake_docker)
    monkeypatch.setattr("switchstand.qualification.validate_host_config", lambda env: tmp_path)
    monkeypatch.setattr("switchstand.qualification.tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr("switchstand.qualification.os.getpid", lambda: 123)
    boundary = prepare_boundary(primary, CANDIDATE, ACTIVE, APPROVED, {"HOME": str(tmp_path)})
    build = next(arguments for arguments, _ in docker_commands if arguments[0] == "build")
    database = next(arguments for arguments, _ in docker_commands if arguments[:2] == ["run", "-d"])
    network = next(arguments for arguments, _ in docker_commands if arguments[:2] == ["network", "create"])
    assert build[build.index("--label") + 1] == f"org.opencontainers.image.revision={CANDIDATE}"
    assert boundary.candidate == CANDIDATE
    assert "--tmpfs" in database and "POSTGRES_DB=switchstand_test" in database
    assert "--internal" not in network
    assert boundary.image == "sha256:image"


def test_candidate_must_descend_from_the_accepted_main(monkeypatch, tmp_path):
    def fake_git(repo, *arguments, env, check=True):
        return completed(arguments, returncode=1 if "--is-ancestor" in arguments else 0)

    monkeypatch.setattr("switchstand.qualification._git", fake_git)
    with pytest.raises(RuntimeError, match="based on accepted main"):
        validate_candidate(tmp_path, tmp_path / "candidate", CANDIDATE, ACCEPTED, {})


def boundary(tmp_path):
    candidate_repo = tmp_path / "candidate"
    candidate_repo.mkdir()
    return QualificationBoundary(
        tmp_path,
        candidate_repo,
        CANDIDATE,
        ACCEPTED,
        "sha256:image",
        "switchstand-qualification:test",
        "network",
        "database",
        "server",
        ACTIVE,
        APPROVED,
    )


def test_cleanup_refuses_to_remove_a_dirty_candidate(monkeypatch, tmp_path):
    subject = boundary(tmp_path)
    monkeypatch.setattr(
        "switchstand.qualification._docker",
        lambda arguments, env, cwd=None, check=True: completed(arguments, returncode=1,
                                                               stderr="not found"),
    )
    monkeypatch.setattr(
        "switchstand.qualification.validate_candidate",
        lambda *args: (_ for _ in ()).throw(RuntimeError("candidate is dirty")),
    )
    monkeypatch.setattr(
        "switchstand.qualification._git",
        lambda repo, *arguments, env, check=True: completed(
            arguments, stdout=str(subject.candidate_repo) if arguments[:2] == ("worktree", "list") else ""
        ),
    )
    with pytest.raises(RuntimeError, match="cleanup failed.*candidate is dirty"):
        cleanup_boundary(subject, {})
    assert subject.candidate_repo.exists()


def test_cleanup_removes_only_exact_resources_and_clean_worktree(monkeypatch, tmp_path):
    subject = boundary(tmp_path)
    docker_commands = []
    git_commands = []

    def fake_docker(arguments, env, cwd=None, check=True):
        docker_commands.append(arguments)
        return completed(arguments, returncode=1 if "inspect" in arguments else 0,
                         stderr="not found" if "inspect" in arguments else "")

    def fake_git(repo, *arguments, env, check=True):
        git_commands.append(arguments)
        if arguments[:2] == ("worktree", "remove"):
            subject.candidate_repo.rmdir()
        return completed(arguments)

    monkeypatch.setattr("switchstand.qualification._docker", fake_docker)
    monkeypatch.setattr("switchstand.qualification._git", fake_git)
    monkeypatch.setattr("switchstand.qualification.validate_candidate", lambda *args: None)
    cleanup_boundary(subject, {})
    assert docker_commands == [
        ["rm", "-f", "server"],
        ["container", "inspect", "server"],
        ["rm", "-f", "database"],
        ["container", "inspect", "database"],
        ["network", "rm", "network"],
        ["network", "inspect", "network"],
        ["image", "rm", "switchstand-qualification:test"],
        ["image", "inspect", "switchstand-qualification:test"],
    ]
    assert ("worktree", "remove", str(subject.candidate_repo)) in git_commands
    assert not subject.candidate_repo.exists()


class FakeClient:
    def __init__(self, _parameters):
        self.active = "00000000-0000-0000-0000-000000000001"
        self.references = (
            "00000000-0000-0000-0000-000000000002",
            "00000000-0000-0000-0000-000000000003",
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def list_tools(self):
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name=name,
                    input_schema={"additionalProperties": False, "properties": {}},
                )
                for name in TOOLS
            ]
        )

    async def call_tool(self, name, arguments):
        if name == "grant_get":
            content = {
                "status": "ok",
                "principal": {"assurance": "test", "subject": ACTIVE},
                "grant": {
                    "operations": ["work_get", "work_append"],
                    "append_qualification": f"test:disposable-task:{ACTIVE}",
                    "authority": {
                        "active_work_id": self.active,
                        "reference_work_ids": list(self.references),
                    }
                },
            }
        else:
            selected = arguments.get("work_id", self.active)
            task = (ACTIVE, *APPROVED)[(self.active, *self.references).index(selected)]
            content = {"status": "ok", "item": {"id": selected, "source": {"task_gid": task}}}
        return SimpleNamespace(structured_content=content)


async def test_tool_readback_proves_exact_work_source_separation(monkeypatch, tmp_path):
    monkeypatch.setattr("switchstand.qualification.Client", FakeClient)
    result = await _inspect_tools(tmp_path / "wrapper", {"HOME": str(tmp_path)}, ACTIVE, APPROVED)
    assert result.active_work_id != ACTIVE
    assert set(result.reference_work_ids).isdisjoint(APPROVED)


def test_profile_readback_accepts_only_read_only_network_off(monkeypatch):
    sources = [str(Path.home() / ".codex/AGENTS.md"), "/repo/AGENTS.md"]
    messages = [
        {"id": 2, "result": {"data": [{"id": PROFILE, "allowed": True}]}},
        {
            "id": 3,
            "result": {
                "activePermissionProfile": {"id": PROFILE},
                "sandbox": {"type": "readOnly", "networkAccess": False},
                "approvalPolicy": "never",
                "instructionSources": sources,
            },
        },
    ]
    monkeypatch.setattr("switchstand.launch._rpc_messages", lambda repo, env, profile: messages)
    assert readback(Path("/repo"), {}, profile=PROFILE, sandbox="readOnly").profile == PROFILE


def test_codex_client_disables_other_servers_and_accepts_no_options():
    command = codex_command(Path("/main"), CANDIDATE, "inspect only")
    assert "mcp_servers.switchstand.enabled=false" in command
    assert "mcp_servers.switchstand_development.enabled=false" in command
    assert "mcp_servers.switchstand_qualification.enabled=true" in command
    assert "mcp_servers.switchstand_qualification.required=true" in command
    assert "danger-full-access" not in " ".join(command)


def test_wrapper_keeps_token_file_server_side_and_enforces_test_database(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$FAKE_DOCKER_ARGS\"\n")
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    config = tmp_path / ".config/switchstand/.env"
    config.parent.mkdir(parents=True)
    config.write_text("ASANA_TOKEN=secret-that-must-not-be-read-by-the-client\n")
    config.chmod(0o600)
    output = tmp_path / "docker.args"
    script = Path(__file__).parents[1] / "scripts/switchstand-qualification-mcp"
    environment = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_DOCKER_ARGS": str(output),
        "SWITCHSTAND_QUALIFICATION": "1",
        "SWITCHSTAND_QUALIFICATION_IMAGE": "sha256:image",
        "SWITCHSTAND_QUALIFICATION_NETWORK": "network",
        "SWITCHSTAND_QUALIFICATION_DATABASE": "database",
        "SWITCHSTAND_QUALIFICATION_SERVER": "server",
        "SWITCHSTAND_QUALIFICATION_ACTIVE": ACTIVE,
        "SWITCHSTAND_QUALIFICATION_APPROVED": ",".join(APPROVED),
    }
    result = subprocess.run([script], env=environment, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    arguments = output.read_text().splitlines()
    assert str(config) in arguments
    assert "secret-that-must-not-be-read-by-the-client" not in "\n".join(arguments)
    assert "DATABASE_URL=" in arguments
    assert any(value.endswith("/switchstand_test") for value in arguments)
    assert any("@database/switchstand_test" in value for value in arguments)
    assert arguments[-5:] == [ACTIVE, "--reference", APPROVED[0], "--reference", APPROVED[1]]


def test_boundary_environment_never_contains_a_database_url_or_token(tmp_path):
    subject = boundary(tmp_path)
    env = boundary_environment(
        {"HOME": "/home/test", "ASANA_TOKEN": "secret", "TEST_DATABASE_URL": "wrong"}, subject
    )
    assert "ASANA_TOKEN" not in env and "TEST_DATABASE_URL" not in env
    assert env["SWITCHSTAND_QUALIFICATION_IMAGE"] == "sha256:image"
