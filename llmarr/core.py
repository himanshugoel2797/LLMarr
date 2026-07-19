"""Application core — high-level operations shared by the MCP tools and the RSS
poller. Holds the config store and database and knows how to wire the individual
services (metadata, indexer, download client, Plex) together for the common
flows: add a series, search releases, grab a release, track downloads, import.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
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
    matches_single_episode,
    parse_absolute_episode,
    parse_absolute_range,
    parse_episode,
    parse_resolution,
    parse_season_pack,
    resolution_rank,
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
        fields = dict(
            provider=info.provider,
            provider_id=info.provider_id,
            title=info.title,
            year=info.year,
            overview=info.overview,
            status=info.status,
            poster=info.poster,
            folder_name=folder,
            absolute_numbering=1 if getattr(prov, "absolute_numbering", False) else 0,
        )
        # monitored / root_folder are user choices — set them only on first add,
        # not on a metadata refresh (re-add), so re-adding never wipes them.
        prior = self.db.query_one(
            "SELECT 1 FROM series WHERE provider=? AND provider_id=?",
            (info.provider, info.provider_id),
        )
        if prior is None:
            fields["monitored"] = 1 if monitored else 0
            fields["root_folder"] = root_folder
        series_id = self.db.upsert_series(**fields)
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
            if ep.season == 0:
                # Specials are off by default; opt in with seasons=[0, …].
                ep_monitored = monitored and seasons is not None and 0 in seasons
            else:
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

    # -- periodic metadata refresh (G1) ------------------------------------- #
    # Statuses that mean a series will not gain new episodes, so refresh can skip
    # it (TMDB uses "Ended"/"Canceled"; jikan uses "Finished Airing").
    ENDED_STATUSES = {"ended", "finished airing", "canceled", "cancelled"}

    async def refresh_series(self, series_id: int) -> dict:
        """Re-fetch provider metadata for an existing series and upsert any newly
        aired episodes. Never clobbers existing episodes' status/monitored flags,
        and never resets series-level monitored/root_folder — only refreshes
        metadata (title/year/overview/status/poster) and adds new episodes. New
        regular episodes are monitored iff the series is monitored; specials
        (season 0) are left unmonitored (mirrors add_series)."""
        series = self.db.get_series(series_id)
        if not series:
            return {"error": f"No series with id {series_id}"}
        if series["provider"] == "plex":
            return {
                "error": "This series was catalogued from Plex without a metadata id; "
                "activate it with activate_series before it can be refreshed."
            }
        prov = self.provider(series["provider"])
        info = await prov.get_series(series["provider_id"])
        monitored = bool(series["monitored"])

        existing = {(e["season"], e["episode"]) for e in self.db.list_episodes(series_id)}
        added = []
        for ep in info.episodes:
            self.db.upsert_episode(
                series_id, ep.season, ep.episode, title=ep.title, air_date=ep.air_date
            )
            if (ep.season, ep.episode) in existing:
                continue
            # New episode: apply the monitoring rule. Specials off by default.
            ep_monitored = monitored and ep.season != 0
            row = self.db.query_one(
                "SELECT id FROM episodes WHERE series_id=? AND season=? AND episode=?",
                (series_id, ep.season, ep.episode),
            )
            self.db.execute(
                "UPDATE episodes SET monitored=? WHERE id=?",
                (1 if ep_monitored else 0, row["id"]),
            )
            added.append((ep.season, ep.episode))

        # Refresh metadata (never touch monitored/root_folder/identity) + stamp time.
        self.db.execute(
            "UPDATE series SET title=?, year=?, overview=?, status=?, poster=?, "
            "last_refresh=? WHERE id=?",
            (info.title, info.year, info.overview, info.status, info.poster,
             time.time(), series_id),
        )
        return {
            "series_id": series_id,
            "title": info.title,
            "status": info.status,
            "new_episodes": len(added),
            "added": [f"S{s:02d}E{e:02d}" for s, e in added],
            "total_episodes": len(info.episodes),
        }

    async def refresh_stale_series(self) -> list[dict]:
        """Refresh monitored, still-airing series whose metadata hasn't been
        re-fetched within rss.refresh_interval_hours. Used by the RSS poller."""
        hours = self.config.rss.refresh_interval_hours
        if not hours:
            return []
        cutoff = time.time() - hours * 3600
        out = []
        for series in self.db.list_series():
            if not series["monitored"] or series["provider"] == "plex":
                continue
            if (series.get("status") or "").strip().lower() in self.ENDED_STATUSES:
                continue
            last = series.get("last_refresh")
            if last is not None and last > cutoff:
                continue
            try:
                res = await self.refresh_series(series["id"])
            except Exception as exc:  # noqa: BLE001 - report, don't crash the poll
                out.append({"series": series["title"], "error": str(exc)})
                continue
            if res.get("new_episodes"):
                out.append(res)
        return out

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

    def reset_grab_to_missing(self, download: dict) -> list:
        """When a grab is cancelled/failed, put the episodes/movie it covered back
        to 'missing' (only if still 'grabbed', never touching imported/downloaded)
        so the RSS poller will try again."""
        reset = []
        if download.get("movie_id"):
            movie = self.db.get_movie(download["movie_id"])
            if movie and movie["movie_status"] == "grabbed":
                self.db.set_movie_status(download["movie_id"], "missing")
                reset.append(("movie", download["movie_id"]))
        eids = set()
        if download.get("series_id"):
            series = self.db.get_series(download["series_id"])
            if series:
                eids.update(self._covered_episode_ids(series, download.get("title") or ""))
        if download.get("episode_id"):
            eids.add(download["episode_id"])
        for eid in eids:
            ep = self.db.get_episode(eid)
            if ep and ep["status"] == "grabbed":
                self.db.set_episode_status(eid, "missing")
                reset.append(("episode", eid))
        return reset

    # -- recovery (G2) ------------------------------------------------------ #
    def reset_episode(self, episode_id: int) -> dict:
        """Force an episode back to 'missing' so the RSS poller re-grabs it —
        for unsticking one that is wedged in 'grabbed'/'downloaded'."""
        ep = self.db.get_episode(episode_id)
        if not ep:
            return {"error": f"No episode with id {episode_id}"}
        self.db.set_episode_status(episode_id, "missing")
        return {"episode_id": episode_id, "status": "missing", "was": ep["status"]}

    def reset_movie(self, movie_id: int) -> dict:
        """Force a movie back to 'missing' so the RSS poller re-grabs it."""
        movie = self.db.get_movie(movie_id)
        if not movie:
            return {"error": f"No movie with id {movie_id}"}
        self.db.set_movie_status(movie_id, "missing")
        return {"movie_id": movie_id, "status": "missing", "was": movie["movie_status"]}

    def mark_download_failed(self, download_id: int) -> dict:
        """Mark a download failed and free the episodes/movie it covered (only
        those still 'grabbed') back to 'missing' so RSS can try another release."""
        d = self.db.get_download(download_id)
        if not d:
            return {"error": f"No download with id {download_id}"}
        self.db.set_download_status(download_id, "failed")
        reset = self.reset_grab_to_missing(d)
        return {"download_id": download_id, "status": "failed", "reset_to_missing": len(reset)}

    def retry_download(self, download_id: int) -> dict:
        """Retry a stuck/failed grab: mark it failed and force every linked
        episode/movie back to 'missing' (regardless of current status) so the RSS
        poller grabs a fresh release for it on the next tick."""
        d = self.db.get_download(download_id)
        if not d:
            return {"error": f"No download with id {download_id}"}
        self.db.set_download_status(download_id, "failed")
        reset = 0
        if d.get("movie_id"):
            self.db.set_movie_status(d["movie_id"], "missing")
            reset += 1
        eids: set[int] = set()
        if d.get("series_id"):
            series = self.db.get_series(d["series_id"])
            if series:
                eids.update(self._covered_episode_ids(series, d.get("title") or ""))
        if d.get("episode_id"):
            eids.add(d["episode_id"])
        for eid in eids:
            if self.db.get_episode(eid):
                self.db.set_episode_status(eid, "missing")
                reset += 1
        return {"download_id": download_id, "status": "failed", "reset_to_missing": reset}

    # -- disk-space guard (G10) --------------------------------------------- #
    def _check_grab_space(self, cfg: DownloadClientConfig, save_path, size) -> None:
        """Refuse a grab that would leave the download filesystem below the
        configured free-space floor. Only fires when the floor is set, the release
        size is known, and the download dir is reachable in the work context
        (single-host, or an explicit mapping); otherwise it silently allows the
        grab rather than guessing."""
        from . import importer as _importer

        floor = self.config.importer.min_free_space_mb
        if floor <= 0 or not size:
            return
        target = save_path or cfg.save_path
        if not target:
            return
        try:
            local = self._to_context(target, "qbittorrent", self.config.importer.work_context)
        except Exception:  # noqa: BLE001 - unmapped path: don't block, just skip
            return
        if not local:
            return
        free = _importer.free_space_mb(local)
        if free is None:
            return
        need = size / (1024 * 1024) + floor
        if free < need:
            raise ValueError(
                f"Insufficient free space to grab: {free:.0f}MB free at {local}, "
                f"need >= {need:.0f}MB (release {size / (1024 * 1024):.0f}MB + "
                f"min_free_space_mb {floor}). Free up space or lower "
                f"importer.min_free_space_mb."
            )

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
        is_upgrade: bool = False,
        mark_status: bool = True,
    ) -> dict:
        name, cfg = self._client_config(client_name)
        client = get_client(cfg)
        category = category or cfg.category

        self._check_grab_space(cfg, save_path, size)

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
            is_upgrade=1 if is_upgrade else 0,
            status="grabbed",
        )
        # A quality upgrade grabs with mark_status=False so the episode/movie stays
        # "downloaded" while the better release downloads — a failed upgrade then
        # can't strand it out of the library. The new file replaces the old on
        # import (see Importer overwrite handling).
        covered: list[int] = []
        if mark_status:
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
        # "completed" is included so an import that failed last time (e.g. a path
        # mapping not yet fixed) is retried — imports are idempotent.
        pending = [
            d
            for d in self.db.list_downloads()
            if d["status"] in ("grabbed", "downloading", "completed") and d["torrent_hash"]
        ]
        for d in pending:
            client = self.download_client(d["client"])
            st = await asyncio.to_thread(client.status, d["torrent_hash"])
            if st is None or st.state in ("error", "missingFiles"):
                # Torrent vanished from the client or errored — fail it and free
                # its episodes/movie so the RSS poller can try another release.
                self.db.set_download_status(d["id"], "failed")
                self.reset_grab_to_missing(d)
                updates.append({
                    "download_id": d["id"], "state": "failed",
                    "reason": "not in client" if st is None else st.state,
                })
                continue
            if st.completed:
                content = st.content_path or st.save_path
                # Do NOT mark "completed" before importing — otherwise an import
                # failure would strand the row out of the retry set.
                result = {"download_id": d["id"], "state": "completed", "notified": False}
                section = (
                    self.config.plex.movie_section
                    if d.get("movie_id")
                    else self.config.plex.tv_section
                )
                await self._import_and_notify(d, content, section, result, notify)
                # _import_and_notify sets "imported" on success; otherwise record
                # "completed" so it's retried next poll.
                if self.db.get_download(d["id"])["status"] != "imported":
                    imp = result.get("import") or {}
                    if d.get("is_upgrade") and imp.get("skipped") and not imp.get("errors"):
                        # The "upgrade" turned out not to be an improvement — a fixed
                        # property of the content, so retrying is pointless and would
                        # keep the item's upgrade guard active forever. Fail it
                        # terminally; the item was never flipped out of 'downloaded'.
                        self.db.set_download_status(d["id"], "failed")
                        result["state"] = "not_an_upgrade"
                    else:
                        self.db.set_download_status(d["id"], "completed", save_path=content)
                updates.append(result)
            elif st.progress > 0:
                self.db.set_download_status(d["id"], "downloading")
                updates.append(
                    {"download_id": d["id"], "state": "downloading", "progress": st.progress}
                )
        return updates

    async def download_queue(self) -> list[dict]:
        """Live progress for every grab still in a download client (not yet
        imported/removed) — name, %, speed, ETA, seeds."""
        out = []
        for d in self.db.list_downloads():
            if not d["torrent_hash"] or d["status"] in ("imported", "removed", "failed"):
                continue
            try:
                st = await asyncio.to_thread(
                    self.download_client(d["client"]).status, d["torrent_hash"]
                )
            except Exception as exc:  # noqa: BLE001
                out.append({"download_id": d["id"], "title": d["title"], "error": str(exc)})
                continue
            if not st:
                continue
            eta = st.eta if (st.eta is not None and 0 <= st.eta < 8640000) else None
            out.append({
                "download_id": d["id"],
                "title": d["title"][:90],
                "status": d["status"],
                "state": st.state,
                "progress_pct": round(st.progress * 100, 1),
                "dl_speed_kbps": round(st.dl_speed / 1024, 1) if st.dl_speed else 0,
                "eta_seconds": eta,
                "seeds": st.num_seeds,
                "size_mb": round(st.size / 1048576, 1) if st.size else None,
                "series_id": d["series_id"],
                "movie_id": d["movie_id"],
            })
        return out

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
        upgraded: list[dict] = []
        cutoff = resolution_rank(cfg.quality.upgrade_until) if cfg.quality.upgrade_until else None
        for series in self.db.list_series():
            if not series["monitored"]:
                continue
            missing = self.db.list_episodes(
                series["id"], status="missing", monitored=True
            )
            # Downloaded, monitored episodes below the upgrade cutoff that don't
            # already have an upgrade in flight (G4).
            upgradable = []
            if cutoff is not None:
                upgradable = [
                    e
                    for e in self.db.list_episodes(
                        series["id"], status="downloaded", monitored=True
                    )
                    if resolution_rank(e.get("quality")) < cutoff
                    and not self.db.has_active_upgrade(episode_id=e["id"])
                ]
            if not missing and not upgradable:
                continue
            title = series["title"]
            absolute = bool(series.get("absolute_numbering"))
            try:
                releases = await self.prowlarr().search(title, categories=[CAT_TV])
            except Exception as exc:  # noqa: BLE001
                candidates.append({"series": title, "error": str(exc)})
                continue
            for ep in upgradable:
                matching = [
                    r
                    for r in releases
                    if matches_single_episode(r.title, ep["season"], ep["episode"], absolute)
                    and not self.db.seen_guid(r.guid)
                ]
                pick = selector.best_upgrade(matching, ep.get("quality"), cfg.quality)
                if not pick or not pick.grab_url:
                    continue
                label = f"{title} S{ep['season']:02d}E{ep['episode']:02d}"
                entry = {
                    "episode": label, "release": pick.title,
                    "from_quality": ep.get("quality"),
                    "to_quality": parse_resolution(pick.title),
                }
                if cfg.rss.auto_grab:
                    res = await self.grab(
                        pick.grab_url,
                        title=pick.title,
                        series_id=series["id"],
                        episode_id=ep["id"],
                        indexer=pick.indexer,
                        size=pick.size,
                        guid=pick.guid,
                        is_upgrade=True,
                        mark_status=False,
                    )
                    upgraded.append({**entry, **res})
                else:
                    entry["grab_url"] = pick.grab_url
                    candidates.append({"upgrade": entry})
            for ep in missing:
                # A pack grabbed earlier in this run may already cover this
                # episode — re-check so we don't grab a redundant single.
                current = self.db.get_episode(ep["id"])
                if not current or current["status"] != "missing":
                    continue
                checked += 1
                # Exclude already-seen releases BEFORE ranking, so a seen top pick
                # doesn't block an available second-best.
                matching = [
                    r
                    for r in releases
                    if title_matches_episode(r.title, ep["season"], ep["episode"], absolute)
                    and not self.db.seen_guid(r.guid)
                ]
                pick = selector.best(matching, cfg.quality)
                if not pick or not pick.grab_url:
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

        # Monitored movies: grab the missing ones, and upgrade downloaded ones
        # sitting below the quality cutoff (G4).
        movies_checked = 0
        for movie in self.db.list_movies():
            if not movie["monitored"]:
                continue
            missing = movie["movie_status"] == "missing"
            upgradable = (
                cutoff is not None
                and movie["movie_status"] == "downloaded"
                and resolution_rank(movie.get("quality")) < cutoff
                and not self.db.has_active_upgrade(movie_id=movie["id"])
            )
            if not missing and not upgradable:
                continue
            movies_checked += 1
            query = f"{movie['title']} {movie['year']}" if movie["year"] else movie["title"]
            try:
                releases = await self.prowlarr().search(query, categories=[CAT_MOVIE])
            except Exception as exc:  # noqa: BLE001
                candidates.append({"movie": movie["title"], "error": str(exc)})
                continue
            fresh = [r for r in releases if not self.db.seen_guid(r.guid)]
            if missing:
                pick = selector.best(fresh, cfg.quality)
                if pick and pick.grab_url:
                    if cfg.rss.auto_grab:
                        res = await self.grab(
                            pick.grab_url, title=pick.title, movie_id=movie["id"],
                            indexer=pick.indexer, size=pick.size, guid=pick.guid,
                        )
                        grabbed.append({"movie": movie["title"], "release": pick.title, **res})
                    else:
                        candidates.append(
                            {"movie": movie["title"], "release": pick.title, "grab_url": pick.grab_url}
                        )
            elif upgradable:
                pick = selector.best_upgrade(fresh, movie.get("quality"), cfg.quality)
                if pick and pick.grab_url:
                    entry = {
                        "movie": movie["title"], "release": pick.title,
                        "from_quality": movie.get("quality"),
                        "to_quality": parse_resolution(pick.title),
                    }
                    if cfg.rss.auto_grab:
                        res = await self.grab(
                            pick.grab_url, title=pick.title, movie_id=movie["id"],
                            indexer=pick.indexer, size=pick.size, guid=pick.guid,
                            is_upgrade=True, mark_status=False,
                        )
                        upgraded.append({**entry, **res})
                    else:
                        entry["grab_url"] = pick.grab_url
                        candidates.append({"upgrade": entry})

        return {
            "checked_episodes": checked,
            "checked_movies": movies_checked,
            "grabbed": grabbed,
            "upgraded": upgraded,
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
        # If Plex flagged this section as anime, absolute numbering applies even
        # if the metadata provider itself doesn't declare it.
        anime_section = self.config.plex.anime_section
        if anime_section and series.get("plex_section") == anime_section:
            absolute = True
        # Capture the Plex handle before we rewrite the series keys — prefer the
        # rating key captured at import time (G5) over the plex-provider fallback.
        plex_key = series.get("plex_rating_key") or (
            series["provider_id"] if series["provider"] == "plex" else None
        )
        plex_title = series["title"]

        try:
            # Activation makes it monitored — otherwise the RSS poller keeps
            # ignoring it and "activation" grabs nothing.
            self.db.execute(
                "UPDATE series SET provider=?, provider_id=?, title=?, year=?, overview=?, "
                "status=?, poster=?, absolute_numbering=?, monitored=1 WHERE id=?",
                (info.provider, info.provider_id, info.title, info.year, info.overview,
                 info.status, info.poster, 1 if absolute else 0, series_id),
            )
        except sqlite3.IntegrityError:
            return {
                "error": f"A different series already uses provider={info.provider} "
                f"id={info.provider_id}. Remove it or activate that one instead."
            }

        existing = {(e["season"], e["episode"]) for e in self.db.list_episodes(series_id)}
        for ep in info.episodes:
            self.db.upsert_episode(
                series_id, ep.season, ep.episode, title=ep.title, air_date=ep.air_date
            )
            # Specials (season 0) off by default, like add_series.
            if ep.season == 0 and (ep.season, ep.episode) not in existing:
                row = self.db.query_one(
                    "SELECT id FROM episodes WHERE series_id=? AND season=0 AND episode=?",
                    (series_id, ep.episode),
                )
                self.db.execute("UPDATE episodes SET monitored=0 WHERE id=?", (row["id"],))

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

    async def bulk_activate_series(
        self, mark_downloaded: bool = True, limit: Optional[int] = None
    ) -> dict:
        """Activate every catalogued (episode-less) series whose provider id is
        safely derivable — i.e. non-anime, tmdb-keyed shows. Anime is skipped on
        purpose: a Plex tmdb id is NOT a jikan/MyAnimeList id, so those must be
        activated one-by-one with their MAL id (see activate_series). plex-only
        entries (no metadata id) are skipped too. Runs sequentially to respect
        provider rate limits. Reports what was activated and what was skipped and
        why."""
        anime_section = self.config.plex.anime_section
        activated, skipped = [], []
        count = 0
        for series in self.db.list_series():
            if self.db.list_episodes(series["id"]):
                continue  # already activated / has episodes
            title = series["title"]
            is_anime = bool(series.get("absolute_numbering")) or (
                anime_section is not None and series.get("plex_section") == anime_section
            )
            if series["provider"] == "plex":
                skipped.append({"series_id": series["id"], "title": title,
                                "reason": "no metadata id (catalogued from Plex without a tmdb guid)"})
                continue
            if is_anime:
                skipped.append({"series_id": series["id"], "title": title,
                                "reason": "anime — a Plex tmdb id is not a MAL id; "
                                "activate_series with the jikan/MyAnimeList id"})
                continue
            if series["provider"] != "tmdb":
                skipped.append({"series_id": series["id"], "title": title,
                                "reason": f"provider {series['provider']} not auto-activatable"})
                continue
            if limit is not None and count >= limit:
                skipped.append({"series_id": series["id"], "title": title,
                                "reason": "limit reached"})
                continue
            try:
                res = await self.activate_series(series["id"], mark_downloaded=mark_downloaded)
            except Exception as exc:  # noqa: BLE001
                skipped.append({"series_id": series["id"], "title": title, "reason": str(exc)})
                continue
            if "error" in res:
                skipped.append({"series_id": series["id"], "title": title, "reason": res["error"]})
                continue
            count += 1
            activated.append({"series_id": series["id"], "title": res["title"],
                              "episodes": res["episodes"], "marked_downloaded": res["marked_downloaded"]})
        return {
            "activated_count": len(activated),
            "skipped_count": len(skipped),
            "activated": activated,
            "skipped": skipped,
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
        anime_section = self.config.plex.anime_section  # None = flag nothing
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
                mid = self.db.upsert_movie(
                    provider=provider, provider_id=str(pid), title=it["title"],
                    year=it["year"], monitored=1 if monitored else 0,
                    folder_name=folder, movie_status="downloaded",
                )
                # upsert_movie doesn't update movie_status on conflict; a movie
                # already tracked as 'missing' is owned in Plex — mark it so.
                self.db.set_movie_status(mid, "downloaded")
                counts["movies"] += 1
            else:
                self.db.upsert_series(
                    provider=provider, provider_id=str(pid), title=it["title"],
                    year=it["year"], monitored=1 if monitored else 0,
                    folder_name=folder, absolute_numbering=1 if absolute else 0,
                    plex_rating_key=str(it["rating_key"]), plex_section=it["section"],
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
