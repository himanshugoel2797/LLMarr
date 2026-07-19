"""OAuth 2.1 authorization-server + resource-server tests, driven over ASGI."""

import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from llmarr import oauth
from llmarr.auth import AuthMiddleware
from llmarr.config import ConfigStore
from llmarr.db import Database
from llmarr.oauth import OAuthProvider, pkce_s256

VERIFIER = "test-verifier-0123456789-0123456789-0123456789"
CHALLENGE = pkce_s256(VERIFIER)


@pytest.fixture
def provider(tmp_path):
    store = ConfigStore(tmp_path / "config.yaml")
    store.mutate(lambda c: setattr(c.server, "auth_token", "SECRET"))
    store.mutate(lambda c: setattr(c.server, "auth_mode", "oauth"))
    db = Database(tmp_path / "db.sqlite")
    return OAuthProvider(store, db, mcp_path="/mcp")


@pytest.fixture
def client(provider):
    async def mcp_stub(request):
        return PlainTextResponse("mcp-ok")

    app = Starlette(routes=[Route("/mcp", mcp_stub, methods=["GET", "POST"])])
    provider.mount(app)
    app.add_middleware(AuthMiddleware, store=provider.store, oauth_provider=provider)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


# --------------------------------------------------------------------------- #
# Units
# --------------------------------------------------------------------------- #
def test_pkce_s256_known_vector():
    # RFC 7636 appendix B
    v = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert pkce_s256(v) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_is_public_path():
    assert oauth.is_public_path("/authorize")
    assert oauth.is_public_path("/.well-known/oauth-protected-resource")
    assert not oauth.is_public_path("/mcp")


def test_verify_access_token(provider):
    now = int(time.time())
    key = provider.signing_key()
    good = jwt.encode({"typ": "access", "exp": now + 60}, key, algorithm="HS256")
    assert provider.verify_access_token(good)["typ"] == "access"
    # refresh token is not an access token
    refresh = jwt.encode({"typ": "refresh", "exp": now + 60}, key, algorithm="HS256")
    assert provider.verify_access_token(refresh) is None
    # expired
    expired = jwt.encode({"typ": "access", "exp": now - 5}, key, algorithm="HS256")
    assert provider.verify_access_token(expired) is None
    # wrong signature
    forged = jwt.encode({"typ": "access", "exp": now + 60}, "other-key", algorithm="HS256")
    assert provider.verify_access_token(forged) is None


def test_rotating_signing_key_invalidates_tokens(provider):
    now = int(time.time())
    token = jwt.encode({"typ": "access", "exp": now + 60}, provider.signing_key(),
                       algorithm="HS256")
    assert provider.verify_access_token(token)  # valid before rotation
    # Rotate the signing key (what rotate_oauth_keys does).
    import secrets
    provider.store.mutate(
        lambda c: setattr(c.server, "oauth_signing_key", secrets.token_urlsafe(48))
    )
    assert provider.verify_access_token(token) is None  # now rejected


def test_clear_oauth_clients(provider):
    provider.db.add_oauth_client("cid", "Claude", '["https://x/cb"]')
    assert provider.db.count_oauth_clients() == 1
    assert provider.db.get_oauth_client("cid") is not None
    assert provider.db.clear_oauth_clients() == 1
    assert provider.db.count_oauth_clients() == 0
    assert provider.db.get_oauth_client("cid") is None


# --------------------------------------------------------------------------- #
# Discovery + DCR
# --------------------------------------------------------------------------- #
async def test_protected_resource_metadata(client):
    async with client as c:
        r = await c.get("/.well-known/oauth-protected-resource")
    assert r.status_code == 200
    data = r.json()
    assert data["resource"] == "http://testserver/mcp"
    assert data["authorization_servers"] == ["http://testserver"]


async def test_authorization_server_metadata(client):
    async with client as c:
        r = await c.get("/.well-known/oauth-authorization-server")
    data = r.json()
    assert data["issuer"] == "http://testserver"
    assert data["authorization_endpoint"] == "http://testserver/authorize"
    assert data["code_challenge_methods_supported"] == ["S256"]
    assert data["token_endpoint_auth_methods_supported"] == ["none"]


