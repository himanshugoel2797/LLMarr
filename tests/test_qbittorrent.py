"""Pure-function tests for the qBittorrent helper (no network)."""

from llmarr.download.qbittorrent import magnet_hash


def test_magnet_hash_40_hex():
    h = "a" * 40
    assert magnet_hash(f"magnet:?xt=urn:btih:{h}&dn=x") == h


def test_magnet_hash_case_normalized():
    h = "A" * 40
    assert magnet_hash(f"magnet:?xt=urn:btih:{h}") == "a" * 40


def test_magnet_hash_base32():
    h = "A" * 32  # 32-char base32 form
    assert magnet_hash(f"magnet:?xt=urn:btih:{h}") == h.lower()


def test_magnet_hash_none_for_plain_url():
    assert magnet_hash("http://example.com/x.torrent") is None
    assert magnet_hash("") is None


def _mock_client(monkeypatch, handler):
    import httpx

    real = httpx.Client

    def factory(*a, **k):
        k["transport"] = httpx.MockTransport(handler)
        return real(*a, **k)

    # _resolve_torrent does `import httpx; httpx.Client(...)`, so patch the module.
    monkeypatch.setattr(httpx, "Client", factory)


def test_resolve_torrent_follows_redirect_to_magnet(monkeypatch):
    import httpx

    from llmarr.download.qbittorrent import _resolve_torrent
    magnet = "magnet:?xt=urn:btih:" + "a" * 40

    def handler(req):
        return httpx.Response(302, headers={"location": magnet})

    _mock_client(monkeypatch, handler)
    kind, payload = _resolve_torrent("http://prowlarr/download?link=x")
    assert kind == "magnet" and payload == magnet


def test_resolve_torrent_fetches_file(monkeypatch):
    import httpx

    from llmarr.download.qbittorrent import _resolve_torrent

    def handler(req):
        return httpx.Response(200, content=b"d8:announce...e")  # bencoded

    _mock_client(monkeypatch, handler)
    kind, payload = _resolve_torrent("http://prowlarr/download?link=x")
    assert kind == "file" and payload.startswith(b"d")


def test_resolve_torrent_falls_back_to_url(monkeypatch):
    import httpx

    from llmarr.download.qbittorrent import _resolve_torrent

    def handler(req):
        return httpx.Response(200, content=b"<html>not a torrent</html>")

    _mock_client(monkeypatch, handler)
    kind, payload = _resolve_torrent("http://x/y")
    assert kind == "url"
