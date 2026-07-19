"""Authentication for the HTTP transport.

A single static bearer token is the whole scheme — deliberately simple, because
LLMarr is a single-user homelab service, not a multi-tenant API. The token is a
*persistent login*: generated once on first HTTP start, saved to config, and
reused across restarts. Clients authenticate every request with
``Authorization: Bearer <token>``.

stdio transport is not wrapped: the MCP client spawns the process directly, so
the channel is local and already trusted.
"""

from __future__ import annotations

import hmac
import logging
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger("llmarr.auth")


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def ensure_token(store) -> str:
    """Return the configured auth token, generating and persisting one if unset."""
    token = store.config.server.auth_token
    if not token:
        token = generate_token()
        store.mutate(lambda c: setattr(c.server, "auth_token", token))
        log.warning("No auth token was set — generated a new one and saved it to config.")
    return token


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject any request whose bearer token does not match, constant-time."""

    def __init__(self, app, token: str):
        super().__init__(app)
        self._expected = f"Bearer {token}"

    async def dispatch(self, request: Request, call_next):
        header = request.headers.get("authorization", "")
        if not hmac.compare_digest(header, self._expected):
            return JSONResponse(
                {"error": "unauthorized", "detail": "valid bearer token required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)
