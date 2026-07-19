"""Tests for importing an existing Plex library into LLMarr."""

import pytest

from tests.conftest import FakePlex

CATALOG = [
    {"type": "show", "section": "Anime", "title": "Aethering", "year": 2023,
     "rating_key": "101", "guids": {"tvdb": "424536", "anidb": "17517"}},
    {"type": "show", "section": "Anime", "title": "Blade x Soul", "year": 2022,
     "rating_key": "102", "guids": {"tmdb": "120089"}},
    {"type": "movie", "section": "Movies", "title": "Nebula", "year": 2021,
     "rating_key": "201", "guids": {"tmdb": "438631", "imdb": "tt1160419"}},
    {"type": "movie", "section": "AV", "title": "Private Thing", "year": 2020,
     "rating_key": "301", "guids": {}},
]


@pytest.fixture
def plex_app(app, monkeypatch):
    # The "Anime" section is the anime library; imports from it get absolute numbering.
    app.store.mutate(lambda c: setattr(c.plex, "anime_section", "Anime"))
    monkeypatch.setattr(app, "plex", lambda: FakePlex(catalog_items=CATALOG))
    return app


async def test_dry_run_previews_without_writing(plex_app):
    res = await plex_app.import_from_plex(dry_run=True)
    assert res["dry_run"] is True
    assert res["scanned"] == 4 and res["matched"] == 4
    assert res["with_tmdb_id"] == 2  # Blade x Soul + Nebula
    assert res["registered"] is None
    assert res["sections_available"] == {"Anime": 2, "Movies": 1, "AV": 1}
    assert plex_app.db.list_series() == []  # nothing written
    assert plex_app.db.list_movies() == []


async def test_sections_filter_excludes_unwanted(plex_app):
    res = await plex_app.import_from_plex(dry_run=False, sections=["Anime", "Movies"])
    assert res["matched"] == 3  # AV excluded
    assert res["registered"] == {"series": 2, "movies": 1, "skipped": 0}
    titles = {m["title"] for m in plex_app.db.list_movies()}
    assert "Private Thing" not in titles and "Nebula" in titles


async def test_real_import_registers_items(plex_app):
    res = await plex_app.import_from_plex(dry_run=False, monitored=False)
    assert res["registered"] == {"series": 2, "movies": 2, "skipped": 0}  # incl AV

    series = {s["title"]: s for s in plex_app.db.list_series()}
    # tmdb id used when Plex has one; else falls back to a plex rating key
    assert series["Blade x Soul"]["provider"] == "tmdb"
    assert series["Blade x Soul"]["provider_id"] == "120089"
    assert series["Aethering"]["provider"] == "plex"
    assert series["Aethering"]["provider_id"] == "101"
    # anime section -> absolute numbering flag set
    assert series["Aethering"]["absolute_numbering"] == 1
    assert series["Blade x Soul"]["absolute_numbering"] == 1

    movies = plex_app.db.list_movies()
    assert movies[0]["title"] == "Nebula"
    assert movies[0]["provider"] == "tmdb" and movies[0]["provider_id"] == "438631"
    assert movies[0]["movie_status"] == "downloaded"  # already owned


async def test_media_type_filter(plex_app):
    res = await plex_app.import_from_plex(dry_run=False, media_type="movie")
    assert res["registered"]["movies"] == 2 and res["registered"]["series"] == 0
    assert plex_app.db.list_series() == []


async def test_import_marks_existing_missing_movie_downloaded(plex_app):
    # A movie previously added as missing, then found in Plex, must flip to owned.
    plex_app.db.upsert_movie(provider="tmdb", provider_id="438631", title="Nebula",
                             year=2021, monitored=1, movie_status="missing")
    await plex_app.import_from_plex(dry_run=False, sections=["Movies"])
    m = [x for x in plex_app.db.list_movies() if x["provider_id"] == "438631"][0]
    assert m["movie_status"] == "downloaded"


async def test_import_stores_plex_identity(plex_app):
    await plex_app.import_from_plex(dry_run=False)
    series = {s["title"]: s for s in plex_app.db.list_series()}
    assert series["Aethering"]["plex_rating_key"] == "101"
    assert series["Aethering"]["plex_section"] == "Anime"
    # Even tmdb-keyed series keep their Plex handle for later activation.
    assert series["Blade x Soul"]["plex_rating_key"] == "102"
    assert series["Blade x Soul"]["plex_section"] == "Anime"


async def test_reimport_is_idempotent(plex_app):
    await plex_app.import_from_plex(dry_run=False)
    await plex_app.import_from_plex(dry_run=False)
    assert len(plex_app.db.list_series()) == 2  # no duplicates
    assert len(plex_app.db.list_movies()) == 2
