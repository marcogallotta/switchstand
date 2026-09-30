import time
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oauth_proxy.models import (
    ClientCode,
    JTIMapping,
    RefreshTokenMetadata,
    UpstreamTokenSet,
    _hash_token,
)
from key_value.aio.stores.filetree import FileTreeStore
from key_value.aio.stores.memory import MemoryStore
from mcp.server.auth.provider import AuthorizationCode, RefreshToken, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from switchstand.oauth_continuity import (
    CANONICAL_UPSTREAM_TOKEN_ID,
    SwitchstandGitHubProvider,
)

ISSUER = "https://switchstand.example.com/"
RESOURCE = "https://switchstand.example.com/mcp"
USER_ID = "192548"
SCOPE = "read:user"


def provider(storage):
    subject = SwitchstandGitHubProvider(
        client_id="github-client",
        client_secret="github-secret",
        allowed_user_id=USER_ID,
        base_url=ISSUER,
        issuer_url=ISSUER,
        required_scopes=[SCOPE],
        client_storage=storage,
        fastmcp_access_token_expiry_seconds=365 * 24 * 60 * 60,
    )
    subject.get_routes(mcp_path="/mcp")
    return subject


def verifier(subject, *, valid=frozenset({"good"}), wrong=frozenset()):
    async def verify(token):
        if token not in valid | wrong:
            return None
        return AccessToken(
            token=token,
            client_id="github",
            scopes=[SCOPE],
            subject="wrong-user" if token in wrong else USER_ID,
        )

    subject._token_validator.verify_token = verify


def upstream(token_id, access="good", refresh="upstream-refresh"):
    return UpstreamTokenSet(
        upstream_token_id=token_id,
        access_token=access,
        refresh_token=refresh,
        refresh_token_expires_at=time.time() + 3600,
        expires_at=time.time() + 1800,
        token_type="Bearer",
        scope=SCOPE,
        client_id="legacy-client",
        created_at=time.time(),
        raw_token_data={
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": 1800,
            "refresh_expires_in": 3600,
            "scope": SCOPE,
        },
    )


async def put_access(subject, token_id, client_id):
    jti = f"jti-{client_id}"
    token = subject.jwt_issuer.issue_access_token(
        client_id=client_id,
        scopes=[SCOPE],
        jti=jti,
        expires_in=3600,
    )
    await subject._jti_mapping_store.put(
        key=jti,
        value=JTIMapping(jti=jti, upstream_token_id=token_id, created_at=time.time()),
        ttl=3600,
    )
    return token, jti


async def test_legacy_clients_converge_without_merging_downstream_identity():
    subject = provider(MemoryStore())
    verifier(subject)
    await subject._upstream_token_store.put(
        key=CANONICAL_UPSTREAM_TOKEN_ID,
        value=upstream(CANONICAL_UPSTREAM_TOKEN_ID),
        ttl=3600,
    )

    tokens = []
    for number in range(12):
        token_id = f"legacy-{number}"
        await subject._upstream_token_store.put(
            key=token_id, value=upstream(token_id, access="revoked"), ttl=3600
        )
        tokens.append(await put_access(subject, token_id, f"client-{number}"))

    for number, (token, jti) in enumerate(tokens):
        access = await subject.verify_token(token)
        assert access is not None
        assert (access.subject, access.client_id) == (USER_ID, f"client-{number}")
        mapping = await subject._jti_mapping_store.get(key=jti)
        assert mapping.upstream_token_id == CANONICAL_UPSTREAM_TOKEN_ID


async def test_valid_legacy_promotes_across_restart_but_wrong_identity_does_not(
    tmp_path: Path,
):
    storage = FileTreeStore(data_directory=tmp_path / "oauth")
    first = provider(storage)
    verifier(first, valid=frozenset({"legacy-good"}), wrong=frozenset({"wrong"}))
    await first._upstream_token_store.put(
        key="legacy", value=upstream("legacy", access="legacy-good"), ttl=3600
    )
    token, jti = await put_access(first, "legacy", "old-chat")
    assert await first.verify_token(token) is not None

    restarted = provider(FileTreeStore(data_directory=tmp_path / "oauth"))
    verifier(restarted, valid=frozenset({"legacy-good"}), wrong=frozenset({"wrong"}))
    access = await restarted.verify_token(token)
    mapping = await restarted._jti_mapping_store.get(key=jti)
    assert access is not None and access.client_id == "old-chat"
    assert mapping.upstream_token_id == CANONICAL_UPSTREAM_TOKEN_ID

    isolated = provider(MemoryStore())
    verifier(isolated, wrong=frozenset({"wrong"}))
    await isolated._upstream_token_store.put(
        key="wrong", value=upstream("wrong", access="wrong"), ttl=3600
    )
    wrong_token, _ = await put_access(isolated, "wrong", "intruder")
    assert await isolated.verify_token(wrong_token) is None
    assert await isolated._upstream_token_store.get(key=CANONICAL_UPSTREAM_TOKEN_ID) is None


def client(client_id="chatgpt"):
    return OAuthClientInformationFull(client_id=client_id)


