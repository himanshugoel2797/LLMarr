"""TMDB provider tests driven by an httpx.MockTransport."""

import httpx
import pytest

from llmarr.metadata import tmdb


def make_handler(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for prefix, payload in routes.items():
            if path == prefix or path.startswith(prefix):
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"status_message": "not found"})
    return handler


def test_requires_api_key():
    with pytest.raises(ValueError):
        tmdb.TMDBProvider(api_key=None)


async def test_search_series(mock_httpx):
    routes = {
        "/3/search/tv": {
            "results": [
                {"id": 95396, "name": "Meridian", "first_air_date": "2022-02-18",
                 "overview": "o", "poster_path": "/p.jpg"},
                {"id": 1, "original_name": "Alt", "first_air_date": ""},
            ]
        }
    }
    mock_httpx(tmdb, make_handler(routes))
    p = tmdb.TMDBProvider(api_key="k")
    res = await p.search_series("meridian")
    assert res[0].title == "Meridian"
    assert res[0].year == 2022
    assert res[0].provider_id == "95396"
    assert res[0].poster.endswith("/p.jpg")
    assert res[1].title == "Alt" and res[1].year is None


async def test_get_series_includes_specials(mock_httpx):
    routes = {
        "/3/tv/95396/season/0": {
            "episodes": [{"episode_number": 1, "name": "Special", "air_date": "2022-01-01"}]
        },
        "/3/tv/95396/season/1": {
            "episodes": [
                {"episode_number": 1, "name": "Good News", "air_date": "2022-02-18"},
                {"episode_number": 2, "name": "Half Loop", "air_date": "2022-02-18"},
            ]
        },
        "/3/tv/95396": {
            "id": 95396, "name": "Meridian", "first_air_date": "2022-02-18",
            "status": "Returning Series",
            "seasons": [{"season_number": 1}, {"season_number": 0}],
        },
    }
    mock_httpx(tmdb, make_handler(routes))
    p = tmdb.TMDBProvider(api_key="k")
    info = await p.get_series("95396")
    assert info.seasons == [0, 1]
    # Specials (season 0) are now included, ordered first.
    assert [(e.season, e.episode) for e in info.episodes] == [(0, 1), (1, 1), (1, 2)]


async def test_search_movies(mock_httpx):
    routes = {
        "/3/search/movie": {
            "results": [
                {"id": 438631, "title": "Nebula", "release_date": "2021-10-22", "overview": "o"},
            ]
        }
    }
    mock_httpx(tmdb, make_handler(routes))
    p = tmdb.TMDBProvider(api_key="k")
    res = await p.search_movies("nebula")
    assert res[0].title == "Nebula" and res[0].year == 2021
    assert res[0].provider_id == "438631"


async def test_get_movie(mock_httpx):
    routes = {"/3/movie/438631": {"id": 438631, "title": "Nebula", "release_date": "2021-10-22",
                                   "status": "Released"}}
    mock_httpx(tmdb, make_handler(routes))
    p = tmdb.TMDBProvider(api_key="k")
    info = await p.get_movie("438631")
    assert info.title == "Nebula" and info.year == 2021 and info.status == "Released"
