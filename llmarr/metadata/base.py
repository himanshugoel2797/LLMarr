"""Abstract metadata provider interface + shared data shapes."""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel


class SeriesSearchResult(BaseModel):
    provider: str
    provider_id: str
    title: str
    year: int | None = None
    overview: str | None = None
    poster: str | None = None


class EpisodeInfo(BaseModel):
    season: int
    episode: int
    title: str | None = None
    air_date: str | None = None


class SeriesInfo(BaseModel):
    provider: str
    provider_id: str
    title: str
    year: int | None = None
    overview: str | None = None
    poster: str | None = None
    status: str | None = None
    seasons: list[int] = []
    episodes: list[EpisodeInfo] = []


class MovieSearchResult(BaseModel):
    provider: str
    provider_id: str
    title: str
    year: int | None = None
    overview: str | None = None
    poster: str | None = None


class MovieInfo(BaseModel):
    provider: str
    provider_id: str
    title: str
    year: int | None = None
    overview: str | None = None
    poster: str | None = None
    status: str | None = None


class MetadataProvider(ABC):
    name: str
    # True for sources whose entries use absolute episode numbering (anime), so
    # releases/files are matched by absolute number rather than SxxExx.
    absolute_numbering: bool = False

    @abstractmethod
    async def search_series(self, query: str) -> list[SeriesSearchResult]:
        ...

    @abstractmethod
    async def get_series(self, provider_id: str) -> SeriesInfo:
        """Full series info including the flat episode list across all seasons."""
        ...

    @abstractmethod
    async def search_movies(self, query: str) -> list[MovieSearchResult]:
        ...

    @abstractmethod
    async def get_movie(self, provider_id: str) -> MovieInfo:
        ...
