from llmarr import pathmap
from llmarr.config import Config, PathMapping


def cfg_with_maps():
    c = Config()
    c.path_mappings = [
        PathMapping(group="dl", context="qbittorrent", path="/downloads"),
        PathMapping(group="dl", context="plex", path="/data/torrents"),
        PathMapping(group="dl", context="local", path="/mnt/media/dl"),
    ]
    return c


def test_translate_nested_path():
    c = cfg_with_maps()
    assert (
        pathmap.translate(c, "/downloads/Show/S01E01.mkv", "qbittorrent", "plex")
        == "/data/torrents/Show/S01E01.mkv"
    )


def test_translate_exact_root():
    c = cfg_with_maps()
    assert pathmap.translate(c, "/downloads", "qbittorrent", "local") == "/mnt/media/dl"


def test_translate_same_context_is_identity():
    c = cfg_with_maps()
    assert pathmap.translate(c, "/anything/here", "plex", "plex") == "/anything/here"


def test_translate_no_mapping_returns_none_in_container_mode():
    c = cfg_with_maps()
    c.single_host = False  # strict split-container mode
    assert pathmap.translate(c, "/somewhere/else", "qbittorrent", "plex") is None


def test_translate_no_mapping_passthrough_in_single_host():
    c = cfg_with_maps()
    assert c.single_host is True  # default
    # Unmapped path passes through unchanged when everything shares a filesystem.
    assert pathmap.translate(c, "/somewhere/else", "qbittorrent", "plex") == "/somewhere/else"


def test_single_host_with_no_mappings_is_identity():
    c = Config()  # no mappings at all
    assert pathmap.translate(c, "/data/media/x.mkv", "qbittorrent", "local") == "/data/media/x.mkv"


def test_translate_trailing_slash_normalized():
    c = cfg_with_maps()
    assert (
        pathmap.translate(c, "/downloads/Show/", "qbittorrent", "local")
        == "/mnt/media/dl/Show"
    )


def test_longest_prefix_wins():
    c = Config()
    c.path_mappings = [
        PathMapping(group="a", context="qbittorrent", path="/data"),
        PathMapping(group="a", context="local", path="/A"),
        PathMapping(group="b", context="qbittorrent", path="/data/tv"),
        PathMapping(group="b", context="local", path="/B"),
    ]
    # /data/tv/... should resolve via the more specific group b.
    assert pathmap.translate(c, "/data/tv/show.mkv", "qbittorrent", "local") == "/B/show.mkv"
    assert pathmap.translate(c, "/data/movies/x.mkv", "qbittorrent", "local") == "/A/movies/x.mkv"


def test_prefix_boundary_not_partial_match():
    c = Config()
    c.single_host = False  # strict: unmapped -> None
    c.path_mappings = [
        PathMapping(group="a", context="qbittorrent", path="/downloads"),
        PathMapping(group="a", context="local", path="/L"),
    ]
    # /downloads-extra must NOT match /downloads.
    assert pathmap.translate(c, "/downloads-extra/x", "qbittorrent", "local") is None


def test_contexts_helper():
    c = cfg_with_maps()
    assert pathmap.contexts(c) == {"qbittorrent", "plex", "local"}
