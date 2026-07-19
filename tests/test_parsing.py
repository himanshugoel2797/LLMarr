import pytest

from llmarr import parsing


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Severance.S02E01.1080p.WEB-DL.x265", (2, 1)),
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
        ("[SubsPlease] Frieren - 12 (1080p) [ABCD1234].mkv", 12),
        ("[Erai-raws] Sousou no Frieren - 28 [1080p]", 28),
        ("Frieren - 12v2 [1080p]", 12),
        ("[Group] Spy x Family - 07 [720p]", 7),
        ("K-On! - 05 [BD]", 5),
        ("[HorribleSubs] Show Name - 100 [1080p]", 100),
        ("Mob Psycho 100 - 08 [1080p]", 8),
        ("Show Episode 12 1080p", 12),
        ("Show E12 [1080p]", 12),
        # False positives that must NOT parse as an episode:
        ("[Group] Attack on Titan 1080p", None),
        ("[Group] Show Name (2023) 1080p", None),
        ("[Group] Show - 1080p", None),
        ("[Group] Fullmetal Alchemist Brotherhood [BD]", None),
    ],
)
def test_parse_absolute_episode(title, expected):
    assert parsing.parse_absolute_episode(title) == expected


@pytest.mark.parametrize(
    "title,expected",
    [
        ("[Group] Frieren (01-28) [1080p][Batch]", (1, 28)),
        ("[Group] Show 01~12 [BD]", (1, 12)),
        ("[Group] Show E01-E24", (1, 24)),
        ("[Group] Show - 12", None),  # single episode, not a range
        # "Season N-M" is a season span, NOT an episode range:
        ("[Tenrai-Sensei] Amagami SS (Season 1-2 + OVAs) [BD]", None),
        ("[Group] Show Seasons 1-3 [1080p]", None),
    ],
)
def test_parse_absolute_range(title, expected):
    assert parsing.parse_absolute_range(title) == expected


def test_matches_episode_absolute():
    assert parsing.matches_episode_absolute("[SubsPlease] Frieren - 12 (1080p)", 12) is True
    assert parsing.matches_episode_absolute("[SubsPlease] Frieren - 12 (1080p)", 13) is False
    # batch/range covers the episode
    assert parsing.matches_episode_absolute("[Group] Frieren (01-28) [Batch]", 12) is True
    assert parsing.matches_episode_absolute("[Group] Frieren (01-28) [Batch]", 40) is False
    # bare batch matches any
    assert parsing.matches_episode_absolute("[Group] Frieren [Batch]", 5) is True
    # SxxExx still honoured as season 1
    assert parsing.matches_episode_absolute("Frieren S01E12", 12) is True
    assert parsing.matches_episode_absolute("Frieren S02E12", 12) is False


def test_title_matches_episode_dispatch():
    # Absolute release must NOT match under standard SxxExx rules...
    assert parsing.title_matches_episode("[Group] Show - 12", 1, 12, absolute=False) is False
    # ...but does when the series is flagged absolute.
    assert parsing.title_matches_episode("[Group] Show - 12", 1, 12, absolute=True) is True
