"""Tests for the HTTP bearer-token auth scheme."""

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from llmarr import auth, server
from llmarr.config import ConfigStore


def test_generate_token_is_unique_and_urlsafe():
    a, b = auth.generate_token(), auth.generate_token()
    assert a != b and len(a) > 20
    assert all(c.isalnum() or c in "-_" for c in a)


def test_ensure_token_generates_and_persists(tmp_path):
    store = ConfigStore(tmp_path / "config.yaml")
    assert store.config.server.auth_token is None
    token = auth.ensure_token(store)
    assert token
    # Persisted and stable across reloads.
    assert ConfigStore(tmp_path / "config.yaml").config.server.auth_token == token
    # Idempotent: does not regenerate.
    assert auth.ensure_token(store) == token


def _app_with_auth(token):
    async def ok(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", ok)])
    app.add_middleware(auth.BearerAuthMiddleware, token=token)
    return app


async def test_middleware_allows_correct_token():
    app = _app_with_auth("secret")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.get("/", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200 and r.text == "ok"


@pytest.mark.parametrize("header", [None, "Bearer wrong", "secret", "Basic secret"])
async def test_middleware_rejects_bad_token(header):
    app = _app_with_auth("secret")
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": header} if header else {}
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.get("/", headers=headers)
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == "Bearer"


# -- server tools ------------------------------------------------------------ #
@pytest.fixture
def wired(app, monkeypatch):
    monkeypatch.setattr(server.state, "app", app, raising=False)
    return app


def test_configure_server_toggles(wired):
    out = server.configure_server(single_host=False, require_auth=False)
    assert out == {"single_host": False, "require_auth": False}


def test_auth_token_tools(wired):
    assert server.get_auth_token()["configured"] is False
    t = server.set_auth_token("mytoken")["auth_token"]
    assert t == "mytoken"
    assert server.get_auth_token()["auth_token"] == "mytoken"
    rotated = server.rotate_auth_token()["auth_token"]
    assert rotated != "mytoken"
    assert server.get_auth_token()["auth_token"] == rotated


def test_get_config_redacts_auth_token(wired):
    server.set_auth_token("supersecret")
    assert server.get_config()["server"]["auth_token"] == "***set***"
