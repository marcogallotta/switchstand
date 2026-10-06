import argparse
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from switchstand import provision
from switchstand.canonical_work import CurrentWork
from switchstand.task_ref import asana_task_id


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1218242783900077", "1218242783900077"),
        ("https://app.asana.com/0/0/1218242783900077/f", "1218242783900077"),
        ("https://app.asana.com/0/123/1218242783900077", "1218242783900077"),
        (
            "https://app.asana.com/1/123/project/456/task/1218242783900077",
            "1218242783900077",
        ),
    ],
)
def test_asana_task_id_accepts_ids_and_task_urls(value, expected):
    assert asana_task_id(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "not-an-id",
        "http://app.asana.com/0/0/1218242783900077/f",
        "https://example.com/0/0/1218242783900077/f",
        "https://app.asana.com/0/0/not-an-id/f",
    ],
)
def test_asana_task_id_rejects_ambiguous_input(value):
    with pytest.raises(ValueError):
        asana_task_id(value)


def test_stale_schema_fails_before_provider_effect(monkeypatch):
    monkeypatch.setattr(
        provision, "require_current_schema", lambda: (_ for _ in ()).throw(RuntimeError("stale"))
    )
    called = []
    monkeypatch.setattr(provision.asyncio, "run", lambda coroutine: called.append(coroutine))
    monkeypatch.setattr(provision, "parser", lambda: type("Parser", (), {
        "parse_args": lambda self: argparse.Namespace(active="123", reference=[]),
        "exit": lambda self, status, message: (_ for _ in ()).throw(SystemExit(status)),
    })())
    with pytest.raises(SystemExit):
        provision.main()
    assert called == []


def test_controller_boundary_redacts_unexpected_exception_with_container_secret(monkeypatch):
    messages = []
    secret = "controller-only-value"
    monkeypatch.setenv("CONTROLLER_SECRET", secret)
    monkeypatch.setattr(
        provision, "require_current_schema",
        lambda: (_ for _ in ()).throw(Exception(f"database exploded with {secret}")),
    )
    monkeypatch.setattr(provision, "parser", lambda: type("Parser", (), {
        "parse_args": lambda self: argparse.Namespace(active="123", reference=[],
                                                       managed_agent=True, repository=False),
        "exit": lambda self, status, message: (
            messages.append(message), (_ for _ in ()).throw(SystemExit(status))
        )[1],
    })())
    with pytest.raises(SystemExit):
        provision.main()
    assert secret not in messages[0]
    assert "[redacted]" in messages[0]


def test_managed_controller_checks_schema_without_upgrading_it():
    compose = (Path(__file__).parents[1] / "compose.yaml").read_text()
    managed = compose.split('if [ "$${SWITCHSTAND_MANAGED:-}" != 1 ]; then', 1)[1]
    assert "require_current_schema; require_current_schema()" in managed
    assert "alembic upgrade" not in managed
    assert "ASANA_TOKEN" not in managed
    assert 'ACTIVE_WORK_ID:?required' in managed


def test_development_image_contains_repository_assets_read_by_tests():
    root = Path(__file__).parents[1]
    assert (root / "compose.yaml").is_file()


@pytest.mark.parametrize("notes", ["", "SWITCHSTAND_REPOSITORY=other/repo",
    "SWITCHSTAND_REPOSITORY=marcogallotta/ai-tools"])
async def test_repository_admission_uses_canonical_rows_before_grant(monkeypatch, capsys, notes):
    active = UUID("00000000-0000-0000-0000-000000000001")
    reference = UUID("00000000-0000-0000-0000-000000000002")
    events = []

    class Engine:
        async def dispose(self):
            events.append("dispose")

    engine = Engine()

    class Works:
        def __init__(self, value):
            assert value is engine

        async def asana_gids(self, work_id):
            assert work_id == active
            return ("123",)

    async def current(_works, value):
        events.append(f"read:{value}")
        work_id = active if value == "123" else reference
        return CurrentWork(work_id, value, False, notes if value == "123" else "")

    async def rotate(*args):
        events.append("grant")

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused:unused@localhost/unused")
    monkeypatch.delenv("ASANA_TOKEN", raising=False)
    monkeypatch.setattr(provision, "create_async_engine", lambda _: engine)
    monkeypatch.setattr(provision, "CanonicalWorkRepository", Works)
    monkeypatch.setattr(provision, "canonical_work", current)
    monkeypatch.setattr(provision, "rotate_managed_grant", rotate)
    valid = notes.endswith("marcogallotta/ai-tools")
    if valid:
        await provision.run("123", ("456",), managed_agent=True, repository=True)
        output = capsys.readouterr().out
        assert f"ACTIVE_WORK_ID={active}" in output
        assert f"REFERENCE_WORK_IDS={reference}" in output
        assert "LEGACY_TASK_GIDS=123" in output
        assert events == ["read:123", "read:456", "grant", "dispose"]
    else:
        with pytest.raises(ValueError):
            await provision.run("123", (), managed_agent=True, repository=True)
        assert events == ["read:123", "dispose"]
        assert "ACTIVE_WORK_ID=" not in capsys.readouterr().out


