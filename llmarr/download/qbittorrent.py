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
    if not m:
        return None
    h = m.group(1)
    if len(h) == 40:  # already hex (v1 btih)
        return h.lower()
    try:  # 32-char base32 btih → hex, which is what qBittorrent reports
        import base64

        return base64.b32decode(h.upper()).hex()
    except Exception:  # noqa: BLE001
        return h.lower()


def _resolve_torrent(url: str):
    """Resolve an indexer/Prowlarr download URL, host-side, into something the
    download client can add without reaching that URL itself.

    Returns ("magnet", str) if it redirects to a magnet, ("file", bytes) if the
    .torrent can be fetched, else ("url", url) as a last resort.
    """
    import httpx

    try:
        with httpx.Client(follow_redirects=False, timeout=30) as client:
            resp = client.get(url)
            # Some indexers 30x-redirect a download link to a magnet.
            if resp.is_redirect:
                loc = resp.headers.get("location", "")
                if loc.startswith("magnet:"):
                    return ("magnet", loc)
                if loc:
                    from urllib.parse import urljoin

                    resp = client.get(urljoin(str(resp.url), loc), follow_redirects=True)
            if resp.status_code == 200 and resp.content[:1] == b"d":
                return ("file", resp.content)  # bencoded .torrent
    except Exception:  # noqa: BLE001 - fall back to handing the URL to the client
        pass
    return ("url", url)


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

        magnet = url if url.startswith("magnet:") else None
        if magnet is None:
            # The download client is often containerised (e.g. behind a VPN) and
            # can't reach the indexer/Prowlarr URL that LLMarr can. So resolve the
            # link ourselves: follow a redirect to a magnet, or fetch the .torrent
            # bytes and hand the FILE to qBittorrent instead of the URL.
            kind, payload = _resolve_torrent(url)
            if kind == "magnet":
                magnet = payload
            elif kind == "file":
                kwargs["torrent_files"] = payload
            else:
                kwargs["urls"] = payload  # last resort: let the client fetch it
        if magnet is not None:
            kwargs["urls"] = magnet

        result = self._client.torrents_add(**kwargs)
        if result != "Ok.":
            raise RuntimeError(f"qBittorrent rejected the torrent: {result!r}")

        # Prefer the deterministic magnet hash.
        h = magnet_hash(magnet or "")
        if h and self.status(h):
            return h

        # Otherwise poll for the newly-appeared torrent.
        for _ in range(20):
            time.sleep(0.5)
            new = [t for t in self._client.torrents_info() if t.hash not in before]
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
            # Only when a completed torrent has settled (an UP state) — NOT while
            # qBittorrent is still "moving"/"checking" files, which also report
            # progress 1.0 and would import from a mid-move path.
            completed=t.state in _COMPLETE_STATES,
            dl_speed=int(getattr(t, "dlspeed", 0) or 0),
            eta=getattr(t, "eta", None),
            num_seeds=getattr(t, "num_seeds", None),
            size=int(getattr(t, "size", 0) or 0),
            ratio=getattr(t, "ratio", None),
        )

    def status(self, torrent_hash: str) -> Optional[TorrentStatus]:
        if not torrent_hash:
            return None
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
