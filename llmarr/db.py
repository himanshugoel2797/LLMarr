"""SQLite persistence for the media library, download history and RSS state.

A single connection guarded by a lock is enough — the server is low-throughput
and writes are short. Rows are returned as plain dicts.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    provider      TEXT NOT NULL DEFAULT 'tmdb',
    provider_id   TEXT NOT NULL,
    title         TEXT NOT NULL,
    year          INTEGER,
    overview      TEXT,
    status        TEXT,
    poster        TEXT,
    monitored     INTEGER NOT NULL DEFAULT 1,
    quality_profile TEXT,
    root_folder   TEXT,
    folder_name   TEXT,
    absolute_numbering INTEGER NOT NULL DEFAULT 0,
    last_refresh  REAL,                              -- last metadata re-fetch (G1)
    plex_rating_key TEXT,                            -- Plex identity from import (G5)
    plex_section  TEXT,
    added_at      REAL NOT NULL,
    UNIQUE(provider, provider_id)
);

CREATE TABLE IF NOT EXISTS episodes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id     INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    season        INTEGER NOT NULL,
    episode       INTEGER NOT NULL,
    title         TEXT,
    air_date      TEXT,
    monitored     INTEGER NOT NULL DEFAULT 1,
    status        TEXT NOT NULL DEFAULT 'missing',  -- missing|grabbed|downloaded
    file_path     TEXT,
    UNIQUE(series_id, season, episode)
);

CREATE TABLE IF NOT EXISTS movies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    provider      TEXT NOT NULL DEFAULT 'tmdb',
    provider_id   TEXT NOT NULL,
    title         TEXT NOT NULL,
    year          INTEGER,
    overview      TEXT,
    status        TEXT,
    poster        TEXT,
    monitored     INTEGER NOT NULL DEFAULT 1,
    movie_status  TEXT NOT NULL DEFAULT 'missing',  -- missing|grabbed|downloaded
    quality_profile TEXT,
    root_folder   TEXT,
    folder_name   TEXT,
    file_path     TEXT,
    added_at      REAL NOT NULL,
    UNIQUE(provider, provider_id)
);

CREATE TABLE IF NOT EXISTS downloads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    series_id     INTEGER REFERENCES series(id) ON DELETE SET NULL,
    episode_id    INTEGER REFERENCES episodes(id) ON DELETE SET NULL,
    movie_id      INTEGER REFERENCES movies(id) ON DELETE SET NULL,
    title         TEXT NOT NULL,
    indexer       TEXT,
    download_url  TEXT,
    torrent_hash  TEXT,
    client        TEXT,
    category      TEXT,
    save_path     TEXT,
    quality       TEXT,
    size          INTEGER,
    status        TEXT NOT NULL DEFAULT 'grabbed',  -- grabbed|downloading|completed|imported|failed
    grabbed_at    REAL NOT NULL
);

-- Every release guid we have already acted on, so RSS polling never
-- double-grabs the same release.
CREATE TABLE IF NOT EXISTS grab_history (
    guid          TEXT PRIMARY KEY,
    grabbed_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key           TEXT PRIMARY KEY,
    value         TEXT
);

-- OAuth clients registered dynamically (RFC 7591) by MCP clients like claude.ai.
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id     TEXT PRIMARY KEY,
    client_name   TEXT,
    redirect_uris TEXT NOT NULL,   -- JSON array
    created_at    REAL NOT NULL
);
"""


def default_db_path() -> Path:
    env = os.environ.get("LLMARR_DB")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
    return Path(base) / "llmarr" / "llmarr.db"


def _row_to_dict(cursor: sqlite3.Cursor, row: sqlite3.Row) -> dict:
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


