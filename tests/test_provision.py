import argparse
from pathlib import Path
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


async def _async_value(value):
    return value