async def test_dynamic_client_registration(client):
    async with client as c:
        r = await c.post("/register", json={
            "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
            "client_name": "Claude",
        })
    assert r.status_code == 201
    body = r.json()
    assert body["client_id"].startswith("llmarr-")
    assert body["token_endpoint_auth_method"] == "none"


async def test_register_requires_redirect_uris(client):
    async with client as c:
        r = await c.post("/register", json={"client_name": "x"})
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Full authorization-code + PKCE flow
# --------------------------------------------------------------------------- #
async def _register(c, redirect="https://claude.ai/api/mcp/auth_callback"):
    r = await c.post("/register", json={"redirect_uris": [redirect]})
    return r.json()["client_id"], redirect


def _authorize_params(client_id, redirect):
    return {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect,
        "state": "xyz",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "scope": "mcp",
    }


async def test_authorize_page_renders(client):
    async with client as c:
        cid, redirect = await _register(c)
        r = await c.get("/authorize", params=_authorize_params(cid, redirect))
    assert r.status_code == 200
    assert "Authorize access to LLMarr" in r.text
    assert 'name="code_challenge"' in r.text


async def test_authorize_rejects_unknown_client(client):
    async with client as c:
        r = await c.get("/authorize", params=_authorize_params("bogus", "https://claude.ai/api/mcp/auth_callback"))
    assert r.status_code == 400


async def test_authorize_wrong_token_reprompts(client):
    async with client as c:
        cid, redirect = await _register(c)
        form = {**_authorize_params(cid, redirect), "token": "WRONG"}
        r = await c.post("/authorize", data=form)
    assert r.status_code == 401
    assert "Incorrect token" in r.text


async def _full_flow_code(c):
    cid, redirect = await _register(c)
    form = {**_authorize_params(cid, redirect), "token": "SECRET"}
    r = await c.post("/authorize", data=form)
    assert r.status_code == 302, r.text
    loc = r.headers["location"]
    qs = parse_qs(urlparse(loc).query)
    assert qs["state"] == ["xyz"]
    return cid, redirect, qs["code"][0]


async def test_token_exchange_and_resource_access(client):
    async with client as c:
        cid, redirect, code = await _full_flow_code(c)
        r = await c.post("/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect,
            "client_id": cid,
            "code_verifier": VERIFIER,
        })
        assert r.status_code == 200, r.text
        tok = r.json()
        assert tok["token_type"] == "Bearer" and tok["access_token"]
        access, refresh = tok["access_token"], tok["refresh_token"]

        # Protected resource: no token -> 401 w/ resource_metadata pointer
        r401 = await c.get("/mcp")
        assert r401.status_code == 401
        assert "resource_metadata=" in r401.headers["www-authenticate"]

        # With the OAuth access token -> allowed
        ok = await c.get("/mcp", headers={"Authorization": f"Bearer {access}"})
        assert ok.status_code == 200 and ok.text == "mcp-ok"

        # Refresh yields a fresh, working access token
        rr = await c.post("/token", data={"grant_type": "refresh_token", "refresh_token": refresh})
        assert rr.status_code == 200
        new_access = rr.json()["access_token"]
        ok2 = await c.get("/mcp", headers={"Authorization": f"Bearer {new_access}"})
        assert ok2.status_code == 200


async def test_static_token_still_accepted_in_oauth_mode(client):
    async with client as c:
        ok = await c.get("/mcp", headers={"Authorization": "Bearer SECRET"})
    assert ok.status_code == 200  # Claude Code's static header keeps working


async def test_token_bad_pkce_rejected(client):
    async with client as c:
        cid, redirect, code = await _full_flow_code(c)
        r = await c.post("/token", data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
            "client_id": cid, "code_verifier": "wrong-verifier",
        })
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


async def test_token_code_single_use(client):
    async with client as c:
        cid, redirect, code = await _full_flow_code(c)
        data = {
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
            "client_id": cid, "code_verifier": VERIFIER,
        }
        first = await c.post("/token", data=data)
        second = await c.post("/token", data=data)
    assert first.status_code == 200
    assert second.status_code == 400 and "already used" in second.json()["error_description"]


async def test_unsupported_grant_type(client):
    async with client as c:
        r = await c.post("/token", data={"grant_type": "password"})
    assert r.status_code == 400 and r.json()["error"] == "unsupported_grant_type"
