"""Single-user upstream OAuth continuity for the ChatGPT edge."""

from __future__ import annotations

import logging
import time
from typing import Any

from fastmcp.server.auth.auth import AccessToken as FastAccessToken
from fastmcp.server.auth.oauth_proxy.models import (
    JTIMapping,
    UpstreamTokenSet,
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


class SwitchstandGitHubProvider(GitHubProvider):
    """Share one validated GitHub credential without merging MCP identities."""

    def __init__(self, *, allowed_user_id: str, **kwargs: Any) -> None:
        self.allowed_user_id = allowed_user_id
        self._required_upstream_scopes = frozenset(kwargs.get("required_scopes") or [])
        super().__init__(**kwargs)

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

    def _jti_and_ttl(self, token: str, *, refresh: bool = False) -> tuple[str, float]:
        payload = self.jwt_issuer.verify_token(
            token, expected_token_use="refresh" if refresh else "access"
        )
        return str(payload["jti"]), max(float(payload["exp"]) - time.time(), 1)

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
            access_jti, access_ttl = self._jti_and_ttl(issued.access_token)
            mapping = await self._jti_mapping_store.get(key=access_jti)
            if mapping is None:
                raise RuntimeError("new access token mapping is missing")
            source, source_ttl = await self._upstream_token_store.ttl(key=mapping.upstream_token_id)
            if source is None:
                raise RuntimeError("new upstream token is missing")
            await self._put_canonical(source, source_ttl)
            await self._rebind(access_jti, access_ttl)
            if issued.refresh_token:
                refresh_jti, refresh_ttl = self._jti_and_ttl(issued.refresh_token, refresh=True)
                await self._rebind(refresh_jti, refresh_ttl)
            await self._upstream_token_store.delete(key=mapping.upstream_token_id)
            return issued

    async def load_access_token(self, token: str) -> MCPAccessToken | None:
        try:
            jti, ttl = self._jti_and_ttl(token)
            mapping = await self._jti_mapping_store.get(key=jti)
        except JoseError, KeyError, ValueError:
            return None
        if mapping is None or mapping.upstream_token_id == CANONICAL_UPSTREAM_TOKEN_ID:
            return await super().load_access_token(token)

        lock = self._get_refresh_lock(CANONICAL_UPSTREAM_TOKEN_ID)
        use_canonical = False
        async with lock:
            canonical = await self._upstream_token_store.get(key=CANONICAL_UPSTREAM_TOKEN_ID)
            if canonical and await self._fresh_valid_upstream(canonical):
                await self._rebind(jti, ttl)
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
            canonical = await self._upstream_token_store.get(key=CANONICAL_UPSTREAM_TOKEN_ID)
            if (
                mapping is not None
                and mapping.upstream_token_id != CANONICAL_UPSTREAM_TOKEN_ID
                and canonical is not None
                and await self._fresh_valid_upstream(canonical)
            ):
                await self._rebind(refresh_jti, refresh_ttl)
            return await super().exchange_refresh_token(client, refresh_token, scopes)
