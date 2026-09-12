import time

import pytest
from mcp.server.auth.provider import AccessToken

from switchstand.principal import RequestPrincipal


@pytest.mark.parametrize("changes", [
    {"subject": None}, {"claims": {"iss": "other"}}, {"resource": "https://other.invalid"},
    {"expires_at": 1}, {"expires_at": None}, {"scopes": []},
])
async def test_incomplete_or_wrong_verified_context_denies(monkeypatch, changes):
    values = {'token': "not-exposed", 'subject': "owner", 'client_id': "chatgpt", 'claims': {"iss": "https://issuer.invalid"}, 'resource': "https://mcp.invalid", 'expires_at': int(time.time()) + 60, 'scopes': ["switchstand"]}
    monkeypatch.setattr("switchstand.principal.get_access_token", lambda: AccessToken(**(values | changes)))
    assert await RequestPrincipal("https://issuer.invalid", "https://mcp.invalid", "switchstand")() is None


async def test_verified_context_becomes_principal_without_token_or_role(monkeypatch):
    token = AccessToken(token="secret", subject="owner", client_id="chatgpt",
                        claims={"iss": "issuer", "role": "coordinator"}, resource="resource",
                        expires_at=int(time.time()) + 60, scopes=["switchstand"])
    monkeypatch.setattr("switchstand.principal.get_access_token", lambda: token)
    principal = await RequestPrincipal("issuer", "resource", "switchstand")()
    assert principal.subject == "owner" and principal.assurance == "authenticated"
    assert "secret" not in principal.model_dump_json() and "role" not in principal.model_dump()


async def test_stdio_without_authenticated_context_is_not_a_user_identity(monkeypatch):
    monkeypatch.setattr("switchstand.principal.get_access_token", lambda: None)
    assert await RequestPrincipal("issuer", "resource", "scope")() is None
