"""App-engine tests. Network services are faked; the DB, config and importer
(filesystem) are real."""

import pytest

from llmarr import core
from llmarr.config import DownloadClientConfig, PathMapping, RootFolder
from llmarr.metadata.base import EpisodeInfo, MovieInfo, SeriesInfo


@pytest.fixture
def configured(app):
    """App with a download client + Plex creds configured."""
    def setup(c):
        c.download_clients["qbit"] = DownloadClientConfig(url="http://qb", save_path="/downloads")
        c.default_download_client = "qbit"
        c.plex.url = "http://plex"
        c.plex.token = "t"
    app.store.mutate(setup)
    return app


# --------------------------------------------------------------------------- #
# add_series / add_movie
# --------------------------------------------------------------------------- #
async def test_add_series_populates_episodes(app, fakes, monkeypatch):
    info = SeriesInfo(
        provider="tmdb", provider_id="1", title="Severance", year=2022,
        seasons=[1, 2],
        episodes=[
            EpisodeInfo(season=1, episode=1, title="A"),
            EpisodeInfo(season=1, episode=2, title="B"),
            EpisodeInfo(season=2, episode=1, title="C"),
        ],
    )
    monkeypatch.setattr(app, "provider", lambda *_a, **_k: fakes["Provider"](series_info=info))

    result = await app.add_series("1", seasons=[2])
    assert result["title"] == "Severance"
    assert result["episode_count"] == 3
    eps = app.db.list_episodes(result["id"])
    monitored = {(e["season"], e["episode"]): e["monitored"] for e in eps}
    # Only season 2 monitored.
    assert monitored[(2, 1)] == 1
    assert monitored[(1, 1)] == 0 and monitored[(1, 2)] == 0


async def test_add_series_anime_sets_absolute_flag(app, fakes, monkeypatch):
    info = SeriesInfo(
        provider="jikan", provider_id="52991", title="Frieren", year=2023,
        seasons=[1],
        episodes=[EpisodeInfo(season=1, episode=1, title="The Journey's End")],
    )
    monkeypatch.setattr(
        app, "provider",
        lambda *_a, **_k: fakes["Provider"](series_info=info, absolute_numbering=True),
    )
    result = await app.add_series("52991", provider="jikan")
    assert app.db.get_series(result["id"])["absolute_numbering"] == 1


async def test_rss_poll_matches_anime_absolute_release(configured, fakes, monkeypatch):
    app = configured
    monkeypatch.setattr(core, "get_client", lambda cfg: fakes["DownloadClient"]())
    rels = [fakes["make_release"]("[SubsPlease] Frieren - 01 (1080p) [ABCD].mkv", guid="a1", seeders=80)]
    monkeypatch.setattr(app, "prowlarr", lambda: fakes["Prowlarr"](releases=rels))

    sid = app.db.upsert_series(
        provider="jikan", provider_id="52991", title="Frieren", monitored=1,
        absolute_numbering=1,
    )
    e = app.db.upsert_episode(sid, 1, 1)
    app.db.execute("UPDATE episodes SET monitored=1 WHERE id=?", (e,))

    result = await app.rss_poll()
    assert len(result["grabbed"]) == 1
    assert app.db.get_episode(e)["status"] == "grabbed"


async def test_rss_poll_anime_release_ignored_for_standard_series(configured, fakes, monkeypatch):
    """A standard (non-anime) series must NOT match an absolute-numbered release."""
    app = configured
    monkeypatch.setattr(core, "get_client", lambda cfg: fakes["DownloadClient"]())
    rels = [fakes["make_release"]("[SubsPlease] Show - 01 (1080p)", guid="a1")]
    monkeypatch.setattr(app, "prowlarr", lambda: fakes["Prowlarr"](releases=rels))

    sid = app.db.upsert_series(provider="tmdb", provider_id="1", title="Show", monitored=1)
    e = app.db.upsert_episode(sid, 1, 1)
    app.db.execute("UPDATE episodes SET monitored=1 WHERE id=?", (e,))

    result = await app.rss_poll()
    assert result["grabbed"] == []  # absolute matching not applied to standard TV


