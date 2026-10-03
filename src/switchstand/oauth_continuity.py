"""Single-user upstream OAuth continuity for the ChatGPT edge."""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from time import monotonic
from typing import Any

from fastmcp.server.auth.auth import AccessToken as FastAccessToken
from fastmcp.server.auth.oauth_proxy.models import (
    JTIMapping,
    UpstreamTokenSet,
    _hash_token,  # pyright: ignore[reportPrivateUsage] -- match FastMCP's store key
)
from fastmcp.server.auth.providers.github import GitHubProvider
from joserfc.errors import JoseError
from mcp.server.auth.provider import (
    AccessToken as MCPAccessToken,
)
from mcp.server.auth.provider import (
    AuthorizationCode,
    RefreshToken,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

LOG = logging.getLogger(__name__)

CANONICAL_UPSTREAM_TOKEN_ID = "switchstand-github-canonical-v1"
FASTMCP_ACCESS_TOKEN_LIFETIME_SECONDS = 365 * 24 * 60 * 60
REFRESH_REPLAY_TTL_SECONDS = 5 * 60
REFRESH_REPLAY_MAX_ENTRIES = 128


@dataclass(frozen=True)
class _RefreshReplay:
    client_id: str | None
    token_scopes: frozenset[str]
    requested_scopes: frozenset[str]
    token_expires_at: int | None
    successor_hash: str
    successor_jti: str
    issued: OAuthToken
    expires_at: float


class SwitchstandGitHubProvider(GitHubProvider):
    """Share one validated GitHub credential without merging MCP identities."""

    def __init__(self, *, allowed_user_id: str, **kwargs: Any) -> None:
        self.allowed_user_id = allowed_user_id
        self._required_upstream_scopes = frozenset(kwargs.get("required_scopes") or [])
        self._refresh_replays: OrderedDict[str, _RefreshReplay] = OrderedDict()
        super().__init__(**kwargs)

    async def _load_refresh_replay(
        self,
        client: OAuthClientInformationFull,
        token: str,
        token_scopes: frozenset[str] | None = None,
        requested_scopes: frozenset[str] | None = None,
    ) -> _RefreshReplay | None:
        key = _hash_token(token)
        replay = self._refresh_replays.get(key)
        if replay is None:
            return None
        if replay.expires_at <= monotonic():
            self._refresh_replays.pop(key, None)
            LOG.info("mcp_refresh_replay_expired")
            raise TokenError("invalid_grant", "Refresh token replay window expired")
        if (
            replay.client_id != client.client_id
            or (token_scopes is not None and replay.token_scopes != token_scopes)
            or (
                requested_scopes is not None
                and replay.requested_scopes != requested_scopes
            )
        ):
            LOG.warning("mcp_refresh_replay_rejected")
            raise TokenError("invalid_grant", "Refresh token replay does not match request")
        successor = await self._refresh_token_store.get(key=replay.successor_hash)
        successor_mapping = await self._jti_mapping_store.get(key=replay.successor_jti)
        if (
            successor is None
            or successor.client_id != replay.client_id
            or successor_mapping is None
            or successor_mapping.upstream_token_id != CANONICAL_UPSTREAM_TOKEN_ID
        ):
            self._refresh_replays.pop(key, None)
            LOG.info("mcp_refresh_replay_successor_consumed")
            raise TokenError("invalid_grant", "Refresh token replay successor was consumed")
        if self._refresh_replays.get(key) is not replay:
            return None
        self._refresh_replays.move_to_end(key)
        LOG.info("mcp_refresh_replay_accepted")
        return replay

    def _remember_refresh_replay(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
        issued: OAuthToken,
    ) -> None:
        if issued.refresh_token is None:
            return
        now = monotonic()
        for key, replay in list(self._refresh_replays.items()):
            if replay.expires_at <= now:
                del self._refresh_replays[key]
        key = _hash_token(refresh_token.token)
        successor_jti, _ = self._jti_and_ttl(issued.refresh_token, refresh=True)
        _, predecessor_ttl = self._jti_and_ttl(refresh_token.token, refresh=True)
        self._refresh_replays[key] = _RefreshReplay(
            client_id=client.client_id,
            token_scopes=frozenset(refresh_token.scopes),
            requested_scopes=frozenset(scopes),
            token_expires_at=refresh_token.expires_at,
            successor_hash=_hash_token(issued.refresh_token),
            successor_jti=successor_jti,
            issued=issued.model_copy(deep=True),
            expires_at=now + min(REFRESH_REPLAY_TTL_SECONDS, predecessor_ttl),
        )
        self._refresh_replays.move_to_end(key)
        if len(self._refresh_replays) > REFRESH_REPLAY_MAX_ENTRIES:
            self._refresh_replays.popitem(last=False)
            LOG.info("mcp_refresh_replay_evicted")

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        current = await super().load_refresh_token(client, refresh_token)
        if current is not None:
            return current
        canonical_lock = self._get_refresh_lock(CANONICAL_UPSTREAM_TOKEN_ID)
        async with canonical_lock:
            current = await super().load_refresh_token(client, refresh_token)
            if current is not None:
                return current
            try:
                replay = await self._load_refresh_replay(client, refresh_token)
            except TokenError:
                return None
            if replay is None:
                return None
            return RefreshToken(
                token=refresh_token,
                client_id=replay.client_id or "",
                scopes=sorted(replay.token_scopes),
                expires_at=replay.token_expires_at,
            )

    async def _valid_upstream(self, token_set: UpstreamTokenSet) -> MCPAccessToken | None:
        validated = await self._token_validator.verify_token(token_set.access_token)
        if (
            validated is None
            or validated.subject != self.allowed_user_id
            or not self._required_upstream_scopes.issubset(validated.scopes)
        ):
            return None
        return validated

    async def verify_token(self, token: str) -> FastAccessToken | None:
        access = await super().verify_token(token)
        if access is None or access.subject != self.allowed_user_id:
            if access is not None:
                LOG.warning("mcp_github_user_rejected")
            return None
        try:
            claims = self.jwt_issuer.verify_token(token)
        except JoseError:
            return None
        scope = claims.get("scope")
        client_id = claims.get("client_id")
        issuer = claims.get("iss")
        resource = claims.get("aud")
        expires_at = claims.get("exp")
        if (
            not isinstance(scope, str)
            or not self._required_upstream_scopes.issubset(scope.split())
            or not isinstance(client_id, str)
            or not client_id
            or not isinstance(issuer, str)
            or not isinstance(resource, str)
            or not isinstance(expires_at, int)
        ):
            return None
        return access.model_copy(
            update={
                "client_id": client_id,
                "scopes": scope.split(),
                "expires_at": expires_at,
                "resource": resource,
                "claims": dict(access.claims or {}) | {"iss": issuer},
            }
        )

    async def _fresh_valid_upstream(
        self, token_set: UpstreamTokenSet
    ) -> tuple[UpstreamTokenSet, MCPAccessToken] | None:
        validated = await self._valid_upstream(token_set)
        if validated is not None:
            return token_set, validated
        if token_set.refresh_token and token_set.expires_at <= time.time():
            try:
                token_set = await self._try_transparent_refresh(token_set)
            except Exception:  # noqa: BLE001 -- an upstream failure leaves it untrusted
                return None
            validated = await self._valid_upstream(token_set)
            if validated is not None:
                return token_set, validated
        return None

    async def _put_canonical(self, source: UpstreamTokenSet, ttl: float | None) -> None:
        canonical = source.model_copy(
            update={"upstream_token_id": CANONICAL_UPSTREAM_TOKEN_ID, "client_id": ""}
        )
        await self._upstream_token_store.put(
            key=CANONICAL_UPSTREAM_TOKEN_ID,
            value=canonical,
            ttl=max(ttl or 0, self._fastmcp_access_token_expiry_seconds or 0, 1),
        )

    async def _rebind(self, jti: str, ttl: float) -> None:
        await self._jti_mapping_store.put(
            key=jti,
            value=JTIMapping(
                jti=jti,
                upstream_token_id=CANONICAL_UPSTREAM_TOKEN_ID,
                created_at=time.time(),
            ),
            ttl=max(ttl, 1),
        )

    async def _rebind_to_valid_canonical(self, jti: str, ttl: float) -> bool:
        canonical = await self._upstream_token_store.get(
            key=CANONICAL_UPSTREAM_TOKEN_ID
        )
        if canonical is None or await self._fresh_valid_upstream(canonical) is None:
            return False
        await self._rebind(jti, ttl)
        return True

    async def _rebind_to_repaired_canonical(
        self,
        jti: str,
        ttl: float,
        failed_identity: tuple[str, str, str | None],
    ) -> bool:
        """Rebind only to a valid credential distinct from the failed source."""
        canonical = await self._upstream_token_store.get(
            key=CANONICAL_UPSTREAM_TOKEN_ID
        )
        canonical_identity = (
            CANONICAL_UPSTREAM_TOKEN_ID,
            canonical.access_token,
            canonical.refresh_token,
        ) if canonical is not None else None
        if (
            canonical is None
            or canonical_identity == failed_identity
            or await self._valid_upstream(canonical) is None
        ):
            return False
        await self._rebind(jti, ttl)
        return True

    def _jti_and_ttl(self, token: str, *, refresh: bool = False) -> tuple[str, float]:
        payload = self.jwt_issuer.verify_token(
            token, expected_token_use="refresh" if refresh else "access"
        )
        return str(payload["jti"]), max(float(payload["exp"]) - time.time(), 1)

    async def _converge_issued_tokens(self, issued: OAuthToken) -> None:
        """Make a successful exchange the shared credential for every client."""
        access_jti, access_ttl = self._jti_and_ttl(issued.access_token)
        mapping = await self._jti_mapping_store.get(key=access_jti)
        if mapping is None:
            raise RuntimeError("new access token mapping is missing")
        source_id = mapping.upstream_token_id
        source, source_ttl = await self._upstream_token_store.ttl(key=source_id)
        if source is None:
            raise RuntimeError("new upstream token is missing")
        await self._put_canonical(source, source_ttl)
        await self._rebind(access_jti, access_ttl)
        if issued.refresh_token:
            refresh_jti, refresh_ttl = self._jti_and_ttl(
                issued.refresh_token, refresh=True
            )
            await self._rebind(refresh_jti, refresh_ttl)
        if source_id != CANONICAL_UPSTREAM_TOKEN_ID:
            await self._upstream_token_store.delete(key=source_id)

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        code = await self._code_store.get(key=authorization_code.code)
        if code is None:
            return await super().exchange_authorization_code(client, authorization_code)
        candidate = await self._token_validator.verify_token(code.idp_tokens["access_token"])
        if (
            candidate is None
            or candidate.subject != self.allowed_user_id
            or not self._required_upstream_scopes.issubset(candidate.scopes)
        ):
            raise TokenError(
                "invalid_grant",
                "GitHub authorization did not establish the allowed identity",
            )

        lock = self._get_refresh_lock(CANONICAL_UPSTREAM_TOKEN_ID)
        async with lock:
            issued = await super().exchange_authorization_code(client, authorization_code)
            await self._converge_issued_tokens(issued)
            return issued

    async def load_access_token(self, token: str) -> MCPAccessToken | None:
        try:
            jti, ttl = self._jti_and_ttl(token)
            mapping = await self._jti_mapping_store.get(key=jti)
        except JoseError, KeyError, ValueError:
            return None
        if mapping is None:
            lock = self._get_refresh_lock(CANONICAL_UPSTREAM_TOKEN_ID)
            async with lock:
                mapping = await self._jti_mapping_store.get(key=jti)
                if mapping is None and not await self._rebind_to_valid_canonical(jti, ttl):
                    return None
            return await super().load_access_token(token)
        if mapping.upstream_token_id == CANONICAL_UPSTREAM_TOKEN_ID:
            return await super().load_access_token(token)

        lock = self._get_refresh_lock(CANONICAL_UPSTREAM_TOKEN_ID)
        use_canonical = False
        async with lock:
            if await self._rebind_to_valid_canonical(jti, ttl):
                use_canonical = True
            else:
                validated = await super().load_access_token(token)
                if validated is None or validated.subject != self.allowed_user_id:
                    return None
                source, source_ttl = await self._upstream_token_store.ttl(
                    key=mapping.upstream_token_id
                )
                if source is None or await self._valid_upstream(source) is None:
                    return None
                await self._put_canonical(source, source_ttl)
                await self._rebind(jti, ttl)
                return validated
        if use_canonical:
            return await super().load_access_token(token)
        return None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        refresh_jti, refresh_ttl = self._jti_and_ttl(refresh_token.token, refresh=True)
        canonical_lock = self._get_refresh_lock(CANONICAL_UPSTREAM_TOKEN_ID)
        async with canonical_lock:
            mapping = await self._jti_mapping_store.get(key=refresh_jti)
            if mapping is None:
                replay = await self._load_refresh_replay(
                    client,
                    refresh_token.token,
                    frozenset(refresh_token.scopes),
                    frozenset(scopes),
                )
                if replay is not None:
                    return replay.issued.model_copy(deep=True)
            if mapping is None:
                current = await super().load_refresh_token(client, refresh_token.token)
                if current is not None:
                    await self._rebind_to_valid_canonical(refresh_jti, refresh_ttl)
            elif mapping.upstream_token_id != CANONICAL_UPSTREAM_TOKEN_ID:
                await self._rebind_to_valid_canonical(refresh_jti, refresh_ttl)
            mapping = await self._jti_mapping_store.get(key=refresh_jti)
            failed_source = (
                await self._upstream_token_store.get(key=mapping.upstream_token_id)
                if mapping is not None
                else None
            )
            failed_identity = (
                mapping.upstream_token_id,
                failed_source.access_token,
                failed_source.refresh_token,
            ) if mapping is not None and failed_source is not None else None
            try:
                issued = await super().exchange_refresh_token(
                    client, refresh_token, scopes
                )
            except TokenError:
                current = await super().load_refresh_token(
                    client, refresh_token.token
                )
                if (
                    current is None
                    or failed_identity is None
                    or not await self._rebind_to_repaired_canonical(
                        refresh_jti, refresh_ttl, failed_identity
                    )
                ):
                    raise
                issued = await super().exchange_refresh_token(
                    client, refresh_token, scopes
                )
            await self._converge_issued_tokens(issued)
            self._remember_refresh_replay(client, refresh_token, scopes, issued)
            return issued
