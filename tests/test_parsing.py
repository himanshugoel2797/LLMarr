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
