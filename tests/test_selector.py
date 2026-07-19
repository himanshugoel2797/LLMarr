from llmarr import selector
from llmarr.config import QualityConfig
from llmarr.indexers.prowlarr import Release


def rel(title, seeders=100, size=1_000_000_000):
    return Release(guid=title, title=title, seeders=seeders, size=size)


def test_ignored_terms_rejected():
    q = QualityConfig(ignored_terms=["cam"])
    ok, reason = selector.passes(rel("Show.S01E01.CAM"), q)
    assert not ok and "cam" in reason


def test_ignored_terms_match_whole_word_only():
    q = QualityConfig()  # default ignored includes "cam", "ts"
    # These contain "ts"/"cam" as substrings but must NOT be rejected.
    assert selector.passes(rel("Catskill.S01E01.1080p.WEB-DL"), q)[0]
    assert selector.passes(rel("Camden.Row.2022.1080p.BluRay"), q)[0]
    assert selector.passes(rel("Botsford.CA.S02E03.720p.HDTV"), q)[0]
    # But standalone CAM/TS tags are still rejected.
    assert not selector.passes(rel("Camden.Row.2022.CAM.x264"), q)[0]
    assert not selector.passes(rel("Nebula.2024.TS.720p"), q)[0]


def test_required_terms_enforced():
    q = QualityConfig(required_terms=["x265"])
    assert not selector.passes(rel("Show.720p.x264"), q)[0]
    assert selector.passes(rel("Show.720p.x265"), q)[0]


def test_min_seeders():
    q = QualityConfig(min_seeders=10)
    assert not selector.passes(rel("Show", seeders=5), q)[0]
    assert selector.passes(rel("Show", seeders=10), q)[0]


def test_size_bounds():
    q = QualityConfig(min_size_mb=500, max_size_mb=2000)
    assert not selector.passes(rel("small", size=100 * 1024 * 1024), q)[0]
    assert not selector.passes(rel("big", size=5000 * 1024 * 1024), q)[0]
    assert selector.passes(rel("ok", size=1000 * 1024 * 1024), q)[0]


def test_resolution_preference_ranks_first():
    q = QualityConfig(preferred_resolutions=["1080p", "720p"], min_seeders=1)
    rels = [
        rel("Show.S01E01.720p.WEBRip", seeders=50),
        rel("Show.S01E01.1080p.WEB-DL", seeders=10),
    ]
    ranked = selector.rank(rels, q)
    assert ranked[0][0].title.startswith("Show.S01E01.1080p")


def test_prefer_terms_add_score():
    q = QualityConfig(preferred_resolutions=["1080p"], prefer_terms=["x265"], min_seeders=1)
    a = rel("Show.1080p.x265", seeders=10)
    b = rel("Show.1080p.x264", seeders=10)
    assert selector.score(a, q) > selector.score(b, q)


def test_best_filters_then_picks():
    q = QualityConfig(preferred_resolutions=["1080p", "720p"], ignored_terms=["cam"], min_seeders=5)
    rels = [
        rel("Show.S01E01.CAM", seeders=999),
        rel("Show.S01E01.720p", seeders=50),
        rel("Show.S01E01.1080p", seeders=2),   # below seeder floor
        rel("Show.S01E01.1080p.WEB", seeders=40),
    ]
    best = selector.best(rels, q)
    assert best.title == "Show.S01E01.1080p.WEB"


def test_rank_empty_when_all_filtered():
    q = QualityConfig(min_seeders=1000)
    assert selector.rank([rel("Show", seeders=10)], q) == []
    assert selector.best([rel("Show", seeders=10)], q) is None


# --- quality upgrades (G4) ------------------------------------------------- #
def test_is_upgrade_disabled_without_cutoff():
    q = QualityConfig()  # upgrade_until unset
    assert not selector.is_upgrade(rel("Show.S01E01.1080p"), "720p", q)


def test_is_upgrade_strictly_better_within_cutoff():
    q = QualityConfig(upgrade_until="1080p", min_seeders=1)
    # 720p -> 1080p is a valid upgrade.
    assert selector.is_upgrade(rel("Show.S01E01.1080p.WEB"), "720p", q)
    # Same resolution is not an upgrade.
    assert not selector.is_upgrade(rel("Show.S01E01.720p.WEB"), "720p", q)
    # Lower resolution is not an upgrade.
    assert not selector.is_upgrade(rel("Show.S01E01.480p"), "720p", q)


def test_is_upgrade_does_not_exceed_cutoff():
    q = QualityConfig(upgrade_until="1080p", min_seeders=1)
    # 2160p is above the cutoff — don't chase it even from 720p.
    assert not selector.is_upgrade(rel("Show.S01E01.2160p"), "720p", q)


def test_is_upgrade_respects_hard_constraints():
    q = QualityConfig(upgrade_until="1080p", ignored_terms=["cam"], min_seeders=5)
    assert not selector.is_upgrade(rel("Show.S01E01.1080p.CAM"), "720p", q)
    assert not selector.is_upgrade(rel("Show.S01E01.1080p", seeders=2), "720p", q)


def test_is_upgrade_unknown_resolution_never_upgrades():
    q = QualityConfig(upgrade_until="1080p", min_seeders=1)
    assert not selector.is_upgrade(rel("Show.S01E01.WEB-DL"), "720p", q)


def test_best_upgrade_picks_highest_within_cutoff():
    q = QualityConfig(upgrade_until="1080p", min_seeders=1)
    rels = [
        rel("Show.S01E01.720p"),       # not an upgrade over 720p
        rel("Show.S01E01.1080p.WEB"),  # valid
        rel("Show.S01E01.2160p"),      # above cutoff, excluded
    ]
    pick = selector.best_upgrade(rels, "720p", q)
    assert pick is not None and "1080p" in pick.title


def test_best_upgrade_none_when_current_at_cutoff():
    q = QualityConfig(upgrade_until="1080p", min_seeders=1)
    assert selector.best_upgrade([rel("Show.S01E01.2160p")], "1080p", q) is None
