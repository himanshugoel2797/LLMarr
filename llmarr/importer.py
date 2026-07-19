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
from .parsing import parse_absolute_episode, parse_episode, parse_multi_episode

# Subtitle sidecars imported alongside their video (kept in step with the video's
# renamed base name, preserving any language/flag suffix like ".en" / ".forced").
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt"}


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

    # -- subtitle sidecars -------------------------------------------------- #
    def _sidecar_subs(self, video: Path) -> list[Path]:
        """Subtitle files sitting next to ``video`` that belong to it (same stem,
        optionally followed by a language/flag suffix)."""
        try:
            siblings = [p for p in video.parent.iterdir() if p.is_file()]
        except OSError:
            return []
        stem = video.stem
        out = []
        for p in siblings:
            if p.suffix.lower() not in SUBTITLE_EXTENSIONS:
                continue
            if p.stem == stem or p.stem.startswith(stem + "."):
                out.append(p)
        return out

    def _place_subs(self, video: Path, dst: Path, result: ImportResult,
                    season=None, episode=None, episode_id=None) -> None:
        """Hardlink/copy each subtitle sidecar next to the placed video, matching
        the video's (possibly renamed) base name and keeping its language suffix."""
        for sub in self._sidecar_subs(video):
            remainder = sub.name[len(video.stem):]  # e.g. ".en.srt" or ".srt"
            sub_dst = dst.with_name(dst.stem + remainder)
            try:
                action = self._place(sub, sub_dst)
            except OSError as exc:
                result.errors.append(f"{sub.name}: {exc}")
                continue
            result.imported.append(
                ImportedFile(source=str(sub), destination=str(sub_dst), action=action,
                             season=season, episode=episode, episode_id=episode_id)
            )

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
                pairs = [(1, n)] if n is not None else parse_multi_episode(video.name)
            else:
                # parse_multi_episode handles both single (S01E01) and
                # double/multi-episode files (S01E01E02 / S01E01-E02).
                pairs = parse_multi_episode(video.name)

            eps = []
            if pairs:
                for season, episode in pairs:
                    row = self.app.db.query_one(
                        "SELECT * FROM episodes WHERE series_id=? AND season=? AND episode=?",
                        (series["id"], season, episode),
                    )
                    if row:
                        eps.append(row)
                if not eps:
                    s0, e0 = pairs[0]
                    result.skipped.append(f"{video.name}: S{s0:02d}E{e0:02d} not in library")
                    continue
            elif linked_episode_id and len(videos) == 1:
                ep = self.app.db.get_episode(linked_episode_id)
                if ep:
                    eps = [ep]
            if not eps:
                result.skipped.append(
                    f"{video.name}: no S/E in name and not a single linked episode"
                )
                continue

            season = eps[0]["season"]
            season_dir = series_dir / f"Season {season:02d}"
            if self.cfg.rename:
                if len(eps) > 1:
                    span = f"S{season:02d}" + "".join(f"E{e['episode']:02d}" for e in eps)
                else:
                    span = f"S{season:02d}E{eps[0]['episode']:02d}"
                title = f" - {_sanitize(eps[0]['title'])}" if eps[0].get("title") else ""
                fname = f"{_sanitize(series['title'])} - {span}{title}" + video.suffix.lower()
            else:
                fname = video.name
            dst = season_dir / fname
            try:
                action = self._place(video, dst)
            except OSError as exc:
                result.errors.append(f"{video.name}: {exc}")
                continue
            # One physical file covers every episode it spans — mark them all.
            for ep in eps:
                self.app.db.set_episode_status(ep["id"], "downloaded", str(dst))
                result.imported.append(
                    ImportedFile(
                        source=str(video), destination=str(dst), action=action,
                        season=ep["season"], episode=ep["episode"], episode_id=ep["id"],
                    )
                )
            self._place_subs(video, dst, result, season=season,
                             episode=eps[0]["episode"], episode_id=eps[0]["id"])
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
        base = _sanitize(movie["folder_name"] or movie["title"])
        # A movie "pack" can hold several feature-length files (e.g. a trilogy).
        # Import every feature-sized file (>= half the largest), skipping extras.
        by_size = sorted(videos, key=lambda p: p.stat().st_size, reverse=True)
        largest = by_size[0].stat().st_size
        features = [p for p in by_size if p.stat().st_size >= largest * 0.5]

        primary_dst = None
        for idx, feature in enumerate(features):
            if self.cfg.rename:
                # The largest keeps the clean "Title (Year)" name; extra features
                # get a distinguishing suffix so they don't collide.
                suffix = "" if idx == 0 else f" - {_sanitize(feature.stem)}"
                fname = base + suffix + feature.suffix.lower()
            else:
                fname = feature.name
            dst = movie_dir / fname
            try:
                action = self._place(feature, dst)
            except OSError as exc:
                result.errors.append(f"{feature.name}: {exc}")
                continue
            if primary_dst is None:
                primary_dst = str(dst)
            result.imported.append(
                ImportedFile(source=str(feature), destination=str(dst), action=action)
            )
            self._place_subs(feature, dst, result)
        if primary_dst is None:
            return result
        self.app.db.set_movie_status(movie["id"], "downloaded", primary_dst)
        result.scan_paths = [str(movie_dir)]
        return result
