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
        provider="tmdb", provider_id="1", title="Meridian", year=2022,
        root_folder="tv", folder_name="Meridian (2022)",
    )
    e = app.db.upsert_episode(sid, 1, 1, title="Good News About Hell")
    src = write(dl / "Meridian.S01E01.1080p.WEB-DL.mkv")
    d = {"id": 1, "series_id": sid, "episode_id": e, "movie_id": None}

    res = app.importer.import_download(d, "/downloads/Meridian.S01E01.1080p.WEB-DL.mkv")

    assert res.ok, res
    imp = res.imported[0]
    assert imp.action == "hardlink"
    dest = Path(imp.destination)
    assert dest.name == "Meridian - S01E01 - Good News About Hell.mkv"
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
        provider="tmdb", provider_id="9", title="Nebula", year=2021,
        root_folder="mv", folder_name="Nebula (2021)",
    )
    mdir = dl / "Nebula.2021.2160p"
    write(mdir / "Nebula.2021.2160p.mkv", size=5000)
    write(mdir / "extras.mkv", size=100)
    d = {"id": 1, "series_id": None, "episode_id": None, "movie_id": mid}
    res = app.importer.import_download(d, "/downloads/Nebula.2021.2160p")
    assert res.ok
    assert Path(res.imported[0].destination).name == "Nebula (2021).mkv"
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


def test_anime_absolute_numbered_import(library):
    app, dl, lib = library
    sid = app.db.upsert_series(
        provider="jikan", provider_id="52991", title="Aethering", year=2023,
        root_folder="tv", folder_name="Aethering (2023)", absolute_numbering=1,
    )
    e1 = app.db.upsert_episode(sid, 1, 1, title="The Journey's End")
    e2 = app.db.upsert_episode(sid, 1, 2, title="It Didn't Have to Be Magic")
    pack = dl / "Aethering.Batch"
    write(pack / "[FanSubA] Aetheria Gaiden - 01 (1080p) [ABCD].mkv")
    write(pack / "[FanSubA] Aetheria Gaiden - 02 (1080p) [EF12].mkv")
    d = {"id": 1, "series_id": sid, "episode_id": None, "movie_id": None}

    res = app.importer.import_download(d, "/downloads/Aethering.Batch")

    got = sorted((i.season, i.episode) for i in res.imported)
    assert got == [(1, 1), (1, 2)], res
    assert app.db.get_episode(e1)["status"] == "downloaded"
    assert app.db.get_episode(e2)["status"] == "downloaded"
    # Renamed into the Season 01 layout.
    assert "Season 01" in res.imported[0].destination


def test_double_episode_file_marks_both(library):
    app, dl, lib = library
    sid = app.db.upsert_series(
        provider="tmdb", provider_id="1", title="Show", root_folder="tv", folder_name="Show"
    )
    e1 = app.db.upsert_episode(sid, 1, 1, title="One")
    e2 = app.db.upsert_episode(sid, 1, 2, title="Two")
    write(dl / "Show.S01E01E02.1080p.WEB.mkv")
    d = {"id": 1, "series_id": sid, "episode_id": None, "movie_id": None}
    res = app.importer.import_download(d, "/downloads/Show.S01E01E02.1080p.WEB.mkv")
    # both episodes marked downloaded, pointing at the one physical file
    assert app.db.get_episode(e1)["status"] == "downloaded"
    assert app.db.get_episode(e2)["status"] == "downloaded"
    dests = {i.destination for i in res.imported}
    assert len(dests) == 1  # single file
    assert "S01E01E02" in next(iter(dests))
    assert {(i.season, i.episode) for i in res.imported} == {(1, 1), (1, 2)}


def test_subtitle_sidecars_imported(library):
    app, dl, lib = library
    sid = app.db.upsert_series(
        provider="tmdb", provider_id="1", title="Show", root_folder="tv", folder_name="Show"
    )
    e = app.db.upsert_episode(sid, 1, 1, title="Pilot")
    write(dl / "Show.S01E01.WEB.mkv")
    write(dl / "Show.S01E01.WEB.en.srt")
    write(dl / "Show.S01E01.WEB.srt")
    write(dl / "unrelated.srt")  # different stem -> not imported
    d = {"id": 1, "series_id": sid, "episode_id": e, "movie_id": None}
    res = app.importer.import_download(d, "/downloads/Show.S01E01.WEB.mkv")
    names = {Path(i.destination).name for i in res.imported}
    assert "Show - S01E01 - Pilot.mkv" in names
    assert "Show - S01E01 - Pilot.en.srt" in names
    assert "Show - S01E01 - Pilot.srt" in names
    assert not any(n.startswith("unrelated") for n in names)


def test_movie_pack_imports_all_features(library):
    app, dl, lib = library
    mid = app.db.upsert_movie(
        provider="tmdb", provider_id="9", title="Trilogy", year=2021,
        root_folder="mv", folder_name="Trilogy (2021)",
    )
    mdir = dl / "Trilogy.Collection"
    write(mdir / "Trilogy.Part1.1080p.mkv", size=5000)
    write(mdir / "Trilogy.Part2.1080p.mkv", size=4800)
    write(mdir / "Trilogy.Part3.1080p.mkv", size=5200)
    write(mdir / "featurette.mkv", size=200)  # extra -> skipped
    d = {"id": 1, "series_id": None, "episode_id": None, "movie_id": mid}
    res = app.importer.import_download(d, "/downloads/Trilogy.Collection")
    names = sorted(Path(i.destination).name for i in res.imported)
    assert len(names) == 3  # three features, featurette skipped
    assert "Trilogy (2021).mkv" in names  # largest keeps the clean name
    assert all("featurette" not in n for n in names)
    assert app.db.get_movie(mid)["movie_status"] == "downloaded"


def test_min_free_space_blocks_copy(library):
    app, dl, lib = library
    # copy mode + an impossibly high free-space floor -> the copy is refused.
    app.store.mutate(lambda c: (setattr(c.importer, "mode", "copy"),
                                setattr(c.importer, "min_free_space_mb", 10 ** 12)))
    sid = app.db.upsert_series(
        provider="tmdb", provider_id="1", title="Show", root_folder="tv", folder_name="Show"
    )
    e = app.db.upsert_episode(sid, 1, 1)
    write(dl / "Show.S01E01.mkv")
    d = {"id": 1, "series_id": sid, "episode_id": e, "movie_id": None}
    res = app.importer.import_download(d, "/downloads/Show.S01E01.mkv")
    assert not res.imported and res.errors
    assert "free space" in res.errors[0]
    assert app.db.get_episode(e)["status"] == "missing"  # not marked


def test_min_free_space_disabled_by_default(library):
    app, dl, lib = library
    app.store.mutate(lambda c: setattr(c.importer, "mode", "copy"))
    assert app.config.importer.min_free_space_mb == 0  # default off
    sid = app.db.upsert_series(
        provider="tmdb", provider_id="1", title="Show", root_folder="tv", folder_name="Show"
    )
    e = app.db.upsert_episode(sid, 1, 1)
    write(dl / "Show.S01E01.mkv")
    d = {"id": 1, "series_id": sid, "episode_id": e, "movie_id": None}
    res = app.importer.import_download(d, "/downloads/Show.S01E01.mkv")
    assert res.ok  # no space check when floor is 0


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
