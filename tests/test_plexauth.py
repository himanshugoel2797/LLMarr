"""Tests for the Plex PIN (browser) login flow + tools."""

import httpx
import pytest

from llmarr import plexauth, server


def test_new_client_id_is_uuid():
    a, b = plexauth.new_client_id(), plexauth.new_client_id()
    assert a != b and len(a) == 36 and a.count("-") == 4


async def test_request_pin(mock_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v2/pins"
        assert request.headers["X-Plex-Client-Identifier"] == "cid-1"
        assert request.headers["X-Plex-Product"] == "LLMarr"
        return httpx.Response(201, json={"id": 999, "code": "H8ZL"})

    mock_httpx(plexauth, handler)
    pin = await plexauth.request_pin("cid-1")
    assert pin == {"id": 999, "code": "H8ZL"}


async def test_poll_token_pending_then_ready(mock_httpx):
    state = {"claimed": False}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/pins/999"
        return httpx.Response(200, json={"authToken": "TOKEN" if state["claimed"] else None})

    mock_httpx(plexauth, handler)
    assert await plexauth.poll_token(999, "cid-1") is None
    state["claimed"] = True
    assert await plexauth.poll_token(999, "cid-1") == "TOKEN"


# -- tools ------------------------------------------------------------------ #
@pytest.fixture
def wired(app, monkeypatch):
    monkeypatch.setattr(server.state, "app", app, raising=False)
    return app


async def test_plex_login_start_generates_client_id(wired, monkeypatch):
    async def fake_request_pin(cid, product="LLMarr"):
        return {"id": 42, "code": "WXYZ"}

    # The tool does `from . import plexauth` lazily, so patch the module attr.
    monkeypatch.setattr(plexauth, "request_pin", fake_request_pin)

    out = await server.plex_login_start()
    assert out["code"] == "WXYZ"
    assert out["link_url"] == "https://plex.tv/link"
    # client id persisted + pin id stored for polling
    assert wired.config.plex.client_id
    assert wired.db.get_kv("plex_pin_id") == "42"


async def test_plex_login_poll_saves_token(wired, monkeypatch):
    wired.store.mutate(lambda c: setattr(c.plex, "client_id", "cid-1"))
    wired.db.set_kv("plex_pin_id", "42")

    async def fake_poll(pin_id, cid, product="LLMarr"):
        return "THE-TOKEN"

    monkeypatch.setattr(plexauth, "poll_token", fake_poll)

    out = await server.plex_login_poll(url="http://localhost:32400", max_wait_seconds=1)
    assert out["authorized"] is True
    assert wired.config.plex.token == "THE-TOKEN"
    assert wired.config.plex.url == "http://localhost:32400"
    assert wired.db.get_kv("plex_pin_id") == ""  # cleared


async def test_plex_login_poll_without_pending(wired):
    out = await server.plex_login_poll(max_wait_seconds=1)
    assert "error" in out


async def test_plex_login_poll_times_out(wired, monkeypatch):
    wired.store.mutate(lambda c: setattr(c.plex, "client_id", "cid-1"))
    wired.db.set_kv("plex_pin_id", "42")

    async def fake_poll(pin_id, cid, product="LLMarr"):
        return None  # never approved

    monkeypatch.setattr(plexauth, "poll_token", fake_poll)
    # max_wait_seconds=0 -> one check then give up (no real sleeping)
    out = await server.plex_login_poll(max_wait_seconds=0)
    assert out["authorized"] is False