async def put_code(subject, code, client_id, access):
    await subject._code_store.put(
        key=code,
        value=ClientCode(
            code=code,
            client_id=client_id,
            redirect_uri="https://client.example/callback",
            code_challenge="challenge",
            code_challenge_method="S256",
            scopes=[SCOPE],
            idp_tokens={
                "access_token": access,
                "refresh_token": f"refresh-{code}",
                "expires_in": 1800,
                "refresh_expires_in": 3600,
                "scope": SCOPE,
            },
            expires_at=time.time() + 300,
            created_at=time.time(),
        ),
        ttl=300,
    )
    return AuthorizationCode(
        code=code,
        client_id=client_id,
        redirect_uri=AnyUrl("https://client.example/callback"),
        redirect_uri_provided_explicitly=True,
        scopes=[SCOPE],
        expires_at=time.time() + 300,
        code_challenge="challenge",
    )


async def test_new_authorizations_replace_canonical_not_multiply_local_credentials():
    subject = provider(MemoryStore())
    valid = {f"good-{number}" for number in range(12)}
    verifier(subject, valid=frozenset(valid), wrong=frozenset({"wrong"}))

    issued = []
    for number in range(12):
        downstream = client(f"client-{number}")
        code = await put_code(subject, f"code-{number}", downstream.client_id, f"good-{number}")
        issued.append(await subject.exchange_authorization_code(downstream, code))

    canonical = await subject._upstream_token_store.get(key=CANONICAL_UPSTREAM_TOKEN_ID)
    assert canonical.access_token == "good-11" and canonical.client_id == ""
    for number, token in enumerate(issued):
        claims = subject.jwt_issuer.verify_token(token.access_token)
        mapping = await subject._jti_mapping_store.get(key=claims["jti"])
        assert claims["client_id"] == f"client-{number}"
        assert mapping.upstream_token_id == CANONICAL_UPSTREAM_TOKEN_ID

    rejected = await put_code(subject, "wrong-code", "wrong-client", "wrong")
    try:
        await subject.exchange_authorization_code(client("wrong-client"), rejected)
    except TokenError as error:
        assert "allowed identity" in str(error)
    else:
        raise AssertionError("wrong GitHub identity was accepted")
    assert (
        await subject._upstream_token_store.get(key=CANONICAL_UPSTREAM_TOKEN_ID)
    ).access_token == "good-11"


async def put_refresh(subject, client_id):
    refresh_jti = f"refresh-jti-{client_id}"
    refresh_token = subject.jwt_issuer.issue_refresh_token(
        client_id=client_id,
        scopes=[SCOPE],
        jti=refresh_jti,
        expires_in=3600,
    )
    await subject._jti_mapping_store.put(
        key=refresh_jti,
        value=JTIMapping(
            jti=refresh_jti,
            upstream_token_id=CANONICAL_UPSTREAM_TOKEN_ID,
            created_at=time.time(),
        ),
        ttl=3600,
    )
    await subject._refresh_token_store.put(
        key=_hash_token(refresh_token),
        value=RefreshTokenMetadata(
            client_id=client_id,
            scopes=[SCOPE],
            expires_at=int(time.time()) + 3600,
            created_at=time.time(),
        ),
        ttl=3600,
    )
    return refresh_token


async def test_distinct_client_refreshes_serialize_one_canonical_rotation_at_a_time():
    subject = provider(MemoryStore())
    verifier(subject, valid=frozenset({"good"}))
    await subject._upstream_token_store.put(
        key=CANONICAL_UPSTREAM_TOKEN_ID,
        value=upstream(CANONICAL_UPSTREAM_TOKEN_ID),
        ttl=3600,
    )
    tokens = {
        client_id: await put_refresh(subject, client_id) for client_id in ("chatgpt-a", "chatgpt-b")
    }
    active = 0
    max_active = 0
    upstream_inputs = []

    class OAuthClient:
        async def refresh_token(self, **kwargs):
            nonlocal active, max_active
            upstream_inputs.append(kwargs["refresh_token"])
            active += 1
            max_active = max(max_active, active)
            await anyio.sleep(0.01)
            active -= 1
            sequence = len(upstream_inputs)
            return {
                "access_token": f"refreshed-{sequence}",
                "refresh_token": f"rotated-upstream-{sequence}",
                "expires_in": 1800,
                "refresh_expires_in": 3600,
                "scope": SCOPE,
            }

    @asynccontextmanager
    async def oauth_client():
        yield OAuthClient()

    subject._upstream_oauth_client = oauth_client
    results = []

    async def exchange(client_id):
        loaded = RefreshToken(
            token=tokens[client_id],
            client_id=client_id,
            scopes=[SCOPE],
            expires_at=int(time.time()) + 3600,
        )
        results.append(await subject.exchange_refresh_token(client(client_id), loaded, [SCOPE]))

    async with anyio.create_task_group() as group:
        group.start_soon(exchange, "chatgpt-a")
        group.start_soon(exchange, "chatgpt-b")

    assert len(results) == 2 and max_active == 1
    assert upstream_inputs == ["upstream-refresh", "rotated-upstream-1"]
