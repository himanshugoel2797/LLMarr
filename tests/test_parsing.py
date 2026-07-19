import pytest

from llmarr import parsing


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Meridian.S02E01.1080p.WEB-DL.x265", (2, 1)),
        ("Show s1e2 720p", (1, 2)),
        ("Show 1x05 720p", (1, 5)),
        ("Show.S10E100.mkv", (10, 100)),
        ("Show.Season.1.Pack", None),
        ("Random.Movie.2021.1080p", None),
    ],
)
def test_parse_episode(title, expected):
    assert parsing.parse_episode(title) == expected


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Show.S01E01E02.1080p.WEB.mkv", [(1, 1), (1, 2)]),
        ("Show.S01E01-E02.mkv", [(1, 1), (1, 2)]),
        ("Show 1x01x02", [(1, 1), (1, 2)]),
        ("Meridian.S02E01.1080p", [(2, 1)]),  # single falls through
        ("Random.Movie.2021.1080p", []),
    ],
)
def test_parse_multi_episode(title, expected):
    assert parsing.parse_multi_episode(title) == expected


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Show.S03.1080p.COMPLETE", 3),
        ("Show Season 2 1080p", 2),
        ("Show.S03E04.1080p", None),  # single episode, not a pack
        ("Show 1x05", None),
        ("Show.2024.1080p.BluRay.DTS5.1.x264-GRP", None),  # audio tag, not season 5
        ("Movie.2024.1080p.WEB-DL-GROUPS3", None),  # group name, not season 3
    ],
)
def test_parse_season_pack(title, expected):
    assert parsing.parse_season_pack(title) == expected


@pytest.mark.parametrize(
    "title,expected",
    [
        ("x.2160p.y", "2160p"),
        ("x.1080p.y", "1080p"),
        ("x.720P.y", "720p"),
        ("x.4k.y", "2160p"),
        ("no res here", None),
    ],
)
def test_parse_resolution(title, expected):
    assert parsing.parse_resolution(title) == expected


def test_matches_episode_single():
    assert parsing.matches_episode("Show.S02E07.1080p", 2, 7) is True
    assert parsing.matches_episode("Show.S02E08.1080p", 2, 7) is False


def test_matches_episode_season_pack():
    assert parsing.matches_episode("Show.S02.Complete.1080p", 2, 7) is True
    assert parsing.matches_episode("Show.S03.Complete.1080p", 2, 7) is False


# --- absolute (anime) numbering -------------------------------------------- #
@pytest.mark.parametrize(
    "title,expected",
    [
        ("[FanSubA] Aethering - 12 (1080p) [ABCD1234].mkv", 12),
        ("[GroupB] Aetheria Gaiden - 28 [1080p]", 28),
        ("Aethering - 12v2 [1080p]", 12),
        ("[Group] Blade x Soul - 07 [720p]", 7),
        ("Lo-Fi! - 05 [BD]", 5),
        ("[SubsX] Show Name - 100 [1080p]", 100),
        ("Arena 100 - 08 [1080p]", 8),
        ("Show Episode 12 1080p", 12),
        ("Show E12 [1080p]", 12),
        # False positives that must NOT parse as an episode:
        ("[Group] Ironhold 1080p", None),
        ("[Group] Show Name (2023) 1080p", None),
        ("[Group] Show - 1080p", None),
        ("[Group] Steel Alchemy Saga [BD]", None),
    ],
)
def test_parse_absolute_episode(title, expected):
    assert parsing.parse_absolute_episode(title) == expected


@pytest.mark.parametrize(
    "title,expected",
    [
        ("[Group] Aethering (01-28) [1080p][Batch]", (1, 28)),
        ("[Group] Show 01~12 [BD]", (1, 12)),
        ("[Group] Show E01-E24", (1, 24)),
        ("[Group] Show - 12", None),  # single episode, not a range
        # "Season N-M" is a season span, NOT an episode range:
        ("[Archivist] Twin Star SS (Season 1-2 + OVAs) [BD]", None),
        ("[Group] Show Seasons 1-3 [1080p]", None),
    ],
)
def test_parse_absolute_range(title, expected):
    assert parsing.parse_absolute_range(title) == expected


def test_matches_episode_absolute():
    assert parsing.matches_episode_absolute("[FanSubA] Aethering - 12 (1080p)", 12) is True
    assert parsing.matches_episode_absolute("[FanSubA] Aethering - 12 (1080p)", 13) is False
    # batch/range covers the episode
    assert parsing.matches_episode_absolute("[Group] Aethering (01-28) [Batch]", 12) is True
    assert parsing.matches_episode_absolute("[Group] Aethering (01-28) [Batch]", 40) is False
    # bare batch matches any
    assert parsing.matches_episode_absolute("[Group] Aethering [Batch]", 5) is True
    # SxxExx still honoured as season 1
    assert parsing.matches_episode_absolute("Aethering S01E12", 12) is True
    assert parsing.matches_episode_absolute("Aethering S02E12", 12) is False


def test_title_matches_episode_dispatch():
    # Absolute release must NOT match under standard SxxExx rules...
    assert parsing.title_matches_episode("[Group] Show - 12", 1, 12, absolute=False) is False
    # ...but does when the series is flagged absolute.
    assert parsing.title_matches_episode("[Group] Show - 12", 1, 12, absolute=True) is True


@pytest.mark.parametrize(
    "res,rank",
    [("480p", 0), ("720p", 1), ("1080p", 2), ("2160p", 3), (None, -1), ("bogus", -1)],
)
def test_resolution_rank(res, rank):
    assert parsing.resolution_rank(res) == rank


def test_matches_single_episode_excludes_packs():
    # A single-episode release matches...
    assert parsing.matches_single_episode("Show.S01E03.1080p", 1, 3) is True
    # ...but a season pack must NOT (upgrades never replace a whole pack).
    assert parsing.matches_single_episode("Show.S01.1080p.COMPLETE", 1, 3) is False
    assert parsing.matches_single_episode("Show.Season.1.1080p", 1, 3) is False
    # Wrong episode.
    assert parsing.matches_single_episode("Show.S01E04.1080p", 1, 3) is False


def test_matches_single_episode_absolute():
    assert parsing.matches_single_episode("[Grp] Show - 12 [1080p]", 1, 12, absolute=True) is True
    # Batches/ranges are rejected for absolute upgrades too.
    assert parsing.matches_single_episode("[Grp] Show (01-24) [1080p]", 1, 12, absolute=True) is False
    assert parsing.matches_single_episode("[Grp] Show Batch [1080p]", 1, 12, absolute=True) is False
