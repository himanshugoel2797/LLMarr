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
