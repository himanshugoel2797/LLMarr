"""A small, self-contained OAuth 2.1 authorization server + resource server.

claude.ai custom connectors (and therefore the mobile apps) authenticate to a
remote MCP server with OAuth 2.1 (authorization code + PKCE, dynamic client
registration) — they can't send a static bearer header. This module makes LLMarr
speak that flow while keeping the "single persistent login" idea: the existing
static ``auth_token`` is the credential the user types on the authorize page.

It is deliberately minimal and single-user:

* Dynamic Client Registration (RFC 7591) — clients self-register, get a public
  ``client_id`` (no secret; PKCE is the proof).
* Discovery metadata — RFC 8414 (authorization server) + RFC 9728 (protected
  resource) so the client can find these endpoints.
* Authorization endpoint — a tiny HTML page that asks for the LLMarr token; on
  success it issues a short-lived authorization code (a signed JWT).
* Token endpoint — exchanges the code (verifying PKCE) for an access token and a
  refresh token, both signed JWTs. No server-side token storage needed.
* Resource validation — ``verify_access_token`` checks the JWT signature/expiry
  so the ``/mcp`` guard can accept OAuth tokens.

All tokens are HS256 JWTs signed with a per-install key, so nothing but the auth
code single-use set lives in memory and issued tokens survive restarts.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import secrets
import time
import urllib.parse
from typing import Optional

import jwt
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .auth import ensure_token

CODE_TTL = 300           # 5 min
ACCESS_TTL = 3600        # 1 hour
REFRESH_TTL = 30 * 86400  # 30 days
ALG = "HS256"
SCOPE = "mcp"

# Paths that must be reachable without a bearer token when OAuth is on.
PUBLIC_PREFIXES = (
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource",
    "/authorize",
    "/token",
    "/register",
)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def pkce_s256(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode()).digest())


def is_public_path(path: str) -> bool:
    return any(path.startswith(p) for p in PUBLIC_PREFIXES)


class OAuthProvider:
    def __init__(self, store, db, mcp_path: str = "/mcp"):
        self.store = store
        self.db = db
        self.mcp_path = mcp_path
        self._used_codes: set[str] = set()

    # -- config-derived values --------------------------------------------- #
    @property
    def config(self):
        return self.store.config

    def signing_key(self) -> str:
        key = self.config.server.oauth_signing_key
        if not key:
            key = secrets.token_urlsafe(48)
            self.store.mutate(lambda c: setattr(c.server, "oauth_signing_key", key))
        return key

    def base_url(self, request: Request) -> str:
        configured = self.config.server.public_url
        if configured:
            return configured.rstrip("/")
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("host", request.url.netloc)
        return f"{proto}://{host}"

    def resource(self, request: Request) -> str:
        return self.base_url(request) + self.mcp_path

    # -- jwt helpers ------------------------------------------------------- #
    def _encode(self, claims: dict) -> str:
        return jwt.encode(claims, self.signing_key(), algorithm=ALG)

    def _decode(self, token: str) -> dict:
        # aud is informational here (single resource); the HMAC signature is the
        # security boundary. Skipping aud verification avoids requiring callers to
        # pass an audience just to read a token we ourselves signed.
        return jwt.decode(
            token, self.signing_key(), algorithms=[ALG], options={"verify_aud": False}
        )

    def verify_access_token(self, token: str) -> Optional[dict]:
        """Return claims for a valid access token, else None."""
        try:
            claims = self._decode(token)
        except jwt.PyJWTError:
            return None
        if claims.get("typ") != "access":
            return None
        return claims

    # -- discovery metadata ------------------------------------------------ #
    async def protected_resource_metadata(self, request: Request) -> Response:
        base = self.base_url(request)
        return JSONResponse(
            {
                "resource": self.resource(request),
                "authorization_servers": [base],
                "bearer_methods_supported": ["header"],
                "scopes_supported": [SCOPE],
            }
        )

    async def authorization_server_metadata(self, request: Request) -> Response:
        base = self.base_url(request)
        return JSONResponse(
            {
                "issuer": base,
                "authorization_endpoint": f"{base}/authorize",
                "token_endpoint": f"{base}/token",
                "registration_endpoint": f"{base}/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none"],
                "scopes_supported": [SCOPE],
            }
        )

    # -- dynamic client registration --------------------------------------- #
    async def register(self, request: Request) -> Response:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
        redirect_uris = body.get("redirect_uris") or []
        if not isinstance(redirect_uris, list) or not redirect_uris:
            return JSONResponse(
                {"error": "invalid_redirect_uri", "error_description": "redirect_uris required"},
                status_code=400,
            )
        client_id = "llmarr-" + secrets.token_urlsafe(16)
        client_name = body.get("client_name") or "mcp-client"
        self.db.add_oauth_client(client_id, client_name, json.dumps(redirect_uris))
        return JSONResponse(
            {
                "client_id": client_id,
                "client_id_issued_at": int(time.time()),
                "client_name": client_name,
                "redirect_uris": redirect_uris,
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
            status_code=201,
        )

    # -- authorize --------------------------------------------------------- #
    def _valid_client(self, client_id: str, redirect_uri: str) -> bool:
        client = self.db.get_oauth_client(client_id)
        if not client:
            return False
        try:
            uris = json.loads(client["redirect_uris"])
        except Exception:
            return False
        return redirect_uri in uris

    async def authorize_get(self, request: Request) -> Response:
        p = request.query_params
        client_id = p.get("client_id", "")
        redirect_uri = p.get("redirect_uri", "")
        if p.get("response_type") != "code":
            return HTMLResponse(_error_page("Unsupported response_type (expected 'code')."), 400)
        if not self._valid_client(client_id, redirect_uri):
            return HTMLResponse(_error_page("Unknown client_id or unregistered redirect_uri."), 400)
        if not p.get("code_challenge") or p.get("code_challenge_method", "S256") != "S256":
            return HTMLResponse(_error_page("PKCE with S256 code_challenge is required."), 400)
        return HTMLResponse(_authorize_page(p, error=None))

    async def authorize_post(self, request: Request) -> Response:
        form = await request.form()
        client_id = form.get("client_id", "")
        redirect_uri = form.get("redirect_uri", "")
        if not self._valid_client(client_id, redirect_uri):
            return HTMLResponse(_error_page("Unknown client_id or unregistered redirect_uri."), 400)

        entered = form.get("token", "")
        expected = ensure_token(self.store)
        if not hmac.compare_digest(entered, expected):
            return HTMLResponse(_authorize_page(form, error="Incorrect token — try again."), 401)

        now = int(time.time())
        code = self._encode(
            {
                "typ": "code",
                "iss": self.base_url(request),
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": form.get("code_challenge", ""),
                "scope": form.get("scope", SCOPE),
                "resource": form.get("resource", self.resource(request)),
                "iat": now,
                "exp": now + CODE_TTL,
                "jti": secrets.token_urlsafe(12),
            }
        )
        sep = "&" if "?" in redirect_uri else "?"
        location = f"{redirect_uri}{sep}code={code}"
        state = form.get("state")
        if state:
            # URL-encode (not HTML-escape) — this goes in a query string.
            location += f"&state={urllib.parse.quote(str(state), safe='')}"
        return RedirectResponse(location, status_code=302)

    # -- token ------------------------------------------------------------- #
    async def token(self, request: Request) -> Response:
        form = await request.form()
        grant = form.get("grant_type")
        if grant == "authorization_code":
            return self._grant_authorization_code(request, form)
        if grant == "refresh_token":
            return self._grant_refresh_token(request, form)
        return _token_error("unsupported_grant_type")

    def _issue_tokens(self, request: Request, scope: str, resource: str, client_id: str) -> Response:
        now = int(time.time())
        base = self.base_url(request)
        access = self._encode({
            "typ": "access", "iss": base, "sub": "llmarr", "aud": resource,
            "scope": scope, "client_id": client_id, "iat": now, "exp": now + ACCESS_TTL,
            "jti": secrets.token_urlsafe(12),
        })
        refresh = self._encode({
            "typ": "refresh", "iss": base, "sub": "llmarr", "aud": resource,
            "scope": scope, "client_id": client_id, "iat": now, "exp": now + REFRESH_TTL,
            "jti": secrets.token_urlsafe(12),
        })
        return JSONResponse(
            {
                "access_token": access,
                "token_type": "Bearer",
                "expires_in": ACCESS_TTL,
                "refresh_token": refresh,
                "scope": scope,
            },
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    def _grant_authorization_code(self, request: Request, form) -> Response:
        code = form.get("code", "")
        verifier = form.get("code_verifier", "")
        client_id = form.get("client_id", "")
        redirect_uri = form.get("redirect_uri", "")
        try:
            claims = self._decode(code)
        except jwt.PyJWTError:
            return _token_error("invalid_grant", "code invalid or expired")
        if claims.get("typ") != "code":
            return _token_error("invalid_grant")
        if claims.get("jti") in self._used_codes:
            return _token_error("invalid_grant", "code already used")
        if claims.get("client_id") != client_id or claims.get("redirect_uri") != redirect_uri:
            return _token_error("invalid_grant", "client_id/redirect_uri mismatch")
        if not verifier or pkce_s256(verifier) != claims.get("code_challenge"):
            return _token_error("invalid_grant", "PKCE verification failed")
        self._used_codes.add(claims["jti"])
        return self._issue_tokens(
            request, claims.get("scope", SCOPE), claims.get("resource", self.resource(request)), client_id
        )

    def _grant_refresh_token(self, request: Request, form) -> Response:
        token = form.get("refresh_token", "")
        try:
            claims = self._decode(token)
        except jwt.PyJWTError:
            return _token_error("invalid_grant", "refresh token invalid or expired")
        if claims.get("typ") != "refresh":
            return _token_error("invalid_grant")
        return self._issue_tokens(
            request, claims.get("scope", SCOPE), claims.get("aud", self.resource(request)),
            claims.get("client_id", ""),
        )

    # -- route wiring ------------------------------------------------------ #
    def routes(self) -> list[Route]:
        return [
            Route("/.well-known/oauth-protected-resource", self.protected_resource_metadata),
            Route("/.well-known/oauth-protected-resource{rest:path}", self.protected_resource_metadata),
            Route("/.well-known/oauth-authorization-server", self.authorization_server_metadata),
            Route("/.well-known/oauth-authorization-server{rest:path}", self.authorization_server_metadata),
            Route("/register", self.register, methods=["POST"]),
            Route("/authorize", self.authorize_get, methods=["GET"]),
            Route("/authorize", self.authorize_post, methods=["POST"]),
            Route("/token", self.token, methods=["POST"]),
        ]

    def mount(self, app) -> None:
        for route in self.routes():
            app.router.routes.append(route)


def _token_error(error: str, description: str = "") -> Response:
    body = {"error": error}
    if description:
        body["error_description"] = description
    return JSONResponse(body, status_code=400, headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------------- #
# Minimal HTML
# --------------------------------------------------------------------------- #
_STYLE = """
body{font-family:system-ui,sans-serif;background:#0f1115;color:#e6e6e6;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
.card{background:#191c24;padding:2rem;border-radius:12px;max-width:380px;width:90%;
box-shadow:0 10px 40px rgba(0,0,0,.4)}
h1{font-size:1.2rem;margin:0 0 .25rem} p{color:#9aa0aa;font-size:.9rem;line-height:1.4}
input{width:100%;box-sizing:border-box;padding:.7rem;margin:.75rem 0;border-radius:8px;
border:1px solid #333;background:#0f1115;color:#e6e6e6;font-size:1rem}
button{width:100%;padding:.7rem;border:0;border-radius:8px;background:#4f7cff;color:#fff;
font-size:1rem;font-weight:600;cursor:pointer}
.err{color:#ff6b6b;font-size:.85rem;margin:.5rem 0 0}
"""

_HIDDEN_FIELDS = (
    "client_id", "redirect_uri", "state", "code_challenge",
    "code_challenge_method", "scope", "resource", "response_type",
)


def _authorize_page(params, error: Optional[str]) -> str:
    hidden = "".join(
        f'<input type="hidden" name="{f}" value="{html.escape(str(params.get(f, "") or ""), quote=True)}">'
        for f in _HIDDEN_FIELDS
    )
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    client = html.escape(str(params.get("client_id", "a client")))
    return f"""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Authorize LLMarr</title><style>{_STYLE}</style></head><body>
<form class="card" method="post" action="/authorize">
<h1>Authorize access to LLMarr</h1>
<p>{client} is requesting access to your media server. Paste your LLMarr access
token to approve.</p>
{hidden}
<input type="password" name="token" placeholder="LLMarr access token" autofocus autocomplete="off">
{err}
<button type="submit">Authorize</button>
</form></body></html>"""


def _error_page(message: str) -> str:
    return f"""<!doctype html><html><head><title>LLMarr</title><style>{_STYLE}</style></head>
<body><div class="card"><h1>Authorization error</h1><p>{html.escape(message)}</p></div></body></html>"""
