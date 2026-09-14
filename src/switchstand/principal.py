"""Resolve identity only from a resource server's verified request context."""

import time

from mcp.server.auth.middleware.auth_context import get_access_token

from .grants import PrincipalContext


class RequestPrincipal:
    def __init__(self, issuer: str, resource: str, required_scope: str):
        self.issuer, self.resource, self.required_scope = issuer, resource, required_scope

    async def __call__(self) -> PrincipalContext | None:
        # Signature/token validation belongs to the configured MCP TokenVerifier.
        # Never read identity from arguments, role prose, _meta or caller headers.
        token = get_access_token()
        if (token is None or not token.subject or not token.client_id
                or (token.claims or {}).get("iss") != self.issuer
                or token.resource != self.resource or token.expires_at is None
                or token.expires_at <= time.time() or self.required_scope not in token.scopes):
            return None
        return PrincipalContext(issuer=self.issuer, subject=token.subject,
                                client_id=token.client_id, assurance="authenticated")
