"""The Movie Database (TMDB) metadata provider.

Uses the v3 REST API. A free API key is required; set it via
``configure_metadata``. Only TV series are implemented for the first cut, but the
same client can be extended to movies.
"""

from __future__ import annotations

import httpx

from .base import (
    EpisodeInfo,
    MetadataProvider,
    MovieInfo,
    MovieSearchResult,
    SeriesInfo,
    SeriesSearchResult,
)

_BASE = "https://api.themoviedb.org/3"
_IMG = "https://image.tmdb.org/t/p/w500"


class TMDBProvider(MetadataProvider):
    name = "tmdb"

    def __init__(self, api_key: str | None, language: str = "en-US"):
        if not api_key:
            raise ValueError(
                "TMDB API key is not configured. Set it with configure_metadata."
            )
        self.api_key = api_key
        self.language = language

    def _params(self, **extra) -> dict:
        return {"api_key": self.api_key, "language": self.language, **extra}

    async def _get(self, client: httpx.AsyncClient, path: str, **params) -> dict:
        resp = await client.get(f"{_BASE}{path}", params=self._params(**params))
        resp.raise_for_status()
        return resp.json()

    async def search_series(self, query: str) -> list[SeriesSearchResult]:
        async with httpx.AsyncClient(timeout=20) as client:
            data = await self._get(client, "/search/tv", query=query)
        results = []
        for item in data.get("results", []):
            date = item.get("first_air_date") or ""
            year = int(date[:4]) if date[:4].isdigit() else None
            results.append(
                SeriesSearchResult(
                    provider="tmdb",
                    provider_id=str(item["id"]),
                    title=item.get("name") or item.get("original_name") or "?",
                    year=year,
                    overview=item.get("overview"),
                    poster=(_IMG + item["poster_path"]) if item.get("poster_path") else None,
                )
            )
        return results

    async def get_series(self, provider_id: str) -> SeriesInfo:
        async with httpx.AsyncClient(timeout=30) as client:
            show = await self._get(client, f"/tv/{provider_id}")
            date = show.get("first_air_date") or ""
            year = int(date[:4]) if date[:4].isdigit() else None
            # Season 0 is specials/OVAs — include it (callers monitor it or not);
            # regular seasons come after so the flat list stays ordered.
            season_numbers = sorted(
                s["season_number"]
                for s in show.get("seasons", [])
                if s.get("season_number") is not None
            )

            episodes: list[EpisodeInfo] = []
            for season_number in season_numbers:
                try:
                    season = await self._get(
                        client, f"/tv/{provider_id}/season/{season_number}"
                    )
                except httpx.HTTPError:
                    continue  # a missing season shouldn't sink the whole fetch
                for ep in season.get("episodes", []):
                    if ep.get("episode_number") is None:
                        continue
                    episodes.append(
                        EpisodeInfo(
                            season=season_number,
                            episode=ep.get("episode_number"),
                            title=ep.get("name"),
                            air_date=ep.get("air_date"),
                        )
                    )

        return SeriesInfo(
            provider="tmdb",
            provider_id=str(provider_id),
            title=show.get("name") or show.get("original_name") or "?",
            year=year,
            overview=show.get("overview"),
            poster=(_IMG + show["poster_path"]) if show.get("poster_path") else None,
            status=show.get("status"),
            seasons=season_numbers,
            episodes=episodes,
        )

    async def search_movies(self, query: str) -> list[MovieSearchResult]:
        async with httpx.AsyncClient(timeout=20) as client:
            data = await self._get(client, "/search/movie", query=query)
        results = []
        for item in data.get("results", []):
            date = item.get("release_date") or ""
            year = int(date[:4]) if date[:4].isdigit() else None
            results.append(
                MovieSearchResult(
                    provider="tmdb",
                    provider_id=str(item["id"]),
                    title=item.get("title") or item.get("original_title") or "?",
                    year=year,
                    overview=item.get("overview"),
                    poster=(_IMG + item["poster_path"]) if item.get("poster_path") else None,
                )
            )
        return results

    async def get_movie(self, provider_id: str) -> MovieInfo:
        async with httpx.AsyncClient(timeout=20) as client:
            movie = await self._get(client, f"/movie/{provider_id}")
        date = movie.get("release_date") or ""
        year = int(date[:4]) if date[:4].isdigit() else None
        return MovieInfo(
            provider="tmdb",
            provider_id=str(provider_id),
            title=movie.get("title") or movie.get("original_title") or "?",
            year=year,
            overview=movie.get("overview"),
            poster=(_IMG + movie["poster_path"]) if movie.get("poster_path") else None,
            status=movie.get("status"),
        )
