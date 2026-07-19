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
        provider="tmdb", provider_id="1", title="Meridian", year=2022,
        seasons=[1, 2],
        episodes=[
            EpisodeInfo(season=1, episode=1, title="A"),
            EpisodeInfo(season=1, episode=2, title="B"),
            EpisodeInfo(season=2, episode=1, title="C"),
        ],
    )
    monkeypatch.setattr(app, "provider", lambda *_a, **_k: fakes["Provider"](series_info=info))

    result = await app.add_series("1", seasons=[2])
    assert result["title"] == "Meridian"
    assert result["episode_count"] == 3
    eps = app.db.list_episodes(result["id"])
    monitored = {(e["season"], e["episode"]): e["monitored"] for e in eps}
    # Only season 2 monitored.
    assert monitored[(2, 1)] == 1
    assert monitored[(1, 1)] == 0 and monitored[(1, 2)] == 0


async def test_add_series_anime_sets_absolute_flag(app, fakes, monkeypatch):
    info = SeriesInfo(
        provider="jikan", provider_id="52991", title="Aethering", year=2023,
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
    rels = [fakes["make_release"]("[FanSubA] Aethering - 01 (1080p) [ABCD].mkv", guid="a1", seeders=80)]
    monkeypatch.setattr(app, "prowlarr", lambda: fakes["Prowlarr"](releases=rels))

    sid = app.db.upsert_series(
        provider="jikan", provider_id="52991", title="Aethering", monitored=1,
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
    rels = [fakes["make_release"]("[FanSubA] Show - 01 (1080p)", guid="a1")]
    monkeypatch.setattr(app, "prowlarr", lambda: fakes["Prowlarr"](releases=rels))

    sid = app.db.upsert_series(provider="tmdb", provider_id="1", title="Show", monitored=1)
    e = app.db.upsert_episode(sid, 1, 1)
    app.db.execute("UPDATE episodes SET monitored=1 WHERE id=?", (e,))

    result = await app.rss_poll()
    assert result["grabbed"] == []  # absolute matching not applied to standard TV


async def test_add_series_specials_unmonitored_by_default(app, fakes, monkeypatch):
    info = SeriesInfo(
        provider="tmdb", provider_id="1", title="Show", seasons=[0, 1],
        episodes=[EpisodeInfo(season=0, episode=1, title="Special"),
                  EpisodeInfo(season=1, episode=1, title="Pilot")],
    )
    monkeypatch.setattr(app, "provider", lambda *_a, **_k: fakes["Provider"](series_info=info))
    r = await app.add_series("1")  # monitor all regular seasons
    mon = {(e["season"], e["episode"]): e["monitored"] for e in app.db.list_episodes(r["id"])}
    assert mon[(1, 1)] == 1 and mon[(0, 1)] == 0  # special off by default


async def test_add_series_specials_opt_in(app, fakes, monkeypatch):
    info = SeriesInfo(
        provider="tmdb", provider_id="1", title="Show", seasons=[0, 1],
        episodes=[EpisodeInfo(season=0, episode=1), EpisodeInfo(season=1, episode=1)],
    )
    monkeypatch.setattr(app, "provider", lambda *_a, **_k: fakes["Provider"](series_info=info))
    r = await app.add_series("1", seasons=[0, 1])  # explicitly include specials
    mon = {(e["season"], e["episode"]): e["monitored"] for e in app.db.list_episodes(r["id"])}
    assert mon[(0, 1)] == 1 and mon[(1, 1)] == 1


async def test_download_queue_reports_progress(configured, fakes, monkeypatch):
    app = configured
    monkeypatch.setattr(
        core, "get_client", lambda cfg: fakes["DownloadClient"](complete=False)
    )
    app.db.add_download(title="Show.S01E01", torrent_hash="h", client="qbit", status="downloading")
    app.db.add_download(title="old", torrent_hash="h2", client="qbit", status="imported")  # excluded
    q = await app.download_queue()
    assert len(q) == 1
    assert q[0]["title"] == "Show.S01E01" and q[0]["progress_pct"] == 30.0


async def test_readd_series_preserves_monitored_flags(app, fakes, monkeypatch):
    info = SeriesInfo(
        provider="tmdb", provider_id="1", title="Show", seasons=[1],
        episodes=[EpisodeInfo(season=1, episode=1), EpisodeInfo(season=1, episode=2)],
    )
    monkeypatch.setattr(app, "provider", lambda *_a, **_k: fakes["Provider"](series_info=info))
    r = await app.add_series("1")
    sid = r["id"]
    # user unmonitors episode 1
    e1 = app.db.list_episodes(sid)[0]
    app.db.execute("UPDATE episodes SET monitored=0 WHERE id=?", (e1["id"],))

    # re-add (metadata refresh) must NOT re-monitor episode 1
    await app.add_series("1")
    assert app.db.get_episode(e1["id"])["monitored"] == 0


async def test_readd_series_applies_rule_to_new_episodes(app, fakes, monkeypatch):
    info1 = SeriesInfo(provider="tmdb", provider_id="1", title="Show", seasons=[1],
                       episodes=[EpisodeInfo(season=1, episode=1)])
    monkeypatch.setattr(app, "provider", lambda *_a, **_k: fakes["Provider"](series_info=info1))
    r = await app.add_series("1", seasons=[1])
    sid = r["id"]
    # season 2 airs later; re-add with a new episode, monitoring only season 1
    info2 = SeriesInfo(provider="tmdb", provider_id="1", title="Show", seasons=[1, 2],
                       episodes=[EpisodeInfo(season=1, episode=1), EpisodeInfo(season=2, episode=1)])
    monkeypatch.setattr(app, "provider", lambda *_a, **_k: fakes["Provider"](series_info=info2))
    await app.add_series("1", seasons=[1])
    mon = {(e["season"], e["episode"]): e["monitored"] for e in app.db.list_episodes(sid)}
    assert mon[(1, 1)] == 1 and mon[(2, 1)] == 0  # new S2 ep not monitored (not in seasons)


async def test_readd_series_preserves_monitored_and_root_folder(app, fakes, monkeypatch):
    info = SeriesInfo(provider="tmdb", provider_id="1", title="Show", seasons=[1],
                      episodes=[EpisodeInfo(season=1, episode=1)])
    monkeypatch.setattr(app, "provider", lambda *_a, **_k: fakes["Provider"](series_info=info))
    r = await app.add_series("1", monitored=False, root_folder="tv-4k")
    sid = r["id"]
    assert app.db.get_series(sid)["monitored"] == 0
    # metadata refresh (re-add with defaults) must NOT reset these
    await app.add_series("1")
    row = app.db.get_series(sid)
    assert row["monitored"] == 0 and row["root_folder"] == "tv-4k"


def test_reset_grab_to_missing(app):
    sid = app.db.upsert_series(provider="tmdb", provider_id="1", title="Show")
    e1 = app.db.upsert_episode(sid, 1, 1)
    e2 = app.db.upsert_episode(sid, 1, 2)
    app.db.set_episode_status(e1, "grabbed")
    app.db.set_episode_status(e2, "downloaded")  # already imported — must NOT reset
    download = {"series_id": sid, "episode_id": e1, "movie_id": None, "title": "Show.S01E01"}
    app.reset_grab_to_missing(download)
    assert app.db.get_episode(e1)["status"] == "missing"
    assert app.db.get_episode(e2)["status"] == "downloaded"


# --------------------------------------------------------------------------- #
# recovery tools (G2)
# --------------------------------------------------------------------------- #
def test_reset_episode_and_movie(app):
    sid = app.db.upsert_series(provider="tmdb", provider_id="1", title="Show")
    e = app.db.upsert_episode(sid, 1, 1)
    app.db.set_episode_status(e, "downloaded", "/x.mkv")
    res = app.reset_episode(e)
    assert res["was"] == "downloaded" and app.db.get_episode(e)["status"] == "missing"
    mid = app.db.upsert_movie(provider="tmdb", provider_id="9", title="Nebula")
    app.db.set_movie_status(mid, "grabbed")
    assert app.reset_movie(mid)["status"] == "missing"
    assert app.db.get_movie(mid)["movie_status"] == "missing"
    assert "error" in app.reset_episode(999)


def test_mark_download_failed_only_resets_grabbed(app):
    sid = app.db.upsert_series(provider="tmdb", provider_id="1", title="Show")
    e = app.db.upsert_episode(sid, 1, 1)
    app.db.set_episode_status(e, "downloaded")  # already imported — must stay
    did = app.db.add_download(series_id=sid, episode_id=e, title="Show.S01E01",
                              torrent_hash="h", client="qbit")
    res = app.mark_download_failed(did)
    assert res["status"] == "failed" and res["reset_to_missing"] == 0
    assert app.db.get_download(did)["status"] == "failed"
    assert app.db.get_episode(e)["status"] == "downloaded"  # untouched


def test_retry_download_force_resets(app):
    sid = app.db.upsert_series(provider="tmdb", provider_id="1", title="Show")
    e = app.db.upsert_episode(sid, 1, 1)
    app.db.set_episode_status(e, "downloaded")  # force-reset even downloaded
    did = app.db.add_download(series_id=sid, episode_id=e, title="Show.S01E01",
                              torrent_hash="h", client="qbit")
    res = app.retry_download(did)
    assert res["reset_to_missing"] == 1
    assert app.db.get_episode(e)["status"] == "missing"
    assert app.db.get_download(did)["status"] == "failed"


def test_forget_and_clear_grab_history(app):
    app.db.record_guid("g1")
    app.db.record_guid("g2")
    assert app.db.forget_guid("g1") is True
    assert not app.db.seen_guid("g1") and app.db.seen_guid("g2")
    assert app.db.forget_guid("missing") is False
    assert app.db.clear_grab_history() == 1  # only g2 left
    assert not app.db.seen_guid("g2")


async def test_rss_picks_second_best_when_top_seen(configured, fakes, monkeypatch):
    app = configured
    monkeypatch.setattr(core, "get_client", lambda cfg: fakes["DownloadClient"]())
    # Top pick (more seeders) already seen; a second matching release is fresh.
    rels = [
        fakes["make_release"]("Show.S01E01.1080p.WEB", guid="seen", seeders=500),
        fakes["make_release"]("Show.S01E01.1080p.WEBRip", guid="fresh", seeders=50),
    ]
    monkeypatch.setattr(app, "prowlarr", lambda: fakes["Prowlarr"](releases=rels))
    app.db.record_guid("seen")
    sid = app.db.upsert_series(provider="tmdb", provider_id="1", title="Show", monitored=1)
    e = app.db.upsert_episode(sid, 1, 1)
    app.db.execute("UPDATE episodes SET monitored=1 WHERE id=?", (e,))
    result = await app.rss_poll()
    assert len(result["grabbed"]) == 1
    assert result["grabbed"][0]["release"] == "Show.S01E01.1080p.WEBRip"  # the unseen one


# --------------------------------------------------------------------------- #
# refresh_series (G1)
# --------------------------------------------------------------------------- #
async def test_refresh_series_adds_new_episodes(app, fakes, monkeypatch):
    info1 = SeriesInfo(provider="tmdb", provider_id="1", title="Show", status="Continuing",
                       seasons=[1], episodes=[EpisodeInfo(season=1, episode=1)])
    monkeypatch.setattr(app, "provider", lambda *_a, **_k: fakes["Provider"](series_info=info1))
    r = await app.add_series("1")
    sid = r["id"]
    # a new episode airs later
    info2 = SeriesInfo(provider="tmdb", provider_id="1", title="Show", status="Continuing",
                       seasons=[1], episodes=[EpisodeInfo(season=1, episode=1),
                                              EpisodeInfo(season=1, episode=2)])
    monkeypatch.setattr(app, "provider", lambda *_a, **_k: fakes["Provider"](series_info=info2))
    res = await app.refresh_series(sid)
    assert res["new_episodes"] == 1 and res["added"] == ["S01E02"]
    mon = {(e["season"], e["episode"]): e["monitored"] for e in app.db.list_episodes(sid)}
    assert mon[(1, 2)] == 1  # new regular ep monitored (series monitored)
    assert app.db.get_series(sid)["last_refresh"] is not None


async def test_refresh_series_preserves_existing_and_specials(app, fakes, monkeypatch):
    info1 = SeriesInfo(provider="tmdb", provider_id="1", title="Show", status="Continuing",
                       seasons=[1], episodes=[EpisodeInfo(season=1, episode=1)])
    monkeypatch.setattr(app, "provider", lambda *_a, **_k: fakes["Provider"](series_info=info1))
    sid = (await app.add_series("1"))["id"]
    # user grabs/imports ep1 and unmonitors it
    e1 = app.db.list_episodes(sid)[0]
    app.db.set_episode_status(e1["id"], "downloaded", "/x.mkv")
    app.db.execute("UPDATE episodes SET monitored=0 WHERE id=?", (e1["id"],))
    # refresh brings a new special + a new regular ep
    info2 = SeriesInfo(provider="tmdb", provider_id="1", title="Show", status="Continuing",
                       seasons=[0, 1], episodes=[EpisodeInfo(season=1, episode=1),
                                                 EpisodeInfo(season=0, episode=1),
                                                 EpisodeInfo(season=1, episode=2)])
    monkeypatch.setattr(app, "provider", lambda *_a, **_k: fakes["Provider"](series_info=info2))
    await app.refresh_series(sid)
    m = {(e["season"], e["episode"]): e for e in app.db.list_episodes(sid)}
    assert m[(1, 1)]["status"] == "downloaded" and m[(1, 1)]["monitored"] == 0  # untouched
    assert m[(0, 1)]["monitored"] == 0  # special unmonitored
    assert m[(1, 2)]["monitored"] == 1


async def test_refresh_series_unmonitored_series_new_ep_unmonitored(app, fakes, monkeypatch):
    info1 = SeriesInfo(provider="tmdb", provider_id="1", title="Show", status="Continuing",
                       seasons=[1], episodes=[EpisodeInfo(season=1, episode=1)])
    monkeypatch.setattr(app, "provider", lambda *_a, **_k: fakes["Provider"](series_info=info1))
    sid = (await app.add_series("1", monitored=False))["id"]
    info2 = SeriesInfo(provider="tmdb", provider_id="1", title="Show", status="Continuing",
                       seasons=[1], episodes=[EpisodeInfo(season=1, episode=1),
                                              EpisodeInfo(season=1, episode=2)])
    monkeypatch.setattr(app, "provider", lambda *_a, **_k: fakes["Provider"](series_info=info2))
    await app.refresh_series(sid)
    mon = {(e["season"], e["episode"]): e["monitored"] for e in app.db.list_episodes(sid)}
    assert mon[(1, 2)] == 0  # series unmonitored -> new ep unmonitored


async def test_refresh_stale_skips_ended_and_recent(app, fakes, monkeypatch):
    info = SeriesInfo(provider="tmdb", provider_id="1", title="Ended Show", status="Ended",
                      seasons=[1], episodes=[EpisodeInfo(season=1, episode=1)])
    monkeypatch.setattr(app, "provider", lambda *_a, **_k: fakes["Provider"](series_info=info))
    # An ended, monitored series is not refreshed.
    app.db.upsert_series(provider="tmdb", provider_id="1", title="Ended Show",
                         monitored=1, status="Ended")
    # A recently-refreshed airing series is skipped too.
    import time as _t
    sid2 = app.db.upsert_series(provider="tmdb", provider_id="2", title="Fresh",
                                monitored=1, status="Continuing", last_refresh=_t.time())
    out = await app.refresh_stale_series()
    assert out == []


async def test_refresh_stale_refreshes_airing(app, fakes, monkeypatch):
    info = SeriesInfo(provider="tmdb", provider_id="1", title="Airing", status="Continuing",
                      seasons=[1], episodes=[EpisodeInfo(season=1, episode=1),
                                             EpisodeInfo(season=1, episode=2)])
    monkeypatch.setattr(app, "provider", lambda *_a, **_k: fakes["Provider"](series_info=info))
    sid = app.db.upsert_series(provider="tmdb", provider_id="1", title="Airing",
                               monitored=1, status="Continuing")
    app.db.upsert_episode(sid, 1, 1)  # only ep1 known so far
    out = await app.refresh_stale_series()
    assert len(out) == 1 and out[0]["new_episodes"] == 1
    assert {(e["season"], e["episode"]) for e in app.db.list_episodes(sid)} == {(1, 1), (1, 2)}


async def test_refresh_stale_disabled_when_interval_zero(app, fakes, monkeypatch):
    app.store.mutate(lambda c: setattr(c.rss, "refresh_interval_hours", 0))
    app.db.upsert_series(provider="tmdb", provider_id="1", title="Airing",
                         monitored=1, status="Continuing")
    assert await app.refresh_stale_series() == []


async def test_add_movie(app, fakes, monkeypatch):
    info = MovieInfo(provider="tmdb", provider_id="9", title="Nebula", year=2021)
    monkeypatch.setattr(app, "provider", lambda *_a, **_k: fakes["Provider"](movie_info=info))
    result = await app.add_movie("9")
    assert result["title"] == "Nebula"
    assert result["folder_name"] == "Nebula (2021)"
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
        provider="jikan", provider_id="1", title="Aethering", absolute_numbering=1
    )
    eps = [app.db.upsert_episode(sid, 1, n) for n in range(1, 5)]
    res = await app.grab(
        "magnet:?xt=urn:btih:" + "a" * 40, title="[Group] Aethering (01-28) [Batch]", series_id=sid
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
    mid = app.db.upsert_movie(provider="tmdb", provider_id="9", title="Nebula")
    res = await app.grab("magnet:?xt=urn:btih:" + "a" * 40, title="Nebula.2021", movie_id=mid)
    assert app.db.get_movie(mid)["movie_status"] == "grabbed"
    assert app.db.get_download(res["download_id"])["movie_id"] == mid


async def test_grab_refused_when_insufficient_space(configured, fakes, monkeypatch, tmp_path):
    app = configured
    monkeypatch.setattr(core, "get_client", lambda cfg: fakes["DownloadClient"]())
    app.store.mutate(lambda c: setattr(c.importer, "min_free_space_mb", 10 ** 12))
    sid = app.db.upsert_series(provider="tmdb", provider_id="1", title="Show")
    e = app.db.upsert_episode(sid, 1, 1)
    with pytest.raises(ValueError, match="free space"):
        await app.grab(
            "magnet:?xt=urn:btih:" + "a" * 40, title="Show.S01E01",
            series_id=sid, episode_id=e, size=2_000_000_000,
            save_path=str(tmp_path),
        )
    # nothing recorded / episode untouched
    assert app.db.list_downloads() == []
    assert app.db.get_episode(e)["status"] == "missing"


async def test_grab_space_check_skipped_when_size_unknown(configured, fakes, monkeypatch, tmp_path):
    app = configured
    monkeypatch.setattr(core, "get_client", lambda cfg: fakes["DownloadClient"]())
    app.store.mutate(lambda c: setattr(c.importer, "min_free_space_mb", 10 ** 12))
    sid = app.db.upsert_series(provider="tmdb", provider_id="1", title="Show")
    e = app.db.upsert_episode(sid, 1, 1)
    # size=None -> can't check, so the grab proceeds.
    res = await app.grab("magnet:?xt=urn:btih:" + "a" * 40, title="Show.S01E01",
                         series_id=sid, episode_id=e, save_path=str(tmp_path))
    assert res["download_id"]


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
    rels = [fakes["make_release"]("Nebula.2021.1080p.WEB", guid="m1", seeders=80)]
    monkeypatch.setattr(app, "prowlarr", lambda: fakes["Prowlarr"](releases=rels))

    mid = app.db.upsert_movie(provider="tmdb", provider_id="9", title="Nebula", year=2021, monitored=1)
    result = await app.rss_poll()
    assert result["checked_movies"] == 1
    assert any(g.get("movie") == "Nebula" for g in result["grabbed"])
    assert app.db.get_movie(mid)["movie_status"] == "grabbed"
