"""Importer tests — real files in tmp dirs, real hardlinks."""

from pathlib import Path

import pytest

from llmarr.config import PathMapping, RootFolder


@pytest.fixture
def library(app, tmp_path):
    """Configure an app with a download dir + library and matching mappings."""
    dl = tmp_path / "dl"
    lib = tmp_path / "library"
    dl.mkdir()
    lib.mkdir()

    def setup(c):
        c.importer.min_video_mb = 0
        c.importer.work_context = "local"
        c.single_host = False  # container-style: explicit mappings, strict
        c.path_mappings = [
            PathMapping(group="dl", context="qbittorrent", path="/downloads"),
            PathMapping(group="dl", context="local", path=str(dl)),
            PathMapping(group="lib", context="local", path=str(lib)),
            PathMapping(group="lib", context="plex", path="/data/tv"),
        ]
        c.root_folders = [
            RootFolder(name="tv", media_type="tv", context="local", path=str(lib)),
            RootFolder(name="mv", media_type="movie", context="local", path=str(lib / "movies")),
        ]

    app.store.mutate(setup)
    return app, dl, lib


def write(path: Path, size=1000):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x" * size)
    return path


def test_single_episode_hardlink(library):
    app, dl, lib = library
    sid = app.db.upsert_series(
        provider="tmdb", provider_id="1", title="Severance", year=2022,
        root_folder="tv", folder_name="Severance (2022)",
    )
    e = app.db.upsert_episode(sid, 1, 1, title="Good News About Hell")
    src = write(dl / "Severance.S01E01.1080p.WEB-DL.mkv")
    d = {"id": 1, "series_id": sid, "episode_id": e, "movie_id": None}

    res = app.importer.import_download(d, "/downloads/Severance.S01E01.1080p.WEB-DL.mkv")

    assert res.ok, res
    imp = res.imported[0]
    assert imp.action == "hardlink"
    dest = Path(imp.destination)
    assert dest.name == "Severance - S01E01 - Good News About Hell.mkv"
    assert "Season 01" in str(dest)
    # Hardlink -> same inode as the source.
    assert dest.stat().st_ino == src.stat().st_ino
    assert app.db.get_episode(e)["status"] == "downloaded"
    assert res.scan_paths == [str(dest.parent)]


def test_single_file_uses_linked_episode_without_se_in_name(library):
    app, dl, lib = library
    sid = app.db.upsert_series(
        provider="tmdb", provider_id="1", title="Show", root_folder="tv", folder_name="Show"
    )
    e = app.db.upsert_episode(sid, 3, 4, title="Ep")
    write(dl / "some.random.name.mkv")
    d = {"id": 1, "series_id": sid, "episode_id": e, "movie_id": None}
    res = app.importer.import_download(d, "/downloads/some.random.name.mkv")
    assert res.ok
    assert res.imported[0].season == 3 and res.imported[0].episode == 4


def test_season_pack_matches_multiple_and_skips_sample(library):
    app, dl, lib = library
    sid = app.db.upsert_series(
        provider="tmdb", provider_id="1", title="Show", root_folder="tv", folder_name="Show"
    )
    e1 = app.db.upsert_episode(sid, 2, 1, title="One")
    e2 = app.db.upsert_episode(sid, 2, 2, title="Two")
    pack = dl / "Show.S02.1080p"
    write(pack / "Show.S02E01.mkv")
    write(pack / "Show.S02E02.mkv")
    write(pack / "sample.mkv")  # skipped by name
    d = {"id": 1, "series_id": sid, "episode_id": None, "movie_id": None}

    res = app.importer.import_download(d, "/downloads/Show.S02.1080p")

    got = sorted((i.season, i.episode) for i in res.imported)
    assert got == [(2, 1), (2, 2)]
    assert app.db.get_episode(e1)["status"] == "downloaded"
    assert app.db.get_episode(e2)["status"] == "downloaded"


def test_rename_off_keeps_original_name(library):
    app, dl, lib = library
    app.store.mutate(lambda c: setattr(c.importer, "rename", False))
    sid = app.db.upsert_series(
        provider="tmdb", provider_id="1", title="Show", root_folder="tv", folder_name="Show"
    )
    e = app.db.upsert_episode(sid, 1, 1)
    write(dl / "Show.S01E01.WEB.mkv")
    d = {"id": 1, "series_id": sid, "episode_id": e, "movie_id": None}
    res = app.importer.import_download(d, "/downloads/Show.S01E01.WEB.mkv")
    assert Path(res.imported[0].destination).name == "Show.S01E01.WEB.mkv"


