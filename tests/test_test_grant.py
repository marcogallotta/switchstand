import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from switchstand import test_grant


@pytest.mark.parametrize("url", [
    "", "postgresql+psycopg:///production",
    "postgresql+psycopg:///switchstand_test?dbname=production",
    "postgresql+psycopg:///switchstand_test?dbname=switchstand_test&dbname=production",
    "sqlite+aiosqlite:///switchstand_test",
])
def test_database_target_cannot_be_overridden(url):
    with pytest.raises(ValueError, match="switchstand_test"):
        test_grant.test_database_url({"TEST_DATABASE_URL": url})


def test_database_target_reaches_driver():
    url = test_grant.test_database_url({
        "TEST_DATABASE_URL": "postgresql+psycopg:///switchstand_test"
    })
    engine = create_async_engine(url)
    _, options = engine.dialect.create_connect_args(engine.url)
    assert options["dbname"] == "switchstand_test"


def test_authenticated_qualification_grant_mode_is_explicit():
    args = test_grant.parser().parse_args([
        "--issuer", "https://switchstand.example.com/",
        "--subject", "192548", "--client-id", "chatgpt-client",
        "--assurance", "authenticated", "inspect",
    ])
    assert args.assurance == "authenticated"


def test_workspace_qualification_grant_mode_is_explicit():
    args = test_grant.parser().parse_args([
        "--issuer", "https://switchstand.example.com/",
        "--subject", "192548", "--client-id", "chatgpt-client",
        "--assurance", "authenticated", "--scope", "workspace", "inspect",
    ])
    assert args.scope == "workspace"


def test_ordinary_certification_profile_is_explicit_and_complete():
    args = test_grant.parser().parse_args([
        "--issuer", "https://switchstand.example.com/",
        "--subject", "192548", "--client-id", "codex-client",
        "--assurance", "authenticated", "--scope", "workspace",
        "--profile", "ordinary-certification", "set", "--task", "100",
        "--test-project", "200", "--expected-version", "0",
        "--ttl-seconds", "60", "--qualification", "test:native-cert",
    ])
    operations, append_qualification, update_qualification = test_grant.grant_permissions(args)
    assert operations == {
        "work_get", "work_search", "work_create", "work_append", "work_update", "message",
    }
    assert append_qualification == update_qualification == "certification:native-cert"


@pytest.mark.parametrize("flags", [
    ["--assurance", "test", "--scope", "workspace"],
    ["--assurance", "authenticated", "--scope", "launch"],
])
def test_ordinary_certification_profile_requires_authenticated_workspace(flags):
    args = test_grant.parser().parse_args([
        "--issuer", "https://switchstand.example.com/",
        "--subject", "192548", "--client-id", "codex-client", *flags,
        "--profile", "ordinary-certification", "set", "--task", "100",
        "--test-project", "200", "--expected-version", "0",
        "--ttl-seconds", "60", "--qualification", "test:native-cert",
    ])
    with pytest.raises(ValueError, match="authenticated workspace"):
        test_grant.grant_permissions(args)
