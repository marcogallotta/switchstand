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


async def test_missing_access_mapping_recovers_from_valid_canonical_credential():
    subject = provider(MemoryStore())
    verifier(subject)
    await subject._upstream_token_store.put(
        key=CANONICAL_UPSTREAM_TOKEN_ID,
        value=upstream(CANONICAL_UPSTREAM_TOKEN_ID),
        ttl=3600,
    )
    token, jti = await put_access(
        subject, CANONICAL_UPSTREAM_TOKEN_ID, "returning-chat"
    )
    await subject._jti_mapping_store.delete(key=jti)

    access = await subject.verify_token(token)

    assert access is not None
    assert (access.subject, access.client_id) == (USER_ID, "returning-chat")
    assert access.scopes == [SCOPE]
    assert access.resource == RESOURCE
    assert access.claims == {"iss": ISSUER}
    mapping = await subject._jti_mapping_store.get(key=jti)
    assert mapping.upstream_token_id == CANONICAL_UPSTREAM_TOKEN_ID


async def test_missing_access_mapping_does_not_bypass_downstream_scope():
    subject = provider(MemoryStore())
    verifier(subject)
    await subject._upstream_token_store.put(
        key=CANONICAL_UPSTREAM_TOKEN_ID,
        value=upstream(CANONICAL_UPSTREAM_TOKEN_ID),
        ttl=3600,
    )
    jti = "jti-insufficient-scope"
    token = subject.jwt_issuer.issue_access_token(
        client_id="returning-chat",
        scopes=[],
        jti=jti,
        expires_in=3600,
    )

    assert await subject.verify_token(token) is None


async def test_missing_access_mapping_requires_allowed_canonical_identity():
    subject = provider(MemoryStore())
    verifier(subject, wrong=frozenset({"wrong"}))
    await subject._upstream_token_store.put(
        key=CANONICAL_UPSTREAM_TOKEN_ID,
        value=upstream(CANONICAL_UPSTREAM_TOKEN_ID, access="wrong"),
        ttl=3600,
    )
    token, jti = await put_access(
        subject, CANONICAL_UPSTREAM_TOKEN_ID, "returning-chat"
    )
    await subject._jti_mapping_store.delete(key=jti)

    assert await subject.verify_token(token) is None
    assert await subject._jti_mapping_store.get(key=jti) is None


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


async def put_refresh(
    subject, client_id, *, mapping_ttl=3600, token_id=CANONICAL_UPSTREAM_TOKEN_ID
):
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
            upstream_token_id=token_id,
            created_at=time.time(),
        ),
        ttl=mapping_ttl,
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


async def test_expired_refresh_mapping_recovers_from_valid_canonical_credential(
    tmp_path: Path,
):
    subject = provider(FileTreeStore(data_directory=tmp_path / "oauth"))
    verifier(subject, valid=frozenset({"good", "refreshed"}))
    await subject._upstream_token_store.put(
        key=CANONICAL_UPSTREAM_TOKEN_ID,
        value=upstream(CANONICAL_UPSTREAM_TOKEN_ID),
        ttl=3600,
    )
    refresh_token = await put_refresh(subject, "returning-chat", mapping_ttl=0.01)
    await anyio.sleep(0.02)
    assert await subject._jti_mapping_store.get(key="refresh-jti-returning-chat") is None

    class OAuthClient:
        async def refresh_token(self, **_kwargs):
            return {
                "access_token": "refreshed",
                "refresh_token": "rotated-upstream",
                "expires_in": 1800,
                "refresh_expires_in": 3600,
                "scope": SCOPE,
            }

    @asynccontextmanager
    async def oauth_client():
        yield OAuthClient()

    subject._upstream_oauth_client = oauth_client
    downstream = client("returning-chat")
    loaded = await subject.load_refresh_token(downstream, refresh_token)
    assert loaded is not None

    result = await subject.exchange_refresh_token(downstream, loaded, [SCOPE])

    access_claims = subject.jwt_issuer.verify_token(result.access_token)
    refresh_claims = subject.jwt_issuer.verify_token(
        result.refresh_token, expected_token_use="refresh"
    )
    assert access_claims["client_id"] == "returning-chat"
    assert access_claims["scope"] == SCOPE
    assert refresh_claims["client_id"] == "returning-chat"
    assert refresh_claims["scope"] == SCOPE


