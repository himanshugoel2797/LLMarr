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


# --- quality upgrades (G4) ------------------------------------------------- #
def test_import_records_quality(library):
    app, dl, lib = library
    sid = app.db.upsert_series(
        provider="tmdb", provider_id="1", title="Show", root_folder="tv", folder_name="Show"
    )
    e = app.db.upsert_episode(sid, 1, 1)
    write(dl / "Show.S01E01.720p.WEB.mkv")
    d = {"id": 1, "series_id": sid, "episode_id": e, "movie_id": None}
    res = app.importer.import_download(d, "/downloads/Show.S01E01.720p.WEB.mkv")
    assert res.ok
    assert app.db.get_episode(e)["quality"] == "720p"


def test_upgrade_replaces_existing_episode_file(library):
    app, dl, lib = library
    sid = app.db.upsert_series(
        provider="tmdb", provider_id="1", title="Show", root_folder="tv", folder_name="Show"
    )
    e = app.db.upsert_episode(sid, 1, 1, title="Ep")
    # First, a 720p import.
    write(dl / "Show.S01E01.720p.WEB.mkv")
    d1 = {"id": 1, "series_id": sid, "episode_id": e, "movie_id": None}
    r1 = app.importer.import_download(d1, "/downloads/Show.S01E01.720p.WEB.mkv")
    dest = Path(r1.imported[0].destination)
    assert dest.exists() and app.db.get_episode(e)["quality"] == "720p"

    # Now a 1080p upgrade — same renamed dst, so it overwrites in place.
    src2 = write(dl / "Show.S01E01.1080p.WEB.mkv")
    d2 = {"id": 2, "series_id": sid, "episode_id": e, "movie_id": None, "is_upgrade": 1}
    r2 = app.importer.import_download(d2, "/downloads/Show.S01E01.1080p.WEB.mkv")
    assert r2.ok, r2
    assert app.db.get_episode(e)["quality"] == "1080p"
    # The library file now points at the new source (same inode via hardlink).
    assert dest.stat().st_ino == src2.stat().st_ino


def test_upgrade_removes_old_file_when_rename_off(library):
    app, dl, lib = library
    app.store.mutate(lambda c: setattr(c.importer, "rename", False))
    sid = app.db.upsert_series(
        provider="tmdb", provider_id="1", title="Show", root_folder="tv", folder_name="Show"
    )
    e = app.db.upsert_episode(sid, 1, 1)
    write(dl / "Show.S01E01.720p.mkv")
    d1 = {"id": 1, "series_id": sid, "episode_id": e, "movie_id": None}
    r1 = app.importer.import_download(d1, "/downloads/Show.S01E01.720p.mkv")
    old = Path(r1.imported[0].destination)
    assert old.exists()

    write(dl / "Show.S01E01.1080p.mkv")
    d2 = {"id": 2, "series_id": sid, "episode_id": e, "movie_id": None, "is_upgrade": 1}
    r2 = app.importer.import_download(d2, "/downloads/Show.S01E01.1080p.mkv")
    new = Path(r2.imported[0].destination)
    # Different name (rename off), so the old file is explicitly cleaned up.
    assert new.exists() and new != old
    assert not old.exists()
    assert str(old) in r2.replaced


def test_upgrade_skips_when_not_better(library):
    app, dl, lib = library
    sid = app.db.upsert_series(
        provider="tmdb", provider_id="1", title="Show", root_folder="tv", folder_name="Show"
    )
    e = app.db.upsert_episode(sid, 1, 1)
    write(dl / "Show.S01E01.1080p.mkv")
    d1 = {"id": 1, "series_id": sid, "episode_id": e, "movie_id": None}
    app.importer.import_download(d1, "/downloads/Show.S01E01.1080p.mkv")
    assert app.db.get_episode(e)["quality"] == "1080p"

    # A 720p "upgrade" must be refused — never downgrade.
    write(dl / "Show.S01E01.720p.mkv")
    d2 = {"id": 2, "series_id": sid, "episode_id": e, "movie_id": None, "is_upgrade": 1}
    r2 = app.importer.import_download(d2, "/downloads/Show.S01E01.720p.mkv")
    assert not r2.imported and r2.skipped
    assert app.db.get_episode(e)["quality"] == "1080p"


def test_movie_upgrade_replaces_and_records_quality(library):
    app, dl, lib = library
    mid = app.db.upsert_movie(
        provider="tmdb", provider_id="1", title="Film", year=2020,
        root_folder="mv", folder_name="Film (2020)",
    )
    write(dl / "Film.2020.720p.mkv", size=2000)
    d1 = {"id": 1, "series_id": None, "episode_id": None, "movie_id": mid}
    r1 = app.importer.import_download(d1, "/downloads/Film.2020.720p.mkv")
    assert app.db.get_movie(mid)["quality"] == "720p"
    dest = Path(r1.imported[0].destination)

    src2 = write(dl / "Film.2020.1080p.mkv", size=2000)
    d2 = {"id": 2, "series_id": None, "episode_id": None, "movie_id": mid, "is_upgrade": 1}
    r2 = app.importer.import_download(d2, "/downloads/Film.2020.1080p.mkv")
    assert r2.ok
    assert app.db.get_movie(mid)["quality"] == "1080p"
    assert dest.stat().st_ino == src2.stat().st_ino


# -- bundled sequels and specials (pack layout) ----------------------------- #

