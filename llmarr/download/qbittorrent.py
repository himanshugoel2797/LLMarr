"""qBittorrent download client (via the qbittorrent-api WebUI client).

qBittorrent's add endpoint does not return the new torrent's hash, so we:
  * parse the btih out of magnet links directly, or
  * diff the torrent list before/after the add for .torrent URLs.

This lets us track a grab through to completion and import.
"""

from __future__ import annotations

import re
import time
from typing import Optional

import qbittorrentapi

from ..config import DownloadClientConfig
from .base import DownloadClient, TorrentStatus

_MAGNET_HASH = re.compile(r"xt=urn:btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})")

_COMPLETE_STATES = {
    "uploading",
    "stalledUP",
    "pausedUP",
    "queuedUP",
    "forcedUP",
    "checkingUP",
}


def magnet_hash(url: str) -> Optional[str]:
    m = _MAGNET_HASH.search(url or "")
    return m.group(1).lower() if m else None


class QBittorrentClient(DownloadClient):
    def __init__(self, cfg: DownloadClientConfig):
        if not cfg.url:
            raise ValueError("qBittorrent URL must be configured.")
        self.cfg = cfg
        self._client = qbittorrentapi.Client(
            host=cfg.url,
            username=cfg.username or "",
            password=cfg.password or "",
            REQUESTS_ARGS={"timeout": 30},
        )

    def _login(self):
        self._client.auth_log_in()

    def add(
        self,
        url: str,
        category: Optional[str] = None,
        save_path: Optional[str] = None,
    ) -> Optional[str]:
        self._login()
        category = category or self.cfg.category
        save_path = save_path or self.cfg.save_path

        before = {t.hash for t in self._client.torrents_info()}

        kwargs: dict = {"category": category}
        if save_path:
            kwargs["save_path"] = save_path
        if url.startswith("magnet:"):
            kwargs["urls"] = url
        else:
            kwargs["urls"] = url  # qBittorrent will fetch .torrent from http(s) URLs
        result = self._client.torrents_add(**kwargs)
        if result != "Ok.":
            raise RuntimeError(f"qBittorrent rejected the torrent: {result!r}")

        # Prefer the deterministic magnet hash.
        h = magnet_hash(url)
        if h and self.status(h):
            return h

        # Otherwise poll for the newly-appeared torrent.
        for _ in range(10):
            time.sleep(0.5)
            after = self._client.torrents_info()
            new = [t for t in after if t.hash not in before]
            if new:
                return new[0].hash
        return h

    def _to_status(self, t) -> TorrentStatus:
        return TorrentStatus(
            hash=t.hash,
            name=t.name,
            state=t.state,
            progress=float(t.progress),
            save_path=getattr(t, "save_path", None),
            content_path=getattr(t, "content_path", None),
            category=getattr(t, "category", None),
            completed=(t.state in _COMPLETE_STATES) or float(t.progress) >= 1.0,
        )

    def status(self, torrent_hash: str) -> Optional[TorrentStatus]:
        self._login()
        infos = self._client.torrents_info(torrent_hashes=torrent_hash)
        for t in infos:
            if t.hash.lower() == torrent_hash.lower():
                return self._to_status(t)
        return None

    def list(self, category: Optional[str] = None) -> list[TorrentStatus]:
        self._login()
        kwargs = {}
        if category:
            kwargs["category"] = category
        return [self._to_status(t) for t in self._client.torrents_info(**kwargs)]

    def remove(self, torrent_hash: str, delete_files: bool = False) -> None:
        self._login()
        self._client.torrents_delete(
            delete_files=delete_files, torrent_hashes=torrent_hash
        )

    def test(self) -> dict:
        self._login()
        version = self._client.app_version()
        return {"ok": True, "version": version, "category": self.cfg.category}
