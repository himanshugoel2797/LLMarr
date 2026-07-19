"""Import completed downloads into the organised Plex library.

Replaces the naive "just scan the download directory" behaviour. On completion a
download's files are hardlinked (or copied/moved) from the download folder into
a Sonarr/Radarr-style library layout::

    <root>/Series Title (Year)/Season 01/Series Title - S01E01 - Ep Title.mkv
    <root>/Movie Title (Year)/Movie Title (Year).mkv

All filesystem work happens in ``importer.work_context`` — the namespace LLMarr
itself can read/write. The download's ``content_path`` (as the client sees it)
and the destination root folder are both translated into that context first, so
hardlinks work as long as the two live on the same filesystem there.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from . import pathmap
from .parsing import parse_absolute_episode, parse_episode


class ImportedFile(BaseModel):
    source: str
    destination: str
    action: str  # hardlink|copy|move
    season: Optional[int] = None
    episode: Optional[int] = None
    episode_id: Optional[int] = None


class ImportResult(BaseModel):
    imported: list[ImportedFile] = []
    scan_paths: list[str] = []  # library dirs (work context) to hand to Plex
    skipped: list[str] = []
    errors: list[str] = []

    @property
    def ok(self) -> bool:
        return bool(self.imported) and not self.errors


def _sanitize(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "", name).strip()


class Importer:
    def __init__(self, app):
        self.app = app

    @property
    def cfg(self):
        return self.app.config.importer

    # -- path helpers ------------------------------------------------------- #
    def to_work(self, path: str, from_context: str) -> Optional[str]:
        """Translate a path into the work context. If no mapping exists but the
        contexts already match, return it unchanged; otherwise ``None``."""
        wc = self.cfg.work_context
        if from_context == wc:
            return path
        return pathmap.translate(self.app.config, path, from_context, wc)

    def root_local(self, root_name: Optional[str], media_type: str) -> Optional[Path]:
        rf = self.app.root_folder(root_name, media_type)
        if not rf:
            return None
        local = self.to_work(rf.path, rf.context)
        return Path(local) if local else None

    # -- file collection ---------------------------------------------------- #
    def collect_videos(self, root: Path) -> list[Path]:
        exts = {e.lower() for e in self.cfg.video_extensions}
        min_bytes = self.cfg.min_video_mb * 1024 * 1024
        if root.is_file():
            candidates = [root]
        else:
            candidates = [p for p in root.rglob("*") if p.is_file()]
        out = []
        for p in candidates:
            if p.suffix.lower() not in exts:
                continue
            if "sample" in p.name.lower():
                continue
            try:
                if p.stat().st_size < min_bytes:
                    continue
            except OSError:
                continue
            out.append(p)
        return out

    # -- linking ------------------------------------------------------------ #
    def _place(self, src: Path, dst: Path) -> str:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            return "exists"
        mode = self.cfg.mode
        if mode == "move":
            shutil.move(str(src), str(dst))
            return "move"
        if mode == "copy":
            shutil.copy2(str(src), str(dst))
            return "copy"
        # hardlink with graceful fallback to copy across filesystems
        try:
            os.link(str(src), str(dst))
            return "hardlink"
        except OSError as exc:
            if exc.errno in (errno.EXDEV, errno.EPERM, errno.EMLINK):
                shutil.copy2(str(src), str(dst))
                return "copy"
            raise

    # -- entry point -------------------------------------------------------- #
    def import_download(self, download: dict, content_path: Optional[str]) -> ImportResult:
        result = ImportResult()
        if not content_path:
            result.errors.append("no content path reported by the download client")
            return result
        local = self.to_work(content_path, "qbittorrent")
        if not local:
            result.errors.append(
                f"cannot map '{content_path}' from qbittorrent to '{self.cfg.work_context}' "
                "context — add a path mapping so LLMarr can access the files"
            )
            return result
        root = Path(local)
        if not root.exists():
            result.errors.append(f"path '{local}' does not exist in the work context")
            return result

        videos = self.collect_videos(root)
        if not videos:
            result.skipped.append(f"no video files found under {local}")
            return result

        if download.get("movie_id"):
            return self._import_movie(download, videos, result)
        return self._import_series(download, videos, result)

    # -- series ------------------------------------------------------------- #
    def _import_series(self, download: dict, videos: list[Path], result: ImportResult):
        series = self.app.db.get_series(download.get("series_id")) if download.get("series_id") else None
        if not series:
            result.errors.append("download is not linked to a series; cannot organise")
            return result
        dest_root = self.root_local(series["root_folder"], "tv")
        if not dest_root:
            result.errors.append(
                "no TV root folder resolves to the work context; configure one with configure_root_folder"
            )
            return result
        series_dir = dest_root / _sanitize(series["folder_name"] or series["title"])
        absolute = bool(series.get("absolute_numbering"))

        linked_episode_id = download.get("episode_id")
        scan_dirs: set[str] = set()
        for video in videos:
            if absolute:
                # Anime files use absolute numbers ([Group] Show - 12); map to
                # season 1. Fall back to SxxExx if the file happens to use it.
                n = parse_absolute_episode(video.name)
                se = (1, n) if n is not None else parse_episode(video.name)
            else:
                se = parse_episode(video.name)
            if se:
                season, episode = se
                ep = self.app.db.query_one(
                    "SELECT * FROM episodes WHERE series_id=? AND season=? AND episode=?",
                    (series["id"], season, episode),
                )
            elif linked_episode_id and len(videos) == 1:
                ep = self.app.db.get_episode(linked_episode_id)
                season, episode = (ep["season"], ep["episode"]) if ep else (None, None)
            else:
                result.skipped.append(f"{video.name}: no S/E in name and not a single linked episode")
                continue
            if not ep:
                result.skipped.append(f"{video.name}: S{season:02d}E{episode:02d} not in library")
                continue

            season_dir = series_dir / f"Season {season:02d}"
            if self.cfg.rename:
                title = f" - {_sanitize(ep['title'])}" if ep.get("title") else ""
                base = f"{_sanitize(series['title'])} - S{season:02d}E{episode:02d}{title}"
                fname = base + video.suffix.lower()
            else:
                fname = video.name
            dst = season_dir / fname
            try:
                action = self._place(video, dst)
            except OSError as exc:
                result.errors.append(f"{video.name}: {exc}")
                continue
            self.app.db.set_episode_status(ep["id"], "downloaded", str(dst))
            result.imported.append(
                ImportedFile(
                    source=str(video), destination=str(dst), action=action,
                    season=season, episode=episode, episode_id=ep["id"],
                )
            )
            scan_dirs.add(str(season_dir))
        result.scan_paths = sorted(scan_dirs)
        return result

    # -- movie -------------------------------------------------------------- #
    def _import_movie(self, download: dict, videos: list[Path], result: ImportResult):
        movie = self.app.db.get_movie(download["movie_id"])
        if not movie:
            result.errors.append("download is not linked to a known movie")
            return result
        dest_root = self.root_local(movie["root_folder"], "movie")
        if not dest_root:
            result.errors.append(
                "no movie root folder resolves to the work context; configure one with configure_root_folder"
            )
            return result
        movie_dir = dest_root / _sanitize(movie["folder_name"] or movie["title"])
        # Largest video file is the feature; ignore extras.
        feature = max(videos, key=lambda p: p.stat().st_size)
        if self.cfg.rename:
            fname = _sanitize(movie["folder_name"] or movie["title"]) + feature.suffix.lower()
        else:
            fname = feature.name
        dst = movie_dir / fname
        try:
            action = self._place(feature, dst)
        except OSError as exc:
            result.errors.append(f"{feature.name}: {exc}")
            return result
        self.app.db.set_movie_status(movie["id"], "downloaded", str(dst))
        result.imported.append(
            ImportedFile(source=str(feature), destination=str(dst), action=action)
        )
        result.scan_paths = [str(movie_dir)]
        return result
