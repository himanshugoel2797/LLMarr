"""Download clients."""

from .base import DownloadClient, TorrentStatus
from .qbittorrent import QBittorrentClient


def get_client(cfg) -> DownloadClient:
    """Instantiate a download client from a DownloadClientConfig."""
    if cfg.type == "qbittorrent":
        return QBittorrentClient(cfg)
    raise ValueError(f"Unknown download client type: {cfg.type}")


__all__ = ["DownloadClient", "TorrentStatus", "QBittorrentClient", "get_client"]