async def test_add_movie(app, fakes, monkeypatch):
    info = MovieInfo(provider="tmdb", provider_id="9", title="Dune", year=2021)
    monkeypatch.setattr(app, "provider", lambda *_a, **_k: fakes["Provider"](movie_info=info))
    result = await app.add_movie("9")
    assert result["title"] == "Dune"
    assert result["folder_name"] == "Dune (2021)"
    assert result["movie_status"] == "missing"


# --------------------------------------------------------------------------- #
# search + grab
# --------------------------------------------------------------------------- #
async def test_search_releases_applies_quality(app, fakes, monkeypatch):
    rels = [
        fakes["make_release"]("Show.S01E01.CAM", seeders=999),
        fakes["make_release"]("Show.S01E01.1080p.WEB", seeders=50),
        fakes["make_release"]("Show.S01E01.720p", seeders=40),
    ]
    monkeypatch.setattr(app, "prowlarr", lambda: fakes["Prowlarr"](releases=rels))
    out = await app.search_releases("show", apply_quality=True)
    titles = [r["title"] for r in out]
    assert "Show.S01E01.CAM" not in titles  # ignored term filtered
    assert titles[0] == "Show.S01E01.1080p.WEB"  # best ranked first
    assert out[0]["resolution"] == "1080p"


async def test_grab_records_download_and_marks_episode(configured, fakes, monkeypatch):
    app = configured
    monkeypatch.setattr(core, "get_client", lambda cfg: fakes["DownloadClient"]())
    sid = app.db.upsert_series(provider="tmdb", provider_id="1", title="Show")
    e = app.db.upsert_episode(sid, 1, 1)

    res = await app.grab(
        "magnet:?xt=urn:btih:" + "a" * 40, title="Show.S01E01.1080p",
        series_id=sid, episode_id=e, guid="g1",
    )
    assert res["torrent_hash"]
    dl = app.db.get_download(res["download_id"])
    assert dl["series_id"] == sid and dl["episode_id"] == e
    assert app.db.get_episode(e)["status"] == "grabbed"
    assert app.db.seen_guid("g1")


async def test_grab_pack_marks_all_covered_episodes(configured, fakes, monkeypatch):
    app = configured
    monkeypatch.setattr(core, "get_client", lambda cfg: fakes["DownloadClient"]())
    sid = app.db.upsert_series(provider="tmdb", provider_id="1", title="Show")
    e1 = app.db.upsert_episode(sid, 2, 1)
    e2 = app.db.upsert_episode(sid, 2, 2)
    e3 = app.db.upsert_episode(sid, 1, 1)  # different season, must stay missing

    res = await app.grab(
        "magnet:?xt=urn:btih:" + "a" * 40, title="Show.S02.1080p.WEB", series_id=sid
    )
    assert res["covered_episodes"] == 2
    assert app.db.get_episode(e1)["status"] == "grabbed"
    assert app.db.get_episode(e2)["status"] == "grabbed"
    assert app.db.get_episode(e3)["status"] == "missing"  # season 1 untouched


async def test_grab_anime_batch_marks_all(configured, fakes, monkeypatch):
    app = configured
    monkeypatch.setattr(core, "get_client", lambda cfg: fakes["DownloadClient"]())
    sid = app.db.upsert_series(
        provider="jikan", provider_id="1", title="Frieren", absolute_numbering=1
    )
    eps = [app.db.upsert_episode(sid, 1, n) for n in range(1, 5)]
    res = await app.grab(
        "magnet:?xt=urn:btih:" + "a" * 40, title="[Group] Frieren (01-28) [Batch]", series_id=sid
    )
    assert res["covered_episodes"] == 4
    assert all(app.db.get_episode(e)["status"] == "grabbed" for e in eps)


async def test_rss_poll_no_double_grab_after_pack(configured, fakes, monkeypatch):
    app = configured
    monkeypatch.setattr(core, "get_client", lambda cfg: fakes["DownloadClient"]())
    # A season pack plus a single-episode release for the same season.
    rels = [
        fakes["make_release"]("Show.S01.Complete.1080p.WEB", guid="pack", seeders=100),
        fakes["make_release"]("Show.S01E02.1080p.WEB", guid="single", seeders=100),
    ]
    monkeypatch.setattr(app, "prowlarr", lambda: fakes["Prowlarr"](releases=rels))
    sid = app.db.upsert_series(provider="tmdb", provider_id="1", title="Show", monitored=1)
    for n in (1, 2, 3):
        e = app.db.upsert_episode(sid, 1, n)
        app.db.execute("UPDATE episodes SET monitored=1 WHERE id=?", (e,))

    result = await app.rss_poll()
    # Exactly one grab (the pack) — the single for S01E02 must be suppressed.
    assert len(result["grabbed"]) == 1
    assert "S01" in result["grabbed"][0]["release"]
    assert not app.db.seen_guid("single")