async def test_rotated_refresh_mapping_is_not_recovered_for_a_late_request():
    subject = provider(MemoryStore())
    verifier(subject, valid=frozenset({"good", "refreshed"}))
    await subject._upstream_token_store.put(
        key=CANONICAL_UPSTREAM_TOKEN_ID,
        value=upstream(CANONICAL_UPSTREAM_TOKEN_ID),
        ttl=3600,
    )
    refresh_token = await put_refresh(subject, "returning-chat")

    class OAuthClient:
        async def refresh_token(self, **_kwargs):
            return {
                "access_token": "refreshed",
                "refresh_token": "rotated-upstream",
                "expires_in": 1800,
                "refresh_expires_in": 3600,
                "scope": SCOPE,
            }

    @asynccontextmanager
    async def oauth_client():
        yield OAuthClient()

    subject._upstream_oauth_client = oauth_client
    downstream = client("returning-chat")
    loaded = await subject.load_refresh_token(downstream, refresh_token)
    assert loaded is not None
    await subject.exchange_refresh_token(downstream, loaded, [SCOPE])

    try:
        await subject.exchange_refresh_token(downstream, loaded, [SCOPE])
    except TokenError as error:
        assert error.error == "invalid_grant"
        assert error.error_description == "Refresh token mapping not found"
    else:
        raise AssertionError("rotated refresh token was accepted twice")
    assert await subject._jti_mapping_store.get(key="refresh-jti-returning-chat") is None


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


async def test_legacy_refresh_recovery_repairs_canonical_for_waiting_clients():
    subject = provider(MemoryStore())
    valid_access = {"legacy-good"}

    async def verify(token):
        if token not in valid_access:
            return None
        return AccessToken(
            token=token,
            client_id="github",
            scopes=[SCOPE],
            subject=USER_ID,
        )

    subject._token_validator.verify_token = verify
    expired = time.time() - 1
    poisoned = upstream(
        CANONICAL_UPSTREAM_TOKEN_ID,
        access="revoked",
        refresh="poisoned-refresh",
    ).model_copy(update={"expires_at": expired})
    await subject._upstream_token_store.put(
        key=CANONICAL_UPSTREAM_TOKEN_ID, value=poisoned, ttl=3600
    )

    client_ids = ("chatgpt-a", "chatgpt-b", "chatgpt-c")
    tokens = {}
    for client_id in client_ids:
        token_id = f"legacy-{client_id}"
        await subject._upstream_token_store.put(
            key=token_id,
            value=upstream(
                token_id,
                access="legacy-good",
                refresh=f"legacy-refresh-{client_id}",
            ),
            ttl=3600,
        )
        tokens[client_id] = await put_refresh(
            subject, client_id, token_id=token_id
        )

    upstream_inputs = []

    class OAuthClient:
        async def refresh_token(self, **kwargs):
            refresh_token = kwargs["refresh_token"]
            upstream_inputs.append(refresh_token)
            await anyio.sleep(0.01)
            if refresh_token == "poisoned-refresh":
                raise TokenError("invalid_grant", "bad_refresh_token")
            sequence = len(upstream_inputs)
            access = f"recovered-{sequence}"
            valid_access.add(access)
            return {
                "access_token": access,
                "refresh_token": f"rotated-{sequence}",
                "expires_in": 1800,
                "refresh_expires_in": 3600,
                "scope": SCOPE,
            }

    @asynccontextmanager
    async def oauth_client():
        yield OAuthClient()

    subject._upstream_oauth_client = oauth_client
    results = {}

    async def exchange(client_id):
        loaded = RefreshToken(
            token=tokens[client_id],
            client_id=client_id,
            scopes=[SCOPE],
            expires_at=int(time.time()) + 3600,
        )
        results[client_id] = await subject.exchange_refresh_token(
            client(client_id), loaded, [SCOPE]
        )

    async with anyio.create_task_group() as group:
        for client_id in client_ids:
            group.start_soon(exchange, client_id)

    assert len(results) == 3
    assert upstream_inputs[0] == "poisoned-refresh"
    assert upstream_inputs[1].startswith("legacy-refresh-")
    assert upstream_inputs[2:] == ["rotated-2", "rotated-3"]
    for client_id, result in results.items():
        access_jti, _ = subject._jti_and_ttl(result.access_token)
        refresh_jti, _ = subject._jti_and_ttl(result.refresh_token, refresh=True)
        for jti in (access_jti, refresh_jti):
            mapping = await subject._jti_mapping_store.get(key=jti)
            assert mapping.upstream_token_id == CANONICAL_UPSTREAM_TOKEN_ID
        loaded = RefreshToken(
            token=tokens[client_id],
            client_id=client_id,
            scopes=[SCOPE],
            expires_at=int(time.time()) + 3600,
        )
        try:
            await subject.exchange_refresh_token(client(client_id), loaded, [SCOPE])
        except TokenError as error:
            assert error.error == "invalid_grant"
        else:
            raise AssertionError("rotated refresh token was accepted twice")


