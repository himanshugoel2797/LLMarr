"""Jikan (MyAnimeList) provider tests via httpx.MockTransport."""

import httpx
import pytest

from llmarr.metadata import jikan


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    """Skip real rate-limit pacing for provider behaviour tests."""
    monkeypatch.setattr(jikan, "_LIMITER", jikan._NullRateLimiter())


def handler_for(routes, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url.path)
        path = request.url.path
        for prefix, payload in routes.items():
            if path == prefix:
                status = payload[0] if isinstance(payload, tuple) else 200
                body = payload[1] if isinstance(payload, tuple) else payload
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={"status": 404})
    return handler


SEARCH = {
    "/v4/anime": {
        "data": [
            {
                "mal_id": 52991,
                "title": "Sousou no Frieren",
                "title_english": "Frieren: Beyond Journey's End",
                "type": "TV",
                "episodes": 28,
                "synopsis": "…",
                "aired": {"prop": {"from": {"year": 2023}}},
                "images": {"jpg": {"image_url": "http://img/f.jpg"}},
            }
        ]
    }
}
DETAIL = {
    "/v4/anime/52991": {
        "data": {
            "mal_id": 52991,
            "title": "Sousou no Frieren",
            "title_english": "Frieren: Beyond Journey's End",
            "type": "TV",
            "episodes": 28,
            "status": "Finished Airing",
            "aired": {"prop": {"from": {"year": 2023}}},
            "images": {"jpg": {"image_url": "http://img/f.jpg"}},
        }
    }
}
EPISODES = {
    "/v4/anime/52991/episodes": {
        "data": [
            {"mal_id": 1, "title": "The Journey's End", "aired": "2023-09-29T00:00:00+00:00"},
            {"mal_id": 2, "title": "It Didn't Have to Be Magic", "aired": "2023-09-29T00:00:00+00:00"},
        ],
        "pagination": {"has_next_page": False},
    }
}


async def test_search_series_prefers_english_title(mock_httpx):
    mock_httpx(jikan, handler_for(SEARCH))
    p = jikan.JikanProvider()
    res = await p.search_series("frieren")
    assert res[0].provider == "jikan"
    assert res[0].provider_id == "52991"
    assert res[0].title == "Frieren: Beyond Journey's End"
    assert res[0].year == 2023


async def test_get_series_flattens_episodes_as_season_1(mock_httpx):
    mock_httpx(jikan, handler_for({**DETAIL, **EPISODES}))
    p = jikan.JikanProvider()
    info = await p.get_series("52991")
    assert info.provider == "jikan"
    assert info.seasons == [1]
    assert [(e.season, e.episode, e.title) for e in info.episodes] == [
        (1, 1, "The Journey's End"),
        (1, 2, "It Didn't Have to Be Magic"),
    ]
    assert info.episodes[0].air_date == "2023-09-29"


async def test_get_series_falls_back_to_count_when_no_episode_list(mock_httpx):
    routes = {
        "/v4/anime/1": {"data": {"mal_id": 1, "title": "X", "episodes": 3, "type": "TV"}},
        "/v4/anime/1/episodes": {"data": [], "pagination": {"has_next_page": False}},
    }
    mock_httpx(jikan, handler_for(routes))
    p = jikan.JikanProvider()
    info = await p.get_series("1")
    assert [e.episode for e in info.episodes] == [1, 2, 3]
    assert all(e.title is None for e in info.episodes)


@pytest.fixture
def no_sleep(monkeypatch):
    async def _instant(_):
        return None
    monkeypatch.setattr(jikan.asyncio, "sleep", _instant)


async def test_retries_on_504_then_succeeds(mock_httpx, no_sleep):
    state = {"n": 0}

    def handler(request):
        if request.url.path == "/v4/anime":
            state["n"] += 1
            if state["n"] < 3:
                return httpx.Response(504, json={"status": 504})
            return httpx.Response(200, json=SEARCH["/v4/anime"])
        return httpx.Response(404, json={})

    mock_httpx(jikan, handler)
    p = jikan.JikanProvider()
    res = await p.search_series("frieren")
    assert state["n"] == 3  # two 504s then success
    assert res[0].provider_id == "52991"


async def test_persistent_5xx_raises_clear_error(mock_httpx, no_sleep):
    mock_httpx(jikan, lambda r: httpx.Response(503, json={"status": 503}))
    p = jikan.JikanProvider()
    with pytest.raises(RuntimeError, match="not responding"):
        await p.search_series("anything")


@pytest.fixture
def fake_clock(monkeypatch):
    """Deterministic clock: monotonic() is controllable and sleep() advances it."""
    clock = {"t": 0.0}
    monkeypatch.setattr(jikan.time, "monotonic", lambda: clock["t"])

    async def fake_sleep(d):
        clock["t"] += max(d, 0.0)

    monkeypatch.setattr(jikan.asyncio, "sleep", fake_sleep)
    return clock


async def test_rate_limiter_enforces_per_second(fake_clock):
    rl = jikan._RateLimiter(per_second=3, per_minute=1000)
    for _ in range(3):
        await rl.acquire()
    assert fake_clock["t"] == 0.0  # first 3 in the same second: no wait
    await rl.acquire()  # 4th must wait out the 1s window
    assert fake_clock["t"] >= 1.0


async def test_rate_limiter_enforces_per_minute(fake_clock):
    rl = jikan._RateLimiter(per_second=1000, per_minute=60)
    for _ in range(60):
        await rl.acquire()
        fake_clock["t"] += 0.001  # advance so the per-second cap never binds
    assert fake_clock["t"] < 1.0
    await rl.acquire()  # 61st must wait until the minute window frees
    assert fake_clock["t"] >= 60.0


async def test_get_movie(mock_httpx):
    routes = {"/v4/anime/5114": {"data": {
        "mal_id": 5114, "title": "Some Movie", "type": "Movie", "status": "Finished Airing",
        "aired": {"prop": {"from": {"year": 2011}}},
    }}}
    mock_httpx(jikan, handler_for(routes))
    p = jikan.JikanProvider()
    info = await p.get_movie("5114")
    assert info.title == "Some Movie" and info.year == 2011