async def test_provisioner_rejects_duplicate_canonical_work(monkeypatch):
    work = CurrentWork(UUID("00000000-0000-0000-0000-000000000001"), "Task", False, "")

    class Engine:
        async def dispose(self):
            pass

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused")
    monkeypatch.setattr(provision, "create_async_engine", lambda _: Engine())
    monkeypatch.setattr(provision, "CanonicalWorkRepository", lambda _: object())
    monkeypatch.setattr(provision, "canonical_work", lambda *_: _async_value(work))
    with pytest.raises(ValueError, match="distinct"):
        await provision.run("123", ("123",))


async def test_feature_opt_ins_are_atomic_with_managed_grant(monkeypatch, capsys):
    work_id = UUID("00000000-0000-4000-8000-000000000011")
    observed = []

    class Engine:
        async def dispose(self):
            pass

    class Works:
        def __init__(self, _engine):
            pass

        async def get(self, requested):
            assert requested == work_id
            return CurrentWork(work_id, "pilot", False, "")

        async def asana_gids(self, requested):
            assert requested == work_id
            return ()

    async def current(_works, value):
        assert value == str(work_id)
        return CurrentWork(work_id, "pilot", False, "")

    async def rotate(
        _grants, authority, *, agent_task=False, priority_claims=False,
    ):
        observed.append((authority.active_work_id, agent_task, priority_claims))
        return SimpleNamespace(id=UUID(int=19), version=4)

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused")
    monkeypatch.setenv("SWITCHSTAND_PRIORITY_CLAIMS", "1")
    monkeypatch.setattr(provision, "create_async_engine", lambda _: Engine())
    monkeypatch.setattr(provision, "CanonicalWorkRepository", Works)
    monkeypatch.setattr(provision, "canonical_work", current)
    monkeypatch.setattr(provision, "rotate_managed_grant", rotate)

    with pytest.raises(ValueError, match="managed-agent"):
        await provision.run(str(work_id), (), agent_task=True)
    await provision.run(
        str(work_id), (), managed_agent=True, agent_task=True
    )

    assert observed == [(work_id, True, True)]
    assert "MANAGED_GRANT_VERSION=4" in capsys.readouterr().out


async def test_exact_canonical_miss_uses_existing_grant_fallback_without_rotation(
    monkeypatch, capsys
):
    work_id = UUID("00000000-0000-4000-8000-000000000001")
    events = []

    class Engine:
        async def dispose(self):
            events.append("dispose")

    class Works:
        def __init__(self, _engine):
            pass

        async def get(self, requested):
            events.append(f"canonical:{requested}")

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def fallback(value, *_args):
        events.append(f"fallback:{value}")
        authority = provision.LaunchAuthority(active_work_id=work_id)
        return SimpleNamespace(
            identity=SimpleNamespace(provider_work_id="1218999999999999"),
            grant=SimpleNamespace(authority=authority, id=UUID(int=9), version=3),
        )

    async def rotate(*_args):
        events.append("rotated")

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused")
    monkeypatch.setenv("ASANA_TOKEN", "credential")
    monkeypatch.setattr(provision, "create_async_engine", lambda _: Engine())
    monkeypatch.setattr(provision, "CanonicalWorkRepository", Works)
    monkeypatch.setattr(provision.httpx, "AsyncClient", lambda **_: Client())
    monkeypatch.setattr(provision, "resolve_and_admit_pre_migration_launch", fallback)
    monkeypatch.setattr(provision, "rotate_managed_grant", rotate)

    await provision.run(str(work_id), (), managed_agent=True)

    assert events == [f"canonical:{work_id}", f"fallback:{work_id}", "dispose"]
    assert f"ACTIVE_WORK_ID={work_id}" in capsys.readouterr().out

    events.clear()
    with pytest.raises(ValueError, match="requires canonical work"):
        await provision.run(
            str(work_id), (), managed_agent=True, agent_task=True
        )
    assert events == [f"canonical:{work_id}", "dispose"]


async def _async_value(value):
    return value
