"""Metadata providers (series/episode info)."""

from .base import (
    EpisodeInfo,
    MetadataProvider,
    MovieInfo,
    MovieSearchResult,
    SeriesInfo,
    SeriesSearchResult,
)
from .tmdb import TMDBProvider


def get_provider(config) -> MetadataProvider:
    """Instantiate the configured metadata provider."""
    meta = config.metadata
    if meta.provider == "tmdb":
        return TMDBProvider(api_key=meta.tmdb_api_key, language=meta.language)
    raise ValueError(f"Unknown metadata provider: {meta.provider}")


__all__ = [
    "MetadataProvider",
    "SeriesSearchResult",
    "SeriesInfo",
    "EpisodeInfo",
    "MovieSearchResult",
    "MovieInfo",
    "TMDBProvider",
    "get_provider",
]