async def test_grab_season_picks_pack(configured, fakes, monkeypatch):
    app = configured
    monkeypatch.setattr(core, "get_client", lambda cfg: fakes["DownloadClient"]())
    rels = [
        fakes["make_release"]("Show.S02E05.1080p", guid="single", seeders=50),
        fakes["make_release"]("Show.S02.1080p.WEB-DL.Complete", guid="pack", seeders=80),
    ]
    monkeypatch.setattr(app, "prowlarr", lambda: fakes["Prowlarr"](releases=rels))
    sid = app.db.upsert_series(provider="tmdb", provider_id="1", title="Show")
    for n in (1, 2, 3):
        app.db.upsert_episode(sid, 2, n)

    out = await app.grab_season(sid, 2)
    assert "S02" in out["picked"] and "Complete" in out["picked"]
    assert out["covered_episodes"] == 3


async def test_grab_season_no_pack_found(configured, fakes, monkeypatch):
    app = configured
    rels = [fakes["make_release"]("Show.S02E05.1080p", guid="s")]
    monkeypatch.setattr(app, "prowlarr", lambda: fakes["Prowlarr"](releases=rels))
    sid = app.db.upsert_series(provider="tmdb", provider_id="1", title="Show")
    app.db.upsert_episode(sid, 2, 5)
    out = await app.grab_season(sid, 2)
    assert "error" in out


async def test_grab_movie_marks_movie(configured, fakes, monkeypatch):
    app = configured
    monkeypatch.setattr(core, "get_client", lambda cfg: fakes["DownloadClient"]())
    mid = app.db.upsert_movie(provider="tmdb", provider_id="9", title="Dune")
    res = await app.grab("magnet:?xt=urn:btih:" + "a" * 40, title="Dune.2021", movie_id=mid)
    assert app.db.get_movie(mid)["movie_status"] == "grabbed"
    assert app.db.get_download(res["download_id"])["movie_id"] == mid


async def test_client_config_error_when_ambiguous(app):
    def setup(c):
        c.download_clients["a"] = DownloadClientConfig(url="http://a")
        c.download_clients["b"] = DownloadClientConfig(url="http://b")
    app.store.mutate(setup)
    with pytest.raises(ValueError):
        app._client_config(None)  # two clients, no default -> ambiguous


# --------------------------------------------------------------------------- #
# refresh_downloads -> import -> plex
# --------------------------------------------------------------------------- #
@pytest.fixture
def import_ready(configured, tmp_path):
    app = configured
    dl = tmp_path / "dl"
    lib = tmp_path / "lib"
    dl.mkdir()
    lib.mkdir()
    (dl / "Show.S01E01.mkv").write_text("x" * 1000)

    def setup(c):
        c.importer.min_video_mb = 0
        c.path_mappings = [
            PathMapping(group="dl", context="qbittorrent", path="/downloads"),
            PathMapping(group="dl", context="local", path=str(dl)),
            PathMapping(group="lib", context="local", path=str(lib)),
            PathMapping(group="lib", context="plex", path="/data/tv"),
        ]
        c.root_folders = [RootFolder(name="tv", media_type="tv", context="local", path=str(lib))]

    app.store.mutate(setup)
    return app, dl, lib


async def test_refresh_completes_imports_and_scans(import_ready, fakes, monkeypatch):
    app, dl, lib = import_ready
    fake_plex = fakes["Plex"]()
    monkeypatch.setattr(
        core, "get_client",
        lambda cfg: fakes["DownloadClient"](complete=True, content_path="/downloads/Show.S01E01.mkv"),
    )
    monkeypatch.setattr(app, "plex", lambda: fake_plex)

    sid = app.db.upsert_series(
        provider="tmdb", provider_id="1", title="Show", year=2020,
        root_folder="tv", folder_name="Show (2020)",
    )
    e = app.db.upsert_episode(sid, 1, 1, title="Pilot")
    did = app.db.add_download(
        series_id=sid, episode_id=e, title="Show.S01E01", torrent_hash="h", client="qbit"
    )

    updates = await app.refresh_downloads()
    assert updates[0]["state"] == "completed"
    assert updates[0]["notified"] is True
    assert app.db.get_download(did)["status"] == "imported"
    assert app.db.get_episode(e)["status"] == "downloaded"
    # Plex scanned the season dir translated into its own namespace.
    section, path = fake_plex.scans[0]
    assert section == "TV Shows"
    assert path.startswith("/data/tv/Show (2020)/Season 01")