class Database:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = _row_to_dict
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Additive migrations for databases created by an earlier version."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(downloads)")}
        if "movie_id" not in cols:
            self._conn.execute(
                "ALTER TABLE downloads ADD COLUMN movie_id INTEGER "
                "REFERENCES movies(id) ON DELETE SET NULL"
            )
        scols = {r["name"] for r in self._conn.execute("PRAGMA table_info(series)")}
        if "absolute_numbering" not in scols:
            self._conn.execute(
                "ALTER TABLE series ADD COLUMN absolute_numbering INTEGER NOT NULL DEFAULT 0"
            )
        if "last_refresh" not in scols:
            # When metadata was last re-fetched (G1 periodic refresh). NULL = never.
            self._conn.execute("ALTER TABLE series ADD COLUMN last_refresh REAL")
        if "plex_rating_key" not in scols:
            # Plex identity captured on import_from_plex (G5), for reliable lookups.
            self._conn.execute("ALTER TABLE series ADD COLUMN plex_rating_key TEXT")
        if "plex_section" not in scols:
            self._conn.execute("ALTER TABLE series ADD COLUMN plex_section TEXT")

    # -- low level ---------------------------------------------------------- #
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            return cur.fetchall()

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # -- kv ----------------------------------------------------------------- #
    def get_kv(self, key: str) -> Optional[str]:
        row = self.query_one("SELECT value FROM kv WHERE key=?", (key,))
        return row["value"] if row else None

    def set_kv(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # -- series ------------------------------------------------------------- #
    def upsert_series(self, **fields) -> int:
        fields.setdefault("added_at", time.time())
        cols = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        # Never overwrite identity/first-add fields on a metadata refresh.
        updates = ", ".join(
            f"{c}=excluded.{c}"
            for c in fields
            if c not in ("provider", "provider_id", "added_at")
        )
        sql = (
            f"INSERT INTO series ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(provider, provider_id) DO UPDATE SET {updates}"
        )
        self.execute(sql, list(fields.values()))
        row = self.query_one(
            "SELECT id FROM series WHERE provider=? AND provider_id=?",
            (fields["provider"], fields["provider_id"]),
        )
        return row["id"]

    def get_series(self, series_id: int) -> Optional[dict]:
        return self.query_one("SELECT * FROM series WHERE id=?", (series_id,))

    def list_series(self) -> list[dict]:
        return self.query("SELECT * FROM series ORDER BY title")

    def delete_series(self, series_id: int) -> None:
        self.execute("DELETE FROM series WHERE id=?", (series_id,))

    # -- movies ------------------------------------------------------------- #
    def upsert_movie(self, **fields) -> int:
        fields.setdefault("added_at", time.time())
        cols = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        updates = ", ".join(
            f"{c}=excluded.{c}"
            for c in fields
            if c not in ("provider", "provider_id", "movie_status", "file_path")
        )
        sql = (
            f"INSERT INTO movies ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(provider, provider_id) DO UPDATE SET {updates}"
        )
        self.execute(sql, list(fields.values()))
        row = self.query_one(
            "SELECT id FROM movies WHERE provider=? AND provider_id=?",
            (fields["provider"], fields["provider_id"]),
        )
        return row["id"]

    def get_movie(self, movie_id: int) -> Optional[dict]:
        return self.query_one("SELECT * FROM movies WHERE id=?", (movie_id,))

    def list_movies(self) -> list[dict]:
        return self.query("SELECT * FROM movies ORDER BY title")

    def delete_movie(self, movie_id: int) -> None:
        self.execute("DELETE FROM movies WHERE id=?", (movie_id,))

    def set_movie_status(self, movie_id: int, status: str, file_path: Optional[str] = None):
        if file_path is not None:
            self.execute(
                "UPDATE movies SET movie_status=?, file_path=? WHERE id=?",
                (status, file_path, movie_id),
            )
        else:
            self.execute("UPDATE movies SET movie_status=? WHERE id=?", (status, movie_id))

    def set_movie_monitored(self, movie_id: int, monitored: bool):
        self.execute(
            "UPDATE movies SET monitored=? WHERE id=?", (1 if monitored else 0, movie_id)
        )

    # -- episodes ----------------------------------------------------------- #
    def upsert_episode(self, series_id: int, season: int, episode: int, **fields) -> int:
        base = {"series_id": series_id, "season": season, "episode": episode}
        base.update(fields)
        cols = ", ".join(base)
        placeholders = ", ".join("?" for _ in base)
        # Do not clobber status/file_path/monitored on metadata refresh.
        updatable = [c for c in fields if c in ("title", "air_date")]
        updates = ", ".join(f"{c}=excluded.{c}" for c in updatable) or "title=title"
        sql = (
            f"INSERT INTO episodes ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(series_id, season, episode) DO UPDATE SET {updates}"
        )
        self.execute(sql, list(base.values()))
        row = self.query_one(
            "SELECT id FROM episodes WHERE series_id=? AND season=? AND episode=?",
            (series_id, season, episode),
        )
        return row["id"]

    def list_episodes(
        self, series_id: int, status: Optional[str] = None, monitored: Optional[bool] = None
    ) -> list[dict]:
        sql = "SELECT * FROM episodes WHERE series_id=?"
        params: list[Any] = [series_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        if monitored is not None:
            sql += " AND monitored=?"
            params.append(1 if monitored else 0)
        sql += " ORDER BY season, episode"
        return self.query(sql, params)

    def get_episode(self, episode_id: int) -> Optional[dict]:
        return self.query_one("SELECT * FROM episodes WHERE id=?", (episode_id,))

    def set_episode_status(self, episode_id: int, status: str, file_path: Optional[str] = None):
        if file_path is not None:
            self.execute(
                "UPDATE episodes SET status=?, file_path=? WHERE id=?",
                (status, file_path, episode_id),
            )
        else:
            self.execute("UPDATE episodes SET status=? WHERE id=?", (status, episode_id))

    def set_monitored(self, series_id: int, monitored: bool, season: Optional[int] = None):
        val = 1 if monitored else 0
        if season is None:
            self.execute("UPDATE series SET monitored=? WHERE id=?", (val, series_id))
            self.execute("UPDATE episodes SET monitored=? WHERE series_id=?", (val, series_id))
        else:
            self.execute(
                "UPDATE episodes SET monitored=? WHERE series_id=? AND season=?",
                (val, series_id, season),
            )

    # -- downloads ---------------------------------------------------------- #
    def add_download(self, **fields) -> int:
        fields.setdefault("grabbed_at", time.time())
        cols = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        cur = self.execute(
            f"INSERT INTO downloads ({cols}) VALUES ({placeholders})",
            list(fields.values()),
        )
        return cur.lastrowid

    def list_downloads(self, status: Optional[str] = None) -> list[dict]:
        if status:
            return self.query(
                "SELECT * FROM downloads WHERE status=? ORDER BY grabbed_at DESC", (status,)
            )
        return self.query("SELECT * FROM downloads ORDER BY grabbed_at DESC")

    def get_download(self, download_id: int) -> Optional[dict]:
        return self.query_one("SELECT * FROM downloads WHERE id=?", (download_id,))

    def set_download_status(self, download_id: int, status: str, **fields):
        sets = ["status=?"]
        params: list[Any] = [status]
        for k, v in fields.items():
            sets.append(f"{k}=?")
            params.append(v)
        params.append(download_id)
        self.execute(f"UPDATE downloads SET {', '.join(sets)} WHERE id=?", params)

    # -- grab history ------------------------------------------------------- #
    def seen_guid(self, guid: str) -> bool:
        return self.query_one("SELECT 1 FROM grab_history WHERE guid=?", (guid,)) is not None

    def record_guid(self, guid: str) -> None:
        self.execute(
            "INSERT OR IGNORE INTO grab_history(guid, grabbed_at) VALUES(?, ?)",
            (guid, time.time()),
        )

    # -- oauth clients ------------------------------------------------------ #
    def add_oauth_client(self, client_id: str, client_name: str, redirect_uris: str) -> None:
        self.execute(
            "INSERT OR REPLACE INTO oauth_clients(client_id, client_name, redirect_uris, created_at) "
            "VALUES(?, ?, ?, ?)",
            (client_id, client_name, redirect_uris, time.time()),
        )

    def get_oauth_client(self, client_id: str) -> Optional[dict]:
        return self.query_one("SELECT * FROM oauth_clients WHERE client_id=?", (client_id,))
