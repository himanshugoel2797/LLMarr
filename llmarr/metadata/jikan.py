"""Anime metadata via a Jikan-compatible MyAnimeList REST API.

Exposes MyAnimeList's catalogue with **no API key**, a drop-in anime-specialised
metadata source. The default base URL is Tenrai (``api.tenrai.org/v1``), a 1:1
mirror of Jikan v4 — the original Jikan (``api.jikan.moe/v4``) is being
discontinued, and Tenrai is API-compatible, so only the base URL changes. It is
configurable via ``metadata.anime_api_url``.

These MAL mirrors can return transient 5xx when MAL is slow, so requests are
retried with backoff. Jikan's documented rate limits (3 requests/second,
60/minute) are enforced globally by a shared limiter (:class:`_RateLimiter`)
across all requests and provider instances (Tenrai is more generous, but the
conservative caps keep any mirror happy).

Anime don't use Sonarr-style season/episode numbering — each MAL entry is one
cour/season with episodes numbered from 1 — so entries are modelled as a single
season (season 1) with absolute episode numbers, matching how anime is released.
Per-episode titles come from the /episodes endpoint.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

import httpx

from .base import (
    EpisodeInfo,
    MetadataProvider,
    MovieInfo,
    MovieSearchResult,
    SeriesInfo,
    SeriesSearchResult,
)

_DEFAULT_BASE = "https://api.tenrai.org/v1"  # Tenrai — Jikan v4-compatible mirror
_MAX_EPISODE_PAGES = 25  # safety cap (~2500 episodes)


class _RateLimiter:
    """Async sliding-window limiter shared by all Jikan requests.

    Jikan's documented limits are 3 requests/second and 60 requests/minute;
    exceeding them returns 429s (and can get you temporarily blocked). Because a
    fresh :class:`JikanProvider` is built per call, the limiter is a module-level
    singleton so the caps hold across every provider instance and concurrent
    operation on the event loop.
    """

    def __init__(self, per_second: int = 3, per_minute: int = 60):
        self.per_second = per_second
        self.per_minute = per_minute
        self._times: deque[float] = deque()
        self._lock: asyncio.Lock | None = None

    async def acquire(self) -> None:
        if self._lock is None:  # lazily bind to the running loop
            self._lock = asyncio.Lock()
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._times and now - self._times[0] >= 60:
                    self._times.popleft()
                if len(self._times) >= self.per_minute:
                    await asyncio.sleep(60 - (now - self._times[0]))
                    continue
                recent = [t for t in self._times if now - t < 1.0]
                if len(recent) >= self.per_second:
                    await asyncio.sleep(1.0 - (now - recent[0]) + 0.001)
                    continue
                self._times.append(now)
                return


class _NullRateLimiter:
    async def acquire(self) -> None:  # used to disable pacing in tests
        return


# Shared across the whole process. Tests swap in _NullRateLimiter.
_LIMITER: _RateLimiter | _NullRateLimiter = _RateLimiter(per_second=3, per_minute=60)


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

    def __init__(self, base_url: str | None = None, language: str | None = None):
        # No API key. `language` is accepted for interface parity but MAL titles
        # are returned in romaji/english regardless.
        self.base = (base_url or _DEFAULT_BASE).rstrip("/")
        self.language = language

    async def _get(self, client: httpx.AsyncClient, path: str, **params) -> dict:
        last_exc: Exception | None = None
        for attempt in range(4):
            await _LIMITER.acquire()  # respect Jikan's 3/s + 60/min limits
            try:
                resp = await client.get(f"{self.base}{path}", params=params)
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
            page += 1  # pacing between pages is handled by the shared rate limiter
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