def _amagami_style_pack(app, dl, extras=True):
    """A 'complete series' pack: the grabbed show, a differently-named sequel
    bundled alongside it, and specials from both."""
    sid = app.db.upsert_series(
        provider="jikan", provider_id="8676", title="Amagami SS", year=2010,
        root_folder="tv", folder_name="Amagami SS (2010)", absolute_numbering=1,
    )
    for n in (1, 2):
        app.db.upsert_episode(sid, 1, n, title=f"Ep {n}")
    pack = dl / "Amagami SS"
    for n in (1, 2, 3):  # 3 is a bonus episode the provider never listed
        write(pack / "Amagami SS" / f"[DB]Amagami SS_-_{n:02d}_(10bit_BD1080p_x265).mkv")
    for n in (1, 2):
        write(pack / "Amagami SS+ Plus" / f"[DB]Amagami SS+ Plus_-_{n:02d}_(10bit_BD1080p_x265).mkv")
    if extras:
        write(pack / "Amagami SS" / "Extras" / "[DB]Amagami SS_-_SP01-03_(10bit_DVD576p_x265).mkv")
        write(pack / "Amagami SS+ Plus" / "Extras" / "[DB]Amagami SS+ Plus_-_SP02_(10bit_BD1080p_x265).mkv")
    return sid, {"id": 1, "series_id": sid, "episode_id": None, "movie_id": None}


def test_bundled_sequel_becomes_a_later_season(library):
    app, dl, lib = library
    sid, d = _amagami_style_pack(app, dl, extras=False)

    res = app.importer.import_download(d, "/downloads/Amagami SS")

    got = sorted((i.season, i.episode) for i in res.imported)
    # The grabbed show stays season 1; the sequel lands in season 2 with its own
    # numbering rather than colliding with season 1's episodes 1-2.
    assert got == [(1, 1), (1, 2), (1, 3), (2, 1), (2, 2)], res
    assert not res.errors and not res.skipped, res
    assert (lib / "Amagami SS (2010)" / "Season 02" / "Amagami SS - S02E01.mkv").exists()
    # Every file got its own destination — nothing silently swallowed as "exists".
    assert len({i.destination for i in res.imported}) == 5


def test_pack_registers_episodes_metadata_never_listed(library):
    app, dl, lib = library
    sid, d = _amagami_style_pack(app, dl, extras=False)

    res = app.importer.import_download(d, "/downloads/Amagami SS")

    assert sorted(res.added_episodes) == ["S01E03", "S02E01", "S02E02"]
    rows = {(e["season"], e["episode"]): e for e in app.db.list_episodes(sid)}
    assert set(rows) == {(1, 1), (1, 2), (1, 3), (2, 1), (2, 2)}
    assert all(e["status"] == "downloaded" and e["file_path"] for e in rows.values())


def test_specials_go_to_season_zero_without_colliding(library):
    app, dl, lib = library
    sid, d = _amagami_style_pack(app, dl, extras=True)

    res = app.importer.import_download(d, "/downloads/Amagami SS")

    specials = sorted(i.episode for i in res.imported if i.season == 0)
    # SP01-03 is one file spanning three specials; the sequel's SP02 follows it
    # rather than reusing a number already taken by the first show.
    assert specials == [1, 2, 3, 4]
    span = {i.destination for i in res.imported if i.season == 0 and i.episode in (1, 2, 3)}
    assert len(span) == 1  # one physical file covers the three-special span
    assert (lib / "Amagami SS (2010)" / "Season 00").is_dir()


def test_single_show_pack_still_maps_to_season_one(library):
    """The common case is unchanged: no sequel bundled, no season shuffling."""
    app, dl, lib = library
    sid = app.db.upsert_series(
        provider="jikan", provider_id="1", title="Aethering", year=2023,
        root_folder="tv", folder_name="Aethering (2023)", absolute_numbering=1,
    )
    app.db.upsert_episode(sid, 1, 1, title="One")
    pack = dl / "Aethering"
    write(pack / "[FanSubA] Aethering - 01 (1080p).mkv")
    write(pack / "[FanSubA] Aethering - 02 (1080p).mkv")
    d = {"id": 1, "series_id": sid, "episode_id": None, "movie_id": None}

    res = app.importer.import_download(d, "/downloads/Aethering")

    assert sorted((i.season, i.episode) for i in res.imported) == [(1, 1), (1, 2)]


def test_create_missing_episodes_off_skips_unlisted(library):
    app, dl, lib = library
    app.store.mutate(lambda c: setattr(c.importer, "create_missing_episodes", False))
    sid, d = _amagami_style_pack(app, dl, extras=False)

    res = app.importer.import_download(d, "/downloads/Amagami SS")

    assert sorted((i.season, i.episode) for i in res.imported) == [(1, 1), (1, 2)]
    assert res.added_episodes == []
    assert len(res.skipped) == 3


def test_regular_tv_never_invents_episodes(library):
    """Auto-creation is absolute-numbering only — ordinary TV keeps skipping."""
    app, dl, lib = library
    sid = app.db.upsert_series(
        provider="tmdb", provider_id="1", title="Meridian", year=2022,
        root_folder="tv", folder_name="Meridian (2022)",
    )
    app.db.upsert_episode(sid, 1, 1, title="One")
    pack = dl / "Meridian.S01"
    write(pack / "Meridian.S01E01.1080p.mkv")
    write(pack / "Meridian.S01E09.1080p.mkv")
    d = {"id": 1, "series_id": sid, "episode_id": None, "movie_id": None}

    res = app.importer.import_download(d, "/downloads/Meridian.S01")

    assert [(i.season, i.episode) for i in res.imported] == [(1, 1)]
    assert res.added_episodes == []
    assert any("S01E09 not in library" in s for s in res.skipped)
