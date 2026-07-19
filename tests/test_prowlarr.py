"""Prowlarr client tests driven by an httpx.MockTransport."""

import httpx

from llmarr.indexers import prowlarr


def test_parse_release_maps_fields():
    item = {
        "guid": "g1", "title": "Show.S01E01.1080p", "indexer": "X", "indexerId": 3,
        "size": 2000, "seeders": 40, "leechers": 2,
        "downloadUrl": "http://dl", "magnetUrl": "magnet:?xt=urn:btih:abc",
        "infoUrl": "http://info", "protocol": "torrent",
        "categories": [{"id": 5040, "name": "TV/HD"}],
    }
    r = prowlarr.ProwlarrClient._parse(item)
    assert r.guid == "g1" and r.indexer_id == 3 and r.seeders == 40
    assert r.categories == [5040]
    # magnet preferred as grab url
    assert r.grab_url.startswith("magnet:")
    assert r.size_mb == 2000 / (1024 * 1024)


def test_grab_url_falls_back_to_download_url():
    r = prowlarr.Release(guid="g", title="t", download_url="http://dl")
    assert r.grab_url == "http://dl"


def test_requires_url_and_key():
    import pytest

    with pytest.raises(ValueError):
        prowlarr.ProwlarrClient(url=None, api_key="k")
    with pytest.raises(ValueError):
        prowlarr.ProwlarrClient(url="http://x", api_key=None)


async def test_search_filters_non_torrent_and_caps(mock_httpx):
    payload = [
        {"guid": "1", "title": "torrent one", "protocol": "torrent", "downloadUrl": "u1"},
        {"guid": "2", "title": "usenet", "protocol": "usenet", "downloadUrl": "u2"},
        {"guid": "3", "title": "torrent two", "protocol": "torrent", "magnetUrl": "magnet:x"},
    ]
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["query"] = str(request.url.query)
        captured["api_key"] = request.headers.get("X-Api-Key")
        return httpx.Response(200, json=payload)

    mock_httpx(prowlarr, handler)
    client = prowlarr.ProwlarrClient(url="http://prowlarr:9696", api_key="secret")
    results = await client.search("show", categories=[5000], indexer_ids=[2])

    assert [r.title for r in results] == ["torrent one", "torrent two"]  # usenet dropped
    assert captured["path"] == "/api/v1/search"
    assert captured["api_key"] == "secret"
    assert "categories=5000" in captured["query"]
    assert "indexerIds=2" in captured["query"]


async def test_test_reports_indexers(mock_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/indexer"
        return httpx.Response(200, json=[
            {"id": 1, "name": "A", "enable": True},
            {"id": 2, "name": "B", "enable": False},
        ])

    mock_httpx(prowlarr, handler)
    client = prowlarr.ProwlarrClient(url="http://p", api_key="k")
    res = await client.test()
    assert res["ok"] and res["indexer_count"] == 2
