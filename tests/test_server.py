"""Server-level tests: tool registration and a few tools exercised through the
module functions with the app state injected."""

import pytest

from llmarr import server


@pytest.fixture
def wired(app, monkeypatch):
    """Point the server module's global state at a test app."""
    monkeypatch.setattr(server.state, "app", app, raising=False)
    return app


async def test_expected_tools_registered():
    tools = await server.mcp.list_tools()
    names = {t.name for t in tools}
    expected = {
        "get_config", "configure_metadata", "configure_prowlarr",
        "configure_download_client", "configure_plex", "configure_import",
        "add_path_mapping", "translate_path", "test_connections",
        "search_series", "add_series", "list_series", "get_series",
        "search_movies", "add_movie", "grab_movie",
        "search_releases", "grab_release", "grab_episode",
        "refresh_downloads", "import_download", "rss_poll_now", "rss_status",
    }
    missing = expected - names
    assert not missing, f"missing tools: {missing}"


async def test_every_tool_has_description():
    tools = await server.mcp.list_tools()
    assert all(t.description for t in tools)


def test_get_config_redacts(wired):
    wired.store.mutate(lambda c: setattr(c.prowlarr, "api_key", "SECRET"))
    cfg = server.get_config()
    assert cfg["prowlarr"]["api_key"] == "***set***"


def test_add_and_translate_path(wired):
    server.add_path_mapping("dl", "qbittorrent", "/downloads")
    server.add_path_mapping("dl", "local", "/mnt/dl")
    maps = server.list_path_mappings()
    assert len(maps) == 2
    res = server.translate_path("/downloads/x.mkv", "qbittorrent", "local")
    assert res["result"] == "/mnt/dl/x.mkv"


def test_add_path_mapping_replaces_same_leg(wired):
    server.add_path_mapping("dl", "local", "/old")
    server.add_path_mapping("dl", "local", "/new")
    maps = [m for m in server.list_path_mappings() if m["context"] == "local"]
    assert len(maps) == 1 and maps[0]["path"] == "/new"


def test_configure_download_client_sets_default(wired):
    out = server.configure_download_client("qbit", url="http://qb", password="pw")
    assert out["url"] == "http://qb"
    assert out["password"] == "***set***"
    assert wired.config.default_download_client == "qbit"


def test_configure_import(wired):
    out = server.configure_import(mode="copy", min_video_mb=200)
    assert out["mode"] == "copy" and out["min_video_mb"] == 200


def test_get_series_not_found(wired):
    assert "error" in server.get_series(999)


async def test_guard_converts_exception_to_error_dict(wired):
    # Default provider is tmdb with no key -> provider() raises ValueError, which
    # the @tool guard turns into an {"error", "hint"} dict instead of surfacing.
    out = await server.search_series("anything")
    assert isinstance(out, dict) and "error" in out
    assert "hint" in out  # ValueError -> configure hint


def test_configure_clears_with_empty_string(wired):
    server.configure_prowlarr(url="http://p", api_key="k")
    assert wired.config.prowlarr.url == "http://p"
    server.configure_prowlarr(url="")  # clear just the url
    assert wired.config.prowlarr.url is None
    assert wired.config.prowlarr.api_key == "k"  # untouched (None = leave)


def test_auth_token_consolidated(wired):
    assert server.auth_token("get")["configured"] is False
    server.auth_token("set", "tok")
    assert wired.config.server.auth_token == "tok"


def test_remove_series(wired):
    sid = wired.db.upsert_series(provider="tmdb", provider_id="1", title="Show")
    out = server.remove_series(sid)
    assert out["removed"] == sid
    assert wired.db.get_series(sid) is None
