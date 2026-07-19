"""Application core — high-level operations shared by the MCP tools and the RSS
poller. Holds the config store and database and knows how to wire the individual
services (metadata, indexer, download client, Plex) together for the common
flows: add a series, search releases, grab a release, track downloads, import.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from typing import Optional

from . import pathmap, selector
from .config import ConfigStore, DownloadClientConfig, RootFolder
from .db import Database
from .download import get_client
from .download.qbittorrent import magnet_hash
from .importer import Importer
from .indexers.prowlarr import CAT_MOVIE, CAT_TV, ProwlarrClient, Release
from .metadata import get_provider
from .notify.plex import PlexNotifier
from .parsing import (
    is_batch,
    parse_absolute_episode,
    parse_absolute_range,
    parse_episode,
    parse_resolution,
    parse_season_pack,
    title_matches_episode,
)


def _folder_name(title: str, year: Optional[int]) -> str:
    safe = re.sub(r"[\\/:*?\"<>|]+", "", title).strip()
    return f"{safe} ({year})" if year else safe


class App:
    def __init__(self, config_store: ConfigStore, db: Database):
        self.store = config_store
        self.db = db
        self.importer = Importer(self)

    @property
    def config(self):
        return self.store.config

    # -- service factories -------------------------------------------------- #
    def provider(self, name: Optional[str] = None):
        return get_provider(self.config, name)

    def prowlarr(self) -> ProwlarrClient:
        p = self.config.prowlarr
        return ProwlarrClient(p.url, p.api_key, p.indexer_ids)

    def _client_config(self, name: Optional[str]) -> tuple[str, DownloadClientConfig]:
        name = name or self.config.default_download_client
        if not name:
            if len(self.config.download_clients) == 1:
                name = next(iter(self.config.download_clients))
            else:
                raise ValueError(
                    "No download client specified and no default set. "
                    "Configure one with configure_download_client."
                )
        cfg = self.config.download_clients.get(name)
        if not cfg:
            raise ValueError(f"Unknown download client: {name}")
        return name, cfg

    def download_client(self, name: Optional[str] = None):
        _, cfg = self._client_config(name)
        return get_client(cfg)

    def plex(self) -> PlexNotifier:
        return PlexNotifier(self.config.plex)

    def root_folder(self, name: Optional[str], media_type: str = "tv") -> Optional[RootFolder]:
        for rf in self.config.root_folders:
            if name and rf.name == name:
                return rf
        for rf in self.config.root_folders:
            if rf.media_type == media_type:
                return rf
        return None

    # -- series/library ----------------------------------------------------- #
    async def add_series(
        self,
        provider_id: str,
        monitored: bool = True,
        root_folder: Optional[str] = None,
        seasons: Optional[list[int]] = None,
        provider: Optional[str] = None,
    ) -> dict:
        prov = self.provider(provider)
        info = await prov.get_series(provider_id)
        folder = _folder_name(info.title, info.year)
        series_id = self.db.upsert_series(
            provider=info.provider,
            provider_id=info.provider_id,
            title=info.title,
            year=info.year,
            overview=info.overview,
            status=info.status,
            poster=info.poster,
            monitored=1 if monitored else 0,
            root_folder=root_folder,
            folder_name=folder,
            absolute_numbering=1 if getattr(prov, "absolute_numbering", False) else 0,
        )
        # Episodes present before this refresh — their monitored flags (the user's
        # per-season choices) must be preserved; only newly-added episodes get the
        # monitoring rule applied.
        existing = {(e["season"], e["episode"]) for e in self.db.list_episodes(series_id)}
        for ep in info.episodes:
            self.db.upsert_episode(
                series_id, ep.season, ep.episode, title=ep.title, air_date=ep.air_date
            )
            if (ep.season, ep.episode) in existing:
                continue
            ep_monitored = monitored and (seasons is None or ep.season in seasons)
            row = self.db.query_one(
                "SELECT id FROM episodes WHERE series_id=? AND season=? AND episode=?",
                (series_id, ep.season, ep.episode),
            )
            self.db.execute(
                "UPDATE episodes SET monitored=? WHERE id=?",
                (1 if ep_monitored else 0, row["id"]),
            )
        return self.db.get_series(series_id) | {
            "episode_count": len(info.episodes)
        }

    # -- release search ----------------------------------------------------- #
    async def search_releases(
        self,
        query: str,
        categories: Optional[list[int]] = None,
        indexer_ids: Optional[list[int]] = None,
        apply_quality: bool = True,
    ) -> list[dict]:
        releases = await self.prowlarr().search(
            query, categories=categories or [CAT_TV], indexer_ids=indexer_ids
        )
        if apply_quality:
            ranked = selector.rank(releases, self.config.quality)
            return [self._release_dict(r, sc) for r, sc, _ in ranked]
        return [self._release_dict(r, selector.score(r, self.config.quality)) for r in releases]

    def _release_dict(self, r: Release, score: float) -> dict:
        return {
            "guid": r.guid,
            "title": r.title,
            "indexer": r.indexer,
            "size_mb": round(r.size_mb, 1),
            "seeders": r.seeders,
            "resolution": parse_resolution(r.title),
            "score": round(score, 1),
            "grab_url": r.grab_url,
            "info_url": r.info_url,
        }

    # -- pack coverage ------------------------------------------------------ #
    def _covered_episode_ids(self, series: dict, title: str) -> list[int]:
        """Episode ids a release/pack covers, inferred from its title. Kept
        conservative so we never mark an episode grabbed that the pack lacks:
        single episodes, whole-season packs, and anime ranges/batches only.
        Ambiguous titles return only what matches exactly."""
        eps = self.db.list_episodes(series["id"])
        absolute = bool(series.get("absolute_numbering"))

        def ids(pred) -> list[int]:
            return [e["id"] for e in eps if pred(e)]

        if absolute:
            rng = parse_absolute_range(title)
            if rng:
                return ids(lambda e: rng[0] <= e["episode"] <= rng[1])
            if is_batch(title):
                return ids(lambda e: True)  # anime entry = one season; batch = all
            n = parse_absolute_episode(title)
            if n is not None:
                return ids(lambda e: e["season"] == 1 and e["episode"] == n)
            se = parse_episode(title)
            return ids(lambda e: (e["season"], e["episode"]) == se) if se else []

        se = parse_episode(title)
        if se:
            return ids(lambda e: (e["season"], e["episode"]) == se)
        pack = parse_season_pack(title)
        if pack is not None:
            return ids(lambda e: e["season"] == pack)
        return []

    def _mark_covered_grabbed(self, series_id: int, title: str, always: list[int]) -> list[int]:
        """Mark every episode a grab covers as 'grabbed' so the RSS poller won't
        redundantly grab singles while a pack downloads. Returns the episode ids."""
        covered = set(always)
        series = self.db.get_series(series_id) if series_id else None
        if series:
            covered.update(self._covered_episode_ids(series, title))
        for eid in covered:
            self.db.set_episode_status(eid, "grabbed")
        return sorted(covered)

    # -- grabbing ----------------------------------------------------------- #
    async def grab(
        self,
        grab_url: str,
        title: str = "manual grab",
        series_id: Optional[int] = None,
        episode_id: Optional[int] = None,
        movie_id: Optional[int] = None,
        client_name: Optional[str] = None,
        category: Optional[str] = None,
        save_path: Optional[str] = None,
        indexer: Optional[str] = None,
        size: Optional[int] = None,
        guid: Optional[str] = None,
    ) -> dict:
        name, cfg = self._client_config(client_name)
        client = get_client(cfg)
        category = category or cfg.category

        torrent_hash = await asyncio.to_thread(
            client.add, grab_url, category, save_path
        )
        if not torrent_hash:
            torrent_hash = magnet_hash(grab_url)

        download_id = self.db.add_download(
            series_id=series_id,
            episode_id=episode_id,
            movie_id=movie_id,
            title=title,
            indexer=indexer,
            download_url=grab_url,
            torrent_hash=torrent_hash,
            client=name,
            category=category,
            save_path=save_path or cfg.save_path,
            quality=parse_resolution(title),
            size=size,
            status="grabbed",
        )
        covered: list[int] = []
        if series_id:
            # Mark the linked episode plus every other episode this pack covers.
            covered = self._mark_covered_grabbed(
                series_id, title, always=[episode_id] if episode_id else []
            )
        elif episode_id:
            self.db.set_episode_status(episode_id, "grabbed")
        if movie_id:
            self.db.set_movie_status(movie_id, "grabbed")
        if guid:
            self.db.record_guid(guid)
        return {
            "download_id": download_id,
            "torrent_hash": torrent_hash,
            "client": name,
            "category": category,
            "covered_episodes": len(covered),
        }

    # -- import / progress -------------------------------------------------- #
    async def refresh_downloads(self, notify: bool = True) -> list[dict]:
        """Poll active grabs, mark completed ones and trigger a Plex scan."""
        updates = []
        pending = [
            d
            for d in self.db.list_downloads()
            if d["status"] in ("grabbed", "downloading") and d["torrent_hash"]
        ]
        for d in pending:
            client = self.download_client(d["client"])
            st = await asyncio.to_thread(client.status, d["torrent_hash"])
            if st is None:
                continue
            if st.completed:
                content = st.content_path or st.save_path
                self.db.set_download_status(d["id"], "completed", save_path=content)
                result = {"download_id": d["id"], "state": "completed", "notified": False}
                section = (
                    self.config.plex.movie_section
                    if d.get("movie_id")
                    else self.config.plex.tv_section
                )
                await self._import_and_notify(d, content, section, result, notify)
                updates.append(result)
            elif st.progress > 0:
                self.db.set_download_status(d["id"], "downloading")
                updates.append(
                    {"download_id": d["id"], "state": "downloading", "progress": st.progress}
                )
        return updates

    async def _import_and_notify(
        self, download: dict, content_path, section: str, result: dict, notify: bool
    ) -> None:
        """Hardlink/copy/move the completed download into the library (if import
        is enabled), then scan Plex on the resulting library folder(s). Falls back
        to scanning the raw download directory when import is off or not possible."""
        imp = self.config.importer
        scan_targets: list[str] = []  # paths in the *importer work* context

        if imp.enabled:
            ir = await asyncio.to_thread(self.importer.import_download, download, content_path)
            result["import"] = ir.model_dump()
            if ir.imported:
                self.db.set_download_status(
                    download["id"], "imported", save_path=ir.imported[0].destination
                )
            scan_targets = ir.scan_paths
            # If import produced nothing usable, fall back to the download dir.
            if not scan_targets and (ir.errors or ir.skipped):
                fallback = self._to_context(content_path, "qbittorrent", imp.work_context)
                if fallback:
                    scan_targets = [fallback]
        else:
            fallback = self._to_context(content_path, "qbittorrent", imp.work_context)
            if fallback:
                scan_targets = [fallback]

        if not (notify and self.config.plex.url and self.config.plex.token):
            return
        notified = []
        for target in scan_targets:
            # Translate from the importer's work context into Plex's namespace.
            plex_path = self._to_context(target, imp.work_context, "plex") or target
            try:
                await asyncio.to_thread(self.plex().scan, section, plex_path)
                notified.append(plex_path)
            except Exception as exc:  # noqa: BLE001 - report, don't crash poll
                result.setdefault("notify_errors", []).append(str(exc))
        if notified:
            result["notified"] = True
            result["plex_paths"] = notified

    def _to_context(self, path: Optional[str], from_ctx: str, to_ctx: str) -> Optional[str]:
        if not path:
            return None
        if from_ctx == to_ctx:
            return path
        translated = pathmap.translate(self.config, path, from_ctx, to_ctx)
        return translated

    # -- RSS / auto-grab ---------------------------------------------------- #
    async def rss_poll(self) -> dict:
        """Search for every monitored, still-missing episode and (optionally)
        auto-grab the best matching, not-yet-seen release."""
        cfg = self.config
        grabbed, candidates, checked = [], [], 0
        for series in self.db.list_series():
            if not series["monitored"]:
                continue
            missing = self.db.list_episodes(
                series["id"], status="missing", monitored=True
            )
            if not missing:
                continue
            title = series["title"]
            absolute = bool(series.get("absolute_numbering"))
            try:
                releases = await self.prowlarr().search(title, categories=[CAT_TV])
            except Exception as exc:  # noqa: BLE001
                candidates.append({"series": title, "error": str(exc)})
                continue
            for ep in missing:
                # A pack grabbed earlier in this run may already cover this
                # episode — re-check so we don't grab a redundant single.
                current = self.db.get_episode(ep["id"])
                if not current or current["status"] != "missing":
                    continue
                checked += 1
                matching = [
                    r
                    for r in releases
                    if title_matches_episode(r.title, ep["season"], ep["episode"], absolute)
                ]
                pick = selector.best(matching, cfg.quality)
                if not pick or not pick.grab_url:
                    continue
                if self.db.seen_guid(pick.guid):
                    continue
                label = f"{title} S{ep['season']:02d}E{ep['episode']:02d}"
                if cfg.rss.auto_grab:
                    res = await self.grab(
                        pick.grab_url,
                        title=pick.title,
                        series_id=series["id"],
                        episode_id=ep["id"],
                        indexer=pick.indexer,
                        size=pick.size,
                        guid=pick.guid,
                    )
                    grabbed.append({"episode": label, "release": pick.title, **res})
                else:
                    candidates.append(
                        {"episode": label, "release": pick.title, "grab_url": pick.grab_url}
                    )

        # Monitored movies that are still missing.
        movies_checked = 0
        for movie in self.db.list_movies():
            if not movie["monitored"] or movie["movie_status"] != "missing":
                continue
            movies_checked += 1
            query = f"{movie['title']} {movie['year']}" if movie["year"] else movie["title"]
            try:
                releases = await self.prowlarr().search(query, categories=[CAT_MOVIE])
            except Exception as exc:  # noqa: BLE001
                candidates.append({"movie": movie["title"], "error": str(exc)})
                continue
            pick = selector.best(releases, cfg.quality)
            if not pick or not pick.grab_url or self.db.seen_guid(pick.guid):
                continue
            if cfg.rss.auto_grab:
                res = await self.grab(
                    pick.grab_url,
                    title=pick.title,
                    movie_id=movie["id"],
                    indexer=pick.indexer,
                    size=pick.size,
                    guid=pick.guid,
                )
                grabbed.append({"movie": movie["title"], "release": pick.title, **res})
            else:
                candidates.append(
                    {"movie": movie["title"], "release": pick.title, "grab_url": pick.grab_url}
                )

        return {
            "checked_episodes": checked,
            "checked_movies": movies_checked,
            "grabbed": grabbed,
            "candidates": candidates,
        }

    # -- movies ------------------------------------------------------------- #
    async def add_movie(
        self,
        provider_id: str,
        monitored: bool = True,
        root_folder: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> dict:
        info = await self.provider(provider).get_movie(provider_id)
        folder = _folder_name(info.title, info.year)
        movie_id = self.db.upsert_movie(
            provider=info.provider,
            provider_id=info.provider_id,
            title=info.title,
            year=info.year,
            overview=info.overview,
            status=info.status,
            poster=info.poster,
            monitored=1 if monitored else 0,
            root_folder=root_folder,
            folder_name=folder,
        )
        return self.db.get_movie(movie_id)

    async def activate_series(
        self,
        series_id: int,
        provider: Optional[str] = None,
        provider_id: Optional[str] = None,
        mark_downloaded: bool = True,
    ) -> dict:
        """Turn a catalogued series (e.g. from import_from_plex) into a fully
        monitored one: fetch its episode list from a metadata provider, then mark
        the episodes Plex already has as downloaded so only the genuinely-missing
        ones are auto-grabbed."""
        series = self.db.get_series(series_id)
        if not series:
            return {"error": f"No series with id {series_id}"}
        provider = provider or series["provider"]
        provider_id = provider_id or series["provider_id"]
        if provider == "plex":
            return {
                "error": "This series was catalogued from Plex without a metadata id. "
                "Pass provider and provider_id — e.g. provider='jikan' with the "
                "MyAnimeList id, or provider='tmdb' with the TMDB id."
            }

        prov = self.provider(provider)
        info = await prov.get_series(provider_id)
        absolute = bool(getattr(prov, "absolute_numbering", False))
        # Capture the Plex handle before we rewrite the series keys.
        plex_key = series["provider_id"] if series["provider"] == "plex" else None
        plex_title = series["title"]

        try:
            self.db.execute(
                "UPDATE series SET provider=?, provider_id=?, title=?, year=?, overview=?, "
                "status=?, poster=?, absolute_numbering=? WHERE id=?",
                (info.provider, info.provider_id, info.title, info.year, info.overview,
                 info.status, info.poster, 1 if absolute else 0, series_id),
            )
        except sqlite3.IntegrityError:
            return {
                "error": f"A different series already uses provider={info.provider} "
                f"id={info.provider_id}. Remove it or activate that one instead."
            }

        for ep in info.episodes:
            self.db.upsert_episode(
                series_id, ep.season, ep.episode, title=ep.title, air_date=ep.air_date
            )

        marked = 0
        if mark_downloaded and self.config.plex.url and self.config.plex.token:
            try:
                plex_eps = await asyncio.to_thread(
                    self.plex().show_episodes, plex_key, plex_title, self.config.plex.tv_section
                )
            except Exception:  # noqa: BLE001
                plex_eps = []
            lib = self.db.list_episodes(series_id)
            if absolute:
                # Anime = one entry; Plex's file count is the absolute progress.
                have = len(plex_eps)
                for e in lib:
                    if e["season"] == 1 and e["episode"] <= have and e["status"] == "missing":
                        self.db.set_episode_status(e["id"], "downloaded")
                        marked += 1
            else:
                haveset = set(plex_eps)
                for e in lib:
                    if (e["season"], e["episode"]) in haveset and e["status"] == "missing":
                        self.db.set_episode_status(e["id"], "downloaded")
                        marked += 1

        lib = self.db.list_episodes(series_id)
        return {
            "series_id": series_id,
            "title": info.title,
            "provider": info.provider,
            "provider_id": info.provider_id,
            "absolute_numbering": absolute,
            "episodes": len(lib),
            "marked_downloaded": marked,
            "still_missing": sum(1 for e in lib if e["status"] == "missing"),
        }

    # -- import existing Plex library --------------------------------------- #
    async def import_from_plex(
        self,
        dry_run: bool = True,
        monitored: bool = False,
        media_type: str = "all",
        sections: Optional[list[str]] = None,
    ) -> dict:
        """Register the shows/movies already in Plex as owned library entries so
        they aren't re-downloaded. Uses Plex's external ids (TMDB when present,
        else a Plex rating key). ``sections`` restricts to specific Plex library
        names (e.g. exclude an "AV" section). Series are catalogued without
        episodes — activate monitoring for one via add_series with a provider id."""
        items = await asyncio.to_thread(self.plex().catalog)
        anime_section = self.config.plex.tv_section
        # Report what's available so the caller can pick sections on a dry run.
        by_section: dict[str, int] = {}
        for it in items:
            by_section[it["section"]] = by_section.get(it["section"], 0) + 1

        preview, counts = [], {"series": 0, "movies": 0, "skipped": 0}
        for it in items:
            if sections is not None and it["section"] not in sections:
                continue
            if media_type == "tv" and it["type"] != "show":
                continue
            if media_type == "movie" and it["type"] != "movie":
                continue
            guids = it.get("guids") or {}
            if guids.get("tmdb"):
                provider, pid = "tmdb", guids["tmdb"]
            else:
                provider, pid = "plex", it["rating_key"]
            absolute = it["type"] == "show" and it["section"] == anime_section
            entry = {
                "title": it["title"], "year": it["year"], "type": it["type"],
                "section": it["section"], "provider": provider, "provider_id": pid,
                "absolute_numbering": absolute,
            }
            preview.append(entry)
            if dry_run:
                continue

            folder = _folder_name(it["title"], it["year"])
            if it["type"] == "movie":
                self.db.upsert_movie(
                    provider=provider, provider_id=str(pid), title=it["title"],
                    year=it["year"], monitored=1 if monitored else 0,
                    folder_name=folder, movie_status="downloaded",
                )
                counts["movies"] += 1
            else:
                self.db.upsert_series(
                    provider=provider, provider_id=str(pid), title=it["title"],
                    year=it["year"], monitored=1 if monitored else 0,
                    folder_name=folder, absolute_numbering=1 if absolute else 0,
                )
                counts["series"] += 1

        note = (
            "Series are catalogued without episodes. To enable episode "
            "monitoring/auto-grab for one, call add_series with its provider id."
        )
        if dry_run and sections is None:
            note = (
                "DRY RUN across ALL libraries — review sections_available and re-run "
                "with sections=[…] to include only the ones you want. " + note
            )
        return {
            "dry_run": dry_run,
            "scanned": len(items),
            "sections_available": by_section,
            "sections_used": sections,
            "matched": len(preview),
            "registered": None if dry_run else counts,
            "with_tmdb_id": sum(1 for e in preview if e["provider"] == "tmdb"),
            "sample": preview[:40],
            "note": note,
        }

    # -- season packs ------------------------------------------------------- #
    async def grab_season(
        self, series_id: int, season: int, client_name: Optional[str] = None
    ) -> dict:
        """Find and grab the best pack covering a season and link it to the
        series. On import each episode is split into place; grabbing marks every
        covered episode as grabbed. For anime (one-season entries) any season
        number resolves to the whole-series batch."""
        series = self.db.get_series(series_id)
        if not series:
            return {"error": f"No series with id {series_id}"}
        absolute = bool(series.get("absolute_numbering"))
        title = series["title"]
        query = title if absolute else f"{title} S{season:02d}"

        try:
            releases = await self.prowlarr().search(query, categories=[CAT_TV])
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "query": query}

        wanted = {
            e["id"]
            for e in self.db.list_episodes(series_id)
            if absolute or e["season"] == season
        }
        # A pack must cover at least two of the season's episodes.
        packs = [
            r
            for r in releases
            if len(set(self._covered_episode_ids(series, r.title)) & wanted) >= 2
        ]
        best = selector.best(packs, self.config.quality)
        if not best or not best.grab_url:
            return {
                "error": "No season pack found",
                "query": query,
                "candidates": [r.title for r in releases[:10]],
            }
        res = await self.grab(
            best.grab_url,
            title=best.title,
            series_id=series_id,
            indexer=best.indexer,
            size=best.size,
            guid=best.guid,
            client_name=client_name,
        )
        return {"picked": best.title, **res}
