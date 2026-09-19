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


async def test_provisioner_rejects_invalid_trusted_test_project(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused:unused@localhost/unused")
    monkeypatch.setenv("ASANA_TOKEN", "unused")
    monkeypatch.setenv("SWITCHSTAND_TEST_PROJECT_GID", "invalid")
    with pytest.raises(ValueError, match="invalid test project GID"):
        await provision.run("123", ())


def test_managed_agent_provisioning_is_explicit():
    args = provision.parser().parse_args(["--active", "123", "--managed-agent"])
    assert args.managed_agent is True


def test_default_provisioning_does_not_issue_managed_agent_grant():
    args = provision.parser().parse_args(["--active", "123"])
    assert args.managed_agent is False
