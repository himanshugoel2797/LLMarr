"""Shared fixtures + fakes for the LLMarr test suite.

Everything network-facing (metadata, Prowlarr, qBittorrent, Plex) is faked so
tests are fast and offline. ``mock_httpx`` lets the few tests that exercise the
real HTTP client code drive it with an ``httpx.MockTransport``.
"""

from __future__ import annotations

import httpx
import pytest

from llmarr.config import ConfigStore
from llmarr.core import App
from llmarr.db import Database
from llmarr.download.base import TorrentStatus
from llmarr.indexers.prowlarr import Release
from llmarr.metadata.base import (
    EpisodeInfo,
    MovieInfo,
    MovieSearchResult,
    SeriesInfo,
    SeriesSearchResult,
)


# --------------------------------------------------------------------------- #
# Core fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def store(tmp_path) -> ConfigStore:
    return ConfigStore(tmp_path / "config.yaml")


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(tmp_path / "llmarr.db")


@pytest.fixture
def app(store, db) -> App:
    return App(store, db)


# --------------------------------------------------------------------------- #
# httpx mocking
# --------------------------------------------------------------------------- #
@pytest.fixture
def mock_httpx(monkeypatch):
    """Return a function that installs a MockTransport handler onto the given
    module's ``httpx.AsyncClient`` usage."""

    def install(module, handler):
        real = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real(*args, **kwargs)

        monkeypatch.setattr(module.httpx, "AsyncClient", factory)

    return install


# --------------------------------------------------------------------------- #
# Fakes for the service layer
# --------------------------------------------------------------------------- #
def make_release(title, guid=None, seeders=100, size=1_000_000_000,
                 magnet="magnet:?xt=urn:btih:" + "a" * 40, indexer="idx",
                 protocol="torrent"):
    return Release(
        guid=guid or title,
        title=title,
        indexer=indexer,
        size=size,
        seeders=seeders,
        magnet_url=magnet,
        protocol=protocol,
    )


class FakeProvider:
    name = "fake"

    def __init__(self, series=None, movies=None, series_info=None, movie_info=None,
                 absolute_numbering=False):
        self.absolute_numbering = absolute_numbering
        self._series = series or []
        self._movies = movies or []
        self._series_info = series_info
        self._movie_info = movie_info

    async def search_series(self, query):
        return self._series

    async def get_series(self, provider_id):
        return self._series_info

    async def search_movies(self, query):
        return self._movies

    async def get_movie(self, provider_id):
        return self._movie_info


class FakeProwlarr:
    def __init__(self, releases=None, raises=None):
        self._releases = releases or []
        self._raises = raises
        self.searches = []

    async def search(self, query, categories=None, indexer_ids=None, limit=100):
        self.searches.append((query, tuple(categories or [])))
        if self._raises:
            raise self._raises
        return list(self._releases)

    async def test(self):
        return {"ok": True, "indexer_count": len(self._releases)}


class FakeDownloadClient:
    def __init__(self, complete=False, content_path="/downloads/x.mkv"):
        self.added = []
        self.removed = []
        self._complete = complete
        self._content_path = content_path

    def add(self, url, category=None, save_path=None):
        self.added.append((url, category, save_path))
        return "b" * 40

    def status(self, torrent_hash):
        return TorrentStatus(
            hash=torrent_hash,
            name="x",
            state="uploading" if self._complete else "downloading",
            progress=1.0 if self._complete else 0.3,
            content_path=self._content_path,
            save_path="/downloads",
            completed=self._complete,
        )

    def list(self, category=None):
        return []

    def remove(self, torrent_hash, delete_files=False):
        self.removed.append((torrent_hash, delete_files))

    def test(self):
        return {"ok": True, "version": "fake"}


class FakePlex:
    def __init__(self):
        self.scans = []

    def scan(self, section=None, path=None):
        self.scans.append((section, path))
        return {"ok": True, "section": section, "path": path}

    def test(self):
        return {"ok": True, "friendly_name": "fake"}


@pytest.fixture
def fakes():
    """Bundle of fake service instances + constructors used across tests."""
    return {
        "Provider": FakeProvider,
        "Prowlarr": FakeProwlarr,
        "DownloadClient": FakeDownloadClient,
        "Plex": FakePlex,
        "make_release": make_release,
        "SeriesInfo": SeriesInfo,
        "SeriesSearchResult": SeriesSearchResult,
        "EpisodeInfo": EpisodeInfo,
        "MovieInfo": MovieInfo,
        "MovieSearchResult": MovieSearchResult,
    }
