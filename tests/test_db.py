import sqlite3

from llmarr.db import Database


def test_series_upsert_no_duplicate(db):
    a = db.upsert_series(provider="tmdb", provider_id="1", title="Show", year=2020)
    b = db.upsert_series(provider="tmdb", provider_id="1", title="Show Renamed", year=2020)
    assert a == b
    assert db.get_series(a)["title"] == "Show Renamed"
    assert len(db.list_series()) == 1


def test_episode_status_transitions(db):
    sid = db.upsert_series(provider="tmdb", provider_id="1", title="Show")
    e = db.upsert_episode(sid, 1, 1, title="Pilot")
    assert db.list_episodes(sid, status="missing")
    db.set_episode_status(e, "grabbed")
    assert db.get_episode(e)["status"] == "grabbed"
    db.set_episode_status(e, "downloaded", "/lib/Show/S01E01.mkv")
    row = db.get_episode(e)
    assert row["status"] == "downloaded" and row["file_path"].endswith("S01E01.mkv")


def test_episode_upsert_preserves_status(db):
    sid = db.upsert_series(provider="tmdb", provider_id="1", title="Show")
    e = db.upsert_episode(sid, 1, 1, title="Pilot")
    db.set_episode_status(e, "downloaded", "/x.mkv")
    # Metadata refresh must not reset status/file_path.
    db.upsert_episode(sid, 1, 1, title="Pilot (Updated)")
    row = db.get_episode(e)
    assert row["status"] == "downloaded"
    assert row["file_path"] == "/x.mkv"
    assert row["title"] == "Pilot (Updated)"


def test_set_monitored_series_and_season(db):
    sid = db.upsert_series(provider="tmdb", provider_id="1", title="Show")
    db.upsert_episode(sid, 1, 1)
    db.upsert_episode(sid, 2, 1)
    db.set_monitored(sid, False)
    assert all(e["monitored"] == 0 for e in db.list_episodes(sid))
    db.set_monitored(sid, True, season=2)
    mon = {(e["season"]): e["monitored"] for e in db.list_episodes(sid)}
    assert mon[1] == 0 and mon[2] == 1


def test_downloads_and_status(db):
    sid = db.upsert_series(provider="tmdb", provider_id="1", title="Show")
    did = db.add_download(series_id=sid, title="Show.S01E01", torrent_hash="h", client="qbit")
    assert db.get_download(did)["status"] == "grabbed"
    db.set_download_status(did, "completed", save_path="/downloads/x")
    row = db.get_download(did)
    assert row["status"] == "completed" and row["save_path"] == "/downloads/x"
    assert db.list_downloads(status="completed")[0]["id"] == did


def test_grab_history_dedup(db):
    assert not db.seen_guid("g1")
    db.record_guid("g1")
    db.record_guid("g1")  # idempotent
    assert db.seen_guid("g1")


def test_movie_lifecycle(db):
    mid = db.upsert_movie(provider="tmdb", provider_id="9", title="Nebula", year=2021)
    assert db.get_movie(mid)["movie_status"] == "missing"
    db.set_movie_status(mid, "grabbed")
    assert db.get_movie(mid)["movie_status"] == "grabbed"
    db.set_movie_status(mid, "downloaded", "/lib/Nebula (2021)/Nebula (2021).mkv")
    assert db.get_movie(mid)["file_path"].endswith("Nebula (2021).mkv")
    db.set_movie_monitored(mid, False)
    assert db.get_movie(mid)["monitored"] == 0
    db.delete_movie(mid)
    assert db.get_movie(mid) is None


def test_movie_upsert_preserves_status(db):
    mid = db.upsert_movie(provider="tmdb", provider_id="9", title="Nebula", year=2021)
    db.set_movie_status(mid, "downloaded", "/x.mkv")
    db.upsert_movie(provider="tmdb", provider_id="9", title="Nebula: Part One", year=2021)
    assert db.get_movie(mid)["movie_status"] == "downloaded"
    assert db.get_movie(mid)["title"] == "Nebula: Part One"


def test_cascade_delete_series_removes_episodes(db):
    sid = db.upsert_series(provider="tmdb", provider_id="1", title="Show")
    db.upsert_episode(sid, 1, 1)
    db.delete_series(sid)
    assert db.list_episodes(sid) == []


def test_migration_adds_movie_id(tmp_path):
    # Simulate an old DB created before the movies feature.
    p = tmp_path / "old.db"
    conn = sqlite3.connect(p)
    conn.executescript(
        "CREATE TABLE downloads (id INTEGER PRIMARY KEY, series_id INTEGER, "
        "episode_id INTEGER, title TEXT, status TEXT, grabbed_at REAL);"
    )
    conn.commit()
    conn.close()
    # Opening via Database should add the movie_id column without error.
    d = Database(p)
    cols = {r["name"] for r in d.query("PRAGMA table_info(downloads)")}
    assert "movie_id" in cols


def test_migration_adds_absolute_numbering(tmp_path):
    import sqlite3

    p = tmp_path / "old.db"
    conn = sqlite3.connect(p)
    conn.executescript(
        "CREATE TABLE series (id INTEGER PRIMARY KEY, provider TEXT, provider_id TEXT, "
        "title TEXT, added_at REAL, UNIQUE(provider, provider_id));"
    )
    conn.commit()
    conn.close()
    d = Database(p)
    cols = {r["name"] for r in d.query("PRAGMA table_info(series)")}
    assert "absolute_numbering" in cols


def test_migration_adds_series_refresh_and_plex_columns(tmp_path):
    import sqlite3

    p = tmp_path / "old.db"
    conn = sqlite3.connect(p)
    conn.executescript(
        "CREATE TABLE series (id INTEGER PRIMARY KEY, provider TEXT, provider_id TEXT, "
        "title TEXT, added_at REAL, UNIQUE(provider, provider_id));"
    )
    conn.commit()
    conn.close()
    d = Database(p)
    cols = {r["name"] for r in d.query("PRAGMA table_info(series)")}
    assert {"last_refresh", "plex_rating_key", "plex_section"} <= cols


def test_migration_adds_quality_and_upgrade_columns(tmp_path):
    import sqlite3

    p = tmp_path / "old.db"
    conn = sqlite3.connect(p)
    conn.executescript(
        "CREATE TABLE downloads (id INTEGER PRIMARY KEY, title TEXT, status TEXT, grabbed_at REAL);"
        "CREATE TABLE episodes (id INTEGER PRIMARY KEY, series_id INTEGER, season INTEGER, "
        "episode INTEGER, status TEXT, UNIQUE(series_id, season, episode));"
        "CREATE TABLE movies (id INTEGER PRIMARY KEY, provider TEXT, provider_id TEXT, "
        "title TEXT, added_at REAL, UNIQUE(provider, provider_id));"
    )
    conn.commit()
    conn.close()
    d = Database(p)
    assert "is_upgrade" in {r["name"] for r in d.query("PRAGMA table_info(downloads)")}
    assert "quality" in {r["name"] for r in d.query("PRAGMA table_info(episodes)")}
    assert "quality" in {r["name"] for r in d.query("PRAGMA table_info(movies)")}
