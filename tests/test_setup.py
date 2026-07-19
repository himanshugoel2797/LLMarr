"""Tests for the guided setup_status diagnostics."""

from llmarr import setup as setupmod
from llmarr.config import DownloadClientConfig, PathMapping, RootFolder


def _step(status, sid):
    return next(s for s in status["steps"] if s["id"] == sid)


def test_empty_config_next_is_metadata(app):
    st = setupmod.build_status(app)
    assert _step(st, "metadata")["done"] is False
    assert st["next"]["id"] == "metadata"
    assert st["summary"].startswith("0/")


def test_jikan_default_counts_metadata_done(app):
    app.store.mutate(lambda c: setattr(c.metadata, "provider", "jikan"))
    assert _step(setupmod.build_status(app), "metadata")["done"] is True


def test_tmdb_needs_key(app):
    app.store.mutate(lambda c: setattr(c.metadata, "provider", "tmdb"))
    assert _step(setupmod.build_status(app), "metadata")["done"] is False
    app.store.mutate(lambda c: setattr(c.metadata, "tmdb_api_key", "k"))
    assert _step(setupmod.build_status(app), "metadata")["done"] is True


def test_fully_configured_all_done(app):
    def setup(c):
        c.metadata.provider = "jikan"
        c.prowlarr.url = "http://p"
        c.prowlarr.api_key = "k"
        c.download_clients["qbit"] = DownloadClientConfig(url="http://qb")
        c.default_download_client = "qbit"
        c.plex.url = "http://plex"
        c.plex.token = "t"
        c.root_folders = [RootFolder(name="tv", media_type="tv", path="/tv")]
    app.store.mutate(setup)
    st = setupmod.build_status(app)
    assert st["next"] is None
    assert all(s["done"] for s in st["steps"] if s["required"])


def test_options_enumerated(app):
    opts = setupmod.build_status(app)["options"]
    assert [p["name"] for p in opts["metadata_providers"]] == ["tmdb", "jikan"]
    assert any(p["needs_api_key"] is False for p in opts["metadata_providers"])  # jikan
    assert opts["download_client_types"] == ["qbittorrent"]
    assert opts["auth_modes"] == ["token", "oauth", "none"]
    assert "hardlink" in opts["import_modes"]


def test_path_mappings_optional_in_single_host(app):
    # single_host default true -> path mappings not required
    assert _step(setupmod.build_status(app), "path_mappings")["required"] is False


def test_path_mappings_required_when_split(app):
    app.store.mutate(lambda c: setattr(c, "single_host", False))
    st = setupmod.build_status(app)
    step = _step(st, "path_mappings")
    assert step["required"] is True and step["done"] is False
    app.store.mutate(lambda c: c.path_mappings.append(
        PathMapping(group="d", context="qbittorrent", path="/data")))
    assert _step(setupmod.build_status(app), "path_mappings")["done"] is True


def test_plex_library_suggestions(app):
    app.store.mutate(lambda c: (setattr(c.plex, "url", "http://p"),
                                setattr(c.plex, "token", "t")))
    libs = [
        {"title": "Anime", "type": "show", "locations": ["/mnt/plex-data/videos"]},
        {"title": "Movies", "type": "movie", "locations": ["/mnt/plex-data/movies"]},
        {"title": "Music", "type": "artist", "locations": ["/mnt/plex-data/music"]},
    ]
    st = setupmod.build_status(app, plex_libraries=libs)
    sug = st["detected"]["suggested_root_folders"]
    assert any("/mnt/plex-data/videos" in s and 'media_type="tv"' in s for s in sug)
    assert any("/mnt/plex-data/movies" in s and 'media_type="movie"' in s for s in sug)
    assert not any("music" in s for s in sug)  # artist libraries skipped
