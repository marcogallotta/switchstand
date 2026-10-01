import argparse
from pathlib import Path

import pytest

from switchstand import provision
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


def test_development_image_contains_repository_assets_read_by_tests():
    root = Path(__file__).parents[1]
    assert (root / "compose.yaml").is_file()
    assert (root / "docs/cutover-asana-coverage.json").is_file()


async def test_provisioner_rejects_invalid_trusted_test_project(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused:unused@localhost/unused")
    monkeypatch.setenv("ASANA_TOKEN", "unused")
    monkeypatch.setenv("SWITCHSTAND_TEST_PROJECT_GID", "invalid")
    with pytest.raises(ValueError, match="invalid test project GID"):
        await provision.run("123", ())


@pytest.mark.parametrize("status,notes", [("denied", ""), ("ok", ""),
    ("ok", "SWITCHSTAND_REPOSITORY=other/repo"),
    ("ok", "SWITCHSTAND_REPOSITORY=marcogallotta/ai-tools")])
async def test_repository_admission_reads_current_work_before_grant(monkeypatch, capsys, status, notes):
    from types import SimpleNamespace
    from uuid import UUID
    active = UUID("00000000-0000-0000-0000-000000000001")
    authority = SimpleNamespace(active_work_id=active, reference_work_ids=())
    events = []
    async def admit(*args):
        events.append("provision")
        return authority
    class Current:
        def __init__(self, bound, state, providers):
            assert bound is authority
        async def get(self, request):
            assert request.work_id == active and request.api_version == "1"
            events.append("current")
            return SimpleNamespace(status=status, item=SimpleNamespace(notes=notes))
    async def rotate(*args):
        events.append("grant")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused:unused@localhost/unused")
    monkeypatch.setenv("ASANA_TOKEN", "unused")
    monkeypatch.delenv("SWITCHSTAND_TEST_PROJECT_GID", raising=False)
    monkeypatch.setattr(provision, "provision_launch", admit)
    monkeypatch.setattr(provision, "Controller", Current)
    monkeypatch.setattr(provision, "rotate_managed_grant", rotate)
    valid = status == "ok" and notes.endswith("marcogallotta/ai-tools")
    if valid:
        await provision.run("123", (), managed_agent=True, repository=True)
        assert events == ["provision", "current", "grant"]
        assert f"ACTIVE_WORK_ID={active}" in capsys.readouterr().out
    else:
        with pytest.raises(ValueError):
            await provision.run("123", (), managed_agent=True, repository=True)
        assert events == ["provision", "current"]
        assert "ACTIVE_WORK_ID=" not in capsys.readouterr().out