async def test_all_invalid_refresh_credentials_fail_once_without_consuming_client_token():
    subject = provider(MemoryStore())
    verifier(subject)
    poisoned = upstream(
        CANONICAL_UPSTREAM_TOKEN_ID,
        access="revoked",
        refresh="poisoned-refresh",
    ).model_copy(update={"expires_at": time.time() - 1})
    await subject._upstream_token_store.put(
        key=CANONICAL_UPSTREAM_TOKEN_ID, value=poisoned, ttl=3600
    )
    await subject._upstream_token_store.put(
        key="legacy",
        value=upstream("legacy", access="revoked", refresh="legacy-invalid"),
        ttl=3600,
    )
    refresh_token = await put_refresh(subject, "stranded", token_id="legacy")
    upstream_inputs = []

    class OAuthClient:
        async def refresh_token(self, **kwargs):
            upstream_inputs.append(kwargs["refresh_token"])
            raise TokenError("invalid_grant", "bad_refresh_token")

    @asynccontextmanager
    async def oauth_client():
        yield OAuthClient()

    subject._upstream_oauth_client = oauth_client
    downstream = client("stranded")
    loaded = await subject.load_refresh_token(downstream, refresh_token)
    assert loaded is not None

    try:
        await subject.exchange_refresh_token(downstream, loaded, [SCOPE])
    except TokenError as error:
        assert error.error == "invalid_grant"
    else:
        raise AssertionError("invalid upstream credentials were accepted")

    assert upstream_inputs == ["poisoned-refresh", "legacy-invalid"]
    assert await subject.load_refresh_token(downstream, refresh_token) is not None
    mapping = await subject._jti_mapping_store.get(key="refresh-jti-stranded")
    assert mapping.upstream_token_id == "legacy"


async def test_failed_legacy_refresh_retries_once_after_canonical_recovery():
    subject = provider(MemoryStore())
    valid_access = {"recovered"}
    verifier(subject, valid=frozenset(valid_access))
    await subject._upstream_token_store.put(
        key="legacy",
        value=upstream("legacy", access="revoked", refresh="legacy-stale"),
        ttl=3600,
    )
    refresh_token = await put_refresh(subject, "waiting", token_id="legacy")
    upstream_inputs = []

    class OAuthClient:
        async def refresh_token(self, **kwargs):
            upstream_inputs.append(kwargs["refresh_token"])
            if kwargs["refresh_token"] == "legacy-stale":
                await subject._upstream_token_store.put(
                    key=CANONICAL_UPSTREAM_TOKEN_ID,
                    value=upstream(
                        CANONICAL_UPSTREAM_TOKEN_ID,
                        access="recovered",
                        refresh="canonical-current",
                    ),
                    ttl=3600,
                )
                raise TokenError("invalid_grant", "bad_refresh_token")
            return {
                "access_token": "recovered",
                "refresh_token": "canonical-next",
                "expires_in": 1800,
                "refresh_expires_in": 3600,
                "scope": SCOPE,
            }

    @asynccontextmanager
    async def oauth_client():
        yield OAuthClient()

    subject._upstream_oauth_client = oauth_client
    downstream = client("waiting")
    loaded = await subject.load_refresh_token(downstream, refresh_token)
    result = await subject.exchange_refresh_token(downstream, loaded, [SCOPE])

    assert upstream_inputs == ["legacy-stale", "canonical-current"]
    refresh_jti, _ = subject._jti_and_ttl(result.refresh_token, refresh=True)
    mapping = await subject._jti_mapping_store.get(key=refresh_jti)
    assert mapping.upstream_token_id == CANONICAL_UPSTREAM_TOKEN_ID


async def test_live_canonical_access_with_dead_refresh_is_not_retried():
    subject = provider(MemoryStore())
    verifier(subject)
    await subject._upstream_token_store.put(
        key=CANONICAL_UPSTREAM_TOKEN_ID,
        value=upstream(
            CANONICAL_UPSTREAM_TOKEN_ID,
            access="good",
            refresh="dead-refresh",
        ),
        ttl=3600,
    )
    refresh_token = await put_refresh(subject, "stranded")
    upstream_inputs = []

    class OAuthClient:
        async def refresh_token(self, **kwargs):
            upstream_inputs.append(kwargs["refresh_token"])
            raise TokenError("invalid_grant", "bad_refresh_token")

    @asynccontextmanager
    async def oauth_client():
        yield OAuthClient()

    subject._upstream_oauth_client = oauth_client
    downstream = client("stranded")
    loaded = await subject.load_refresh_token(downstream, refresh_token)
    assert loaded is not None

    try:
        await subject.exchange_refresh_token(downstream, loaded, [SCOPE])
    except TokenError as error:
        assert error.error == "invalid_grant"
    else:
        raise AssertionError("dead canonical refresh token was accepted")

    assert upstream_inputs == ["dead-refresh"]
    assert await subject.load_refresh_token(downstream, refresh_token) is not None
