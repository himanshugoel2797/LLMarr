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


def effective_mode(server_cfg) -> str:
    """Resolve the active auth mode. ``require_auth=false`` forces 'none'."""
    if not server_cfg.require_auth:
        return "none"
    return server_cfg.auth_mode


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer "):
        return header[len("Bearer "):]
    return ""


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject any request whose bearer token does not match, constant-time.

    Kept for the simple static-token deployment; :class:`AuthMiddleware` is the
    mode-aware guard used by the server entrypoint."""

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


class AuthMiddleware(BaseHTTPMiddleware):
    """Mode-aware guard for the HTTP transport.

    * ``none``  — pass everything through.
    * ``token`` — require the static bearer token on every request.
    * ``oauth`` — let the OAuth endpoints through unauthenticated; on protected
      paths accept EITHER the static token (so Claude Code keeps working) OR a
      valid OAuth access token, and return a 401 that points MCP clients at the
      protected-resource metadata so they can start the OAuth flow.
    """

    def __init__(self, app, store, oauth_provider=None):
        super().__init__(app)
        self.store = store
        self.oauth = oauth_provider

    async def dispatch(self, request: Request, call_next):
        server = self.store.config.server
        mode = effective_mode(server)
        if mode == "none":
            return await call_next(request)

        if mode == "oauth":
            from .oauth import is_public_path

            if is_public_path(request.url.path):
                return await call_next(request)

        token = _bearer(request)
        static = server.auth_token
        if static and token and hmac.compare_digest(token, static):
            return await call_next(request)
        if mode == "oauth" and self.oauth and token and self.oauth.verify_access_token(token):
            return await call_next(request)

        headers = {"WWW-Authenticate": "Bearer"}
        if mode == "oauth" and self.oauth:
            meta = self.oauth.base_url(request) + "/.well-known/oauth-protected-resource"
            headers["WWW-Authenticate"] = f'Bearer resource_metadata="{meta}"'
        return JSONResponse(
            {"error": "unauthorized", "detail": "valid bearer token required"},
            status_code=401,
            headers=headers,
        )