async def test_refresh_downloading_not_completed(configured, fakes, monkeypatch):
    app = configured
    monkeypatch.setattr(core, "get_client", lambda cfg: fakes["DownloadClient"](complete=False))
    did = app.db.add_download(title="X", torrent_hash="h", client="qbit")
    updates = await app.refresh_downloads()
    assert updates[0]["state"] == "downloading"
    assert app.db.get_download(did)["status"] == "downloading"


# --------------------------------------------------------------------------- #
# rss_poll
# --------------------------------------------------------------------------- #
async def test_rss_poll_auto_grabs_missing_episode(configured, fakes, monkeypatch):
    app = configured
    monkeypatch.setattr(core, "get_client", lambda cfg: fakes["DownloadClient"]())
    rels = [fakes["make_release"]("Show.S01E01.1080p.WEB", guid="rel1", seeders=50)]
    monkeypatch.setattr(app, "prowlarr", lambda: fakes["Prowlarr"](releases=rels))

    sid = app.db.upsert_series(provider="tmdb", provider_id="1", title="Show", monitored=1)
    e = app.db.upsert_episode(sid, 1, 1)
    app.db.execute("UPDATE episodes SET monitored=1 WHERE id=?", (e,))

    result = await app.rss_poll()
    assert len(result["grabbed"]) == 1
    assert app.db.get_episode(e)["status"] == "grabbed"
    assert app.db.seen_guid("rel1")


async def test_rss_poll_skips_seen_guid(configured, fakes, monkeypatch):
    app = configured
    monkeypatch.setattr(core, "get_client", lambda cfg: fakes["DownloadClient"]())
    rels = [fakes["make_release"]("Show.S01E01.1080p", guid="rel1")]
    monkeypatch.setattr(app, "prowlarr", lambda: fakes["Prowlarr"](releases=rels))
    app.db.record_guid("rel1")

    sid = app.db.upsert_series(provider="tmdb", provider_id="1", title="Show", monitored=1)
    e = app.db.upsert_episode(sid, 1, 1)
    app.db.execute("UPDATE episodes SET monitored=1 WHERE id=?", (e,))

    result = await app.rss_poll()
    assert result["grabbed"] == []


async def test_rss_poll_candidates_when_autograb_off(configured, fakes, monkeypatch):
    app = configured
    app.store.mutate(lambda c: setattr(c.rss, "auto_grab", False))
    rels = [fakes["make_release"]("Show.S01E01.1080p", guid="rel1")]
    monkeypatch.setattr(app, "prowlarr", lambda: fakes["Prowlarr"](releases=rels))

    sid = app.db.upsert_series(provider="tmdb", provider_id="1", title="Show", monitored=1)
    e = app.db.upsert_episode(sid, 1, 1)
    app.db.execute("UPDATE episodes SET monitored=1 WHERE id=?", (e,))

    result = await app.rss_poll()
    assert result["grabbed"] == []
    assert any("S01E01" in c.get("episode", "") for c in result["candidates"])
    assert app.db.get_episode(e)["status"] == "missing"  # not grabbed


async def test_rss_poll_auto_grabs_missing_movie(configured, fakes, monkeypatch):
    app = configured
    monkeypatch.setattr(core, "get_client", lambda cfg: fakes["DownloadClient"]())
    rels = [fakes["make_release"]("Dune.2021.1080p.WEB", guid="m1", seeders=80)]
    monkeypatch.setattr(app, "prowlarr", lambda: fakes["Prowlarr"](releases=rels))

    mid = app.db.upsert_movie(provider="tmdb", provider_id="9", title="Dune", year=2021, monitored=1)
    result = await app.rss_poll()
    assert result["checked_movies"] == 1
    assert any(g.get("movie") == "Dune" for g in result["grabbed"])
    assert app.db.get_movie(mid)["movie_status"] == "grabbed"
