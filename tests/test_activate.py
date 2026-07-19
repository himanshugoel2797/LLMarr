"""Tests for activate_series — fetching episodes for a catalogued show and
marking the ones Plex already has as downloaded."""

import pytest

from tests.conftest import FakePlex
from llmarr.metadata.base import EpisodeInfo, SeriesInfo


def _anime_info(n=28):
    return SeriesInfo(
        provider="jikan", provider_id="52991", title="Aethering", year=2023, seasons=[1],
        episodes=[EpisodeInfo(season=1, episode=i, title=f"E{i}") for i in range(1, n + 1)],
    )


@pytest.fixture
def plex_configured(app):
    app.store.mutate(lambda c: (setattr(c.plex, "url", "http://plex"),
                                setattr(c.plex, "token", "t")))
    return app


async def test_activate_absolute_marks_present_episodes(plex_configured, fakes, monkeypatch):
    app = plex_configured
    # Catalogued from Plex: provider="plex", no episodes, absolute (anime section).
    sid = app.db.upsert_series(
        provider="plex", provider_id="101", title="Aethering", absolute_numbering=1
    )
    # Plex currently has 12 aired episodes on disk.
    plex = FakePlex()
    plex._show_episodes = [(1, i) for i in range(1, 13)]
    monkeypatch.setattr(app, "plex", lambda: plex)
    monkeypatch.setattr(
        app, "provider", lambda *_a, **_k: fakes["Provider"](
            series_info=_anime_info(28), absolute_numbering=True
        )
    )

    res = await app.activate_series(sid, provider="jikan", provider_id="52991")
    assert res["episodes"] == 28
    assert res["marked_downloaded"] == 12
    assert res["still_missing"] == 16
    # series re-keyed to the metadata provider AND now monitored
    row = app.db.get_series(sid)
    assert row["provider"] == "jikan" and row["provider_id"] == "52991"
    assert row["monitored"] == 1
    # episodes 1..12 downloaded, 13..28 missing
    eps = {e["episode"]: e["status"] for e in app.db.list_episodes(sid)}
    assert eps[12] == "downloaded" and eps[13] == "missing"


async def test_activate_standard_matches_season_episode(plex_configured, fakes, monkeypatch):
    app = plex_configured
    sid = app.db.upsert_series(provider="plex", provider_id="200", title="Show")
    info = SeriesInfo(
        provider="tmdb", provider_id="55", title="Show", seasons=[1, 2],
        episodes=[EpisodeInfo(season=1, episode=1), EpisodeInfo(season=1, episode=2),
                  EpisodeInfo(season=2, episode=1)],
    )
    plex = FakePlex()
    plex._show_episodes = [(1, 1), (1, 2)]  # only season 1 on disk
    monkeypatch.setattr(app, "plex", lambda: plex)
    monkeypatch.setattr(app, "provider", lambda *_a, **_k: fakes["Provider"](series_info=info))

    res = await app.activate_series(sid, provider="tmdb", provider_id="55")
    assert res["marked_downloaded"] == 2 and res["still_missing"] == 1
    eps = {(e["season"], e["episode"]): e["status"] for e in app.db.list_episodes(sid)}
    assert eps[(2, 1)] == "missing"


async def test_activate_requires_provider_for_plex_only_entry(plex_configured):
    app = plex_configured
    sid = app.db.upsert_series(provider="plex", provider_id="9", title="X")
    res = await app.activate_series(sid)  # no provider/id, series has none usable
    assert "error" in res and "provider" in res["error"]


async def test_activate_conflict_returns_error(plex_configured, fakes, monkeypatch):
    app = plex_configured
    app.db.upsert_series(provider="jikan", provider_id="52991", title="Existing Aethering")
    sid = app.db.upsert_series(provider="plex", provider_id="101", title="Aethering", absolute_numbering=1)
    monkeypatch.setattr(
        app, "provider", lambda *_a, **_k: fakes["Provider"](
            series_info=_anime_info(3), absolute_numbering=True
        )
    )
    res = await app.activate_series(sid, provider="jikan", provider_id="52991")
    assert "error" in res and "already uses" in res["error"]


async def test_activate_no_plex_still_adds_episodes(app, fakes, monkeypatch):
    # Plex not configured -> episodes fetched, none marked downloaded.
    sid = app.db.upsert_series(provider="plex", provider_id="101", title="Aethering", absolute_numbering=1)
    monkeypatch.setattr(
        app, "provider", lambda *_a, **_k: fakes["Provider"](
            series_info=_anime_info(5), absolute_numbering=True
        )
    )
    res = await app.activate_series(sid, provider="jikan", provider_id="52991")
    assert res["episodes"] == 5 and res["marked_downloaded"] == 0 and res["still_missing"] == 5
