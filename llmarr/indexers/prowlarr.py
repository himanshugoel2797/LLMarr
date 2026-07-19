"""Prowlarr search + RSS client.

Prowlarr aggregates every configured indexer behind one API, so LLMarr never
needs to speak to trackers directly. We use the v1 ``/search`` endpoint for
on-demand queries and per-indexer ``/api/v1/indexer/{id}/newznab`` RSS is avoided
in favour of a periodic search (simpler and works across all indexers).
"""

from __future__ import annotations

from typing import Optional

import httpx
from pydantic import BaseModel

# Torznab/newznab categories.
CAT_TV = 5000
CAT_MOVIE = 2000


class Release(BaseModel):
    guid: str
    title: str
    indexer: Optional[str] = None
    indexer_id: Optional[int] = None
    size: int = 0
    seeders: Optional[int] = None
    leechers: Optional[int] = None
    download_url: Optional[str] = None
    magnet_url: Optional[str] = None
    info_url: Optional[str] = None
    protocol: str = "torrent"
    categories: list[int] = []

    @property
    def grab_url(self) -> Optional[str]:
        """The URL to hand to the download client (magnet preferred)."""
        return self.magnet_url or self.download_url

    @property
    def size_mb(self) -> float:
        return self.size / (1024 * 1024) if self.size else 0.0


class ProwlarrClient:
    def __init__(self, url: str, api_key: str, indexer_ids: Optional[list[int]] = None):
        if not url or not api_key:
            raise ValueError("Prowlarr URL and API key must be configured.")
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.indexer_ids = indexer_ids or []

    def _headers(self) -> dict:
        return {"X-Api-Key": self.api_key}

    @staticmethod
    def _parse(item: dict) -> Release:
        cats = []
        for c in item.get("categories", []) or []:
            cid = c.get("id") if isinstance(c, dict) else c
            if cid is not None:
                cats.append(int(cid))
        return Release(
            guid=str(item.get("guid") or item.get("downloadUrl") or item.get("title")),
            title=item.get("title", "?"),
            indexer=item.get("indexer"),
            indexer_id=item.get("indexerId"),
            size=int(item.get("size") or 0),
            seeders=item.get("seeders"),
            leechers=item.get("leechers"),
            download_url=item.get("downloadUrl"),
            magnet_url=item.get("magnetUrl"),
            info_url=item.get("infoUrl"),
            protocol=item.get("protocol", "torrent"),
            categories=cats,
        )

    async def search(
        self,
        query: str,
        categories: Optional[list[int]] = None,
        indexer_ids: Optional[list[int]] = None,
        limit: int = 100,
    ) -> list[Release]:
        params: list[tuple[str, str]] = [("query", query), ("type", "search")]
        for cid in categories or []:
            params.append(("categories", str(cid)))
        for iid in indexer_ids or self.indexer_ids:
            params.append(("indexerIds", str(iid)))
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(
                f"{self.url}/api/v1/search", params=params, headers=self._headers()
            )
            resp.raise_for_status()
            data = resp.json()
        releases = [self._parse(i) for i in data]
        # Prowlarr already sorts, but ensure torrents only and cap.
        releases = [r for r in releases if r.protocol == "torrent"]
        return releases[:limit]

    async def test(self) -> dict:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{self.url}/api/v1/indexer", headers=self._headers()
            )
            resp.raise_for_status()
            indexers = resp.json()
        return {
            "ok": True,
            "indexer_count": len(indexers),
            "indexers": [
                {"id": i.get("id"), "name": i.get("name"), "enable": i.get("enable")}
                for i in indexers
            ],
        }
