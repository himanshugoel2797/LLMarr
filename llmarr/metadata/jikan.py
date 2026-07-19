"""Anime metadata via Jikan — the unofficial MyAnimeList REST API.

Jikan (https://jikan.moe) exposes MyAnimeList's catalogue with **no API key**,
which makes it a drop-in anime-specialised metadata source. It proxies MAL, so it
can return transient 5xx when MAL is slow; requests are retried with backoff, and
its rate limit (~3 req/s) is respected when paging episodes.

Anime don't use Sonarr-style season/episode numbering — each MAL entry is one
cour/season with episodes numbered from 1 — so entries are modelled as a single
season (season 1) with absolute episode numbers, matching how anime is released.
Per-episode titles come from the /episodes endpoint.
"""

from __future__ import annotations

import asyncio

import httpx

from .base import (
    EpisodeInfo,
    MetadataProvider,
    MovieInfo,
    MovieSearchResult,
    SeriesInfo,
    SeriesSearchResult,
)

_BASE = "https://api.jikan.moe/v4"
_MAX_EPISODE_PAGES = 25  # safety cap (~2500 episodes)
_PAGE_DELAY = 0.5  # seconds between episode pages (rate-limit courtesy)


def _year(entry: dict) -> int | None:
    prop = ((entry.get("aired") or {}).get("prop") or {}).get("from") or {}
    y = prop.get("year")
    return int(y) if y else None


def _title(entry: dict) -> str:
    return entry.get("title_english") or entry.get("title") or entry.get("title_japanese") or "?"


def _poster(entry: dict) -> str | None:
    return ((entry.get("images") or {}).get("jpg") or {}).get("image_url")


class JikanProvider(MetadataProvider):
    name = "jikan"
    absolute_numbering = True

    def __init__(self, language: str | None = None):
        # No API key. `language` is accepted for interface parity but Jikan/MAL
        # titles are returned in romaji/english regardless.
        self.language = language

    async def _get(self, client: httpx.AsyncClient, path: str, **params) -> dict:
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                resp = await client.get(f"{_BASE}{path}", params=params)
            except httpx.HTTPError as exc:  # network hiccup
                last_exc = exc
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            # MAL/Jikan flakes with 5xx and rate-limits with 429 — retry those.
            if resp.status_code in (429, 500, 502, 503, 504):
                last_exc = httpx.HTTPStatusError(
                    f"Jikan {resp.status_code}", request=resp.request, response=resp
                )
                await asyncio.sleep(0.7 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(
            "Jikan/MyAnimeList is not responding (repeated upstream errors). "
            "This is usually transient — try again shortly."
        ) from last_exc

    async def _search(self, query: str, kind: str) -> list[dict]:
        async with httpx.AsyncClient(timeout=25) as client:
            data = await self._get(
                client, "/anime", q=query, limit=15, type=kind, order_by="members", sort="desc"
            )
        return data.get("data", [])

    async def search_series(self, query: str) -> list[SeriesSearchResult]:
        # ONA/OVA/special series are common in anime; include the main TV type
        # plus everything by not over-filtering — MAL relevance ordering handles it.
        results = await self._search(query, kind="tv")
        return [
            SeriesSearchResult(
                provider="jikan",
                provider_id=str(a["mal_id"]),
                title=_title(a),
                year=_year(a),
                overview=a.get("synopsis"),
                poster=_poster(a),
            )
            for a in results
        ]

    async def _episodes(self, client: httpx.AsyncClient, mal_id: str) -> list[EpisodeInfo]:
        episodes: list[EpisodeInfo] = []
        page = 1
        while page <= _MAX_EPISODE_PAGES:
            data = await self._get(client, f"/anime/{mal_id}/episodes", page=page)
            for ep in data.get("data", []):
                num = ep.get("mal_id")  # episode number within the entry
                if num is None:
                    continue
                aired = ep.get("aired")
                episodes.append(
                    EpisodeInfo(
                        season=1,
                        episode=int(num),
                        title=ep.get("title"),
                        air_date=aired[:10] if isinstance(aired, str) else None,
                    )
                )
            if not (data.get("pagination") or {}).get("has_next_page"):
                break
            page += 1
            await asyncio.sleep(_PAGE_DELAY)
        return episodes

    async def get_series(self, provider_id: str) -> SeriesInfo:
        async with httpx.AsyncClient(timeout=25) as client:
            detail = (await self._get(client, f"/anime/{provider_id}")).get("data", {})
            episodes = await self._episodes(client, provider_id)

        # Fall back to a synthetic 1..N list if the episodes endpoint is empty
        # (some currently-airing shows) but a total count is known.
        if not episodes and detail.get("episodes"):
            episodes = [EpisodeInfo(season=1, episode=n) for n in range(1, int(detail["episodes"]) + 1)]

        return SeriesInfo(
            provider="jikan",
            provider_id=str(provider_id),
            title=_title(detail),
            year=_year(detail),
            overview=detail.get("synopsis"),
            poster=_poster(detail),
            status=detail.get("status"),
            seasons=[1] if episodes else [],
            episodes=episodes,
        )

    async def search_movies(self, query: str) -> list[MovieSearchResult]:
        results = await self._search(query, kind="movie")
        return [
            MovieSearchResult(
                provider="jikan",
                provider_id=str(a["mal_id"]),
                title=_title(a),
                year=_year(a),
                overview=a.get("synopsis"),
                poster=_poster(a),
            )
            for a in results
        ]

    async def get_movie(self, provider_id: str) -> MovieInfo:
        async with httpx.AsyncClient(timeout=25) as client:
            detail = (await self._get(client, f"/anime/{provider_id}")).get("data", {})
        return MovieInfo(
            provider="jikan",
            provider_id=str(provider_id),
            title=_title(detail),
            year=_year(detail),
            overview=detail.get("synopsis"),
            poster=_poster(detail),
            status=detail.get("status"),
        )
