"""Metadata providers (series/episode info)."""

from .base import (
    EpisodeInfo,
    MetadataProvider,
    MovieInfo,
    MovieSearchResult,
    SeriesInfo,
    SeriesSearchResult,
)
from .jikan import JikanProvider
from .tmdb import TMDBProvider


def get_provider(config, name: str | None = None) -> MetadataProvider:
    """Instantiate a metadata provider. ``name`` overrides the configured default
    (e.g. ``"jikan"`` for a single anime lookup while the default stays TMDB)."""
    meta = config.metadata
    provider = name or meta.provider
    if provider == "tmdb":
        return TMDBProvider(api_key=meta.tmdb_api_key, language=meta.language)
    if provider == "jikan":
        return JikanProvider(base_url=meta.anime_api_url, language=meta.language)
    raise ValueError(f"Unknown metadata provider: {provider}")


__all__ = [
    "MetadataProvider",
    "SeriesSearchResult",
    "SeriesInfo",
    "EpisodeInfo",
    "MovieSearchResult",
    "MovieInfo",
    "TMDBProvider",
    "JikanProvider",
    "get_provider",
]