def test_copy_mode(library):
    app, dl, lib = library
    app.store.mutate(lambda c: setattr(c.importer, "mode", "copy"))
    sid = app.db.upsert_series(
        provider="tmdb", provider_id="1", title="Show", root_folder="tv", folder_name="Show"
    )
    e = app.db.upsert_episode(sid, 1, 1)
    src = write(dl / "Show.S01E01.mkv")
    d = {"id": 1, "series_id": sid, "episode_id": e, "movie_id": None}
    res = app.importer.import_download(d, "/downloads/Show.S01E01.mkv")
    dest = Path(res.imported[0].destination)
    assert res.imported[0].action == "copy"
    assert dest.stat().st_ino != src.stat().st_ino  # copy -> different inode
    assert src.exists()  # original untouched


def test_movie_picks_largest_file(library):
    app, dl, lib = library
    mid = app.db.upsert_movie(
        provider="tmdb", provider_id="9", title="Dune", year=2021,
        root_folder="mv", folder_name="Dune (2021)",
    )
    mdir = dl / "Dune.2021.2160p"
    write(mdir / "Dune.2021.2160p.mkv", size=5000)
    write(mdir / "extras.mkv", size=100)
    d = {"id": 1, "series_id": None, "episode_id": None, "movie_id": mid}
    res = app.importer.import_download(d, "/downloads/Dune.2021.2160p")
    assert res.ok
    assert Path(res.imported[0].destination).name == "Dune (2021).mkv"
    assert app.db.get_movie(mid)["movie_status"] == "downloaded"


def test_unmapped_path_errors(library):
    app, dl, lib = library
    sid = app.db.upsert_series(
        provider="tmdb", provider_id="1", title="Show", root_folder="tv", folder_name="Show"
    )
    e = app.db.upsert_episode(sid, 1, 1)
    d = {"id": 1, "series_id": sid, "episode_id": e, "movie_id": None}
    res = app.importer.import_download(d, "/not/mapped/file.mkv")
    assert res.errors and not res.imported
    assert "path mapping" in res.errors[0]


def test_no_content_path_errors(library):
    app, dl, lib = library
    d = {"id": 1, "series_id": None, "episode_id": None, "movie_id": None}
    res = app.importer.import_download(d, None)
    assert res.errors and not res.imported


def test_single_host_no_mappings_needed(app, tmp_path):
    """The non-container case: no path mappings, paths are identical everywhere."""
    lib = tmp_path / "lib"
    lib.mkdir()

    def setup(c):
        c.importer.min_video_mb = 0
        assert c.single_host is True  # default
        c.root_folders = [RootFolder(name="tv", media_type="tv", context="local", path=str(lib))]

    app.store.mutate(setup)
    sid = app.db.upsert_series(
        provider="tmdb", provider_id="1", title="Show", year=2020,
        root_folder="tv", folder_name="Show (2020)",
    )
    e = app.db.upsert_episode(sid, 1, 1, title="Pilot")
    # In single-host mode the download path is a real local path already.
    src = write(tmp_path / "downloads" / "Show.S01E01.1080p.mkv")
    d = {"id": 1, "series_id": sid, "episode_id": e, "movie_id": None}

    res = app.importer.import_download(d, str(src))
    assert res.ok, res
    dest = Path(res.imported[0].destination)
    assert dest.name == "Show - S01E01 - Pilot.mkv"
    assert dest.stat().st_ino == src.stat().st_ino  # hardlink


def test_missing_video_files_skipped(library):
    app, dl, lib = library
    sid = app.db.upsert_series(
        provider="tmdb", provider_id="1", title="Show", root_folder="tv", folder_name="Show"
    )
    app.db.upsert_episode(sid, 1, 1)
    folder = dl / "Show.S01"
    write(folder / "readme.txt")  # no video
    d = {"id": 1, "series_id": sid, "episode_id": None, "movie_id": None}
    res = app.importer.import_download(d, "/downloads/Show.S01")
    assert not res.imported and res.skipped
