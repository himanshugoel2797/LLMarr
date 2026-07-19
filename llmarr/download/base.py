"""Download client interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from pydantic import BaseModel


class TorrentStatus(BaseModel):
    hash: str
    name: str
    state: str
    progress: float  # 0..1
    save_path: Optional[str] = None
    content_path: Optional[str] = None
    category: Optional[str] = None
    completed: bool = False
    dl_speed: int = 0  # bytes/sec
    eta: Optional[int] = None  # seconds (client may report a sentinel for "infinite")
    num_seeds: Optional[int] = None
    size: Optional[int] = None  # bytes
    ratio: Optional[float] = None


class DownloadClient(ABC):
    @abstractmethod
    def add(
        self,
        url: str,
        category: Optional[str] = None,
        save_path: Optional[str] = None,
    ) -> Optional[str]:
        """Add a torrent by magnet or .torrent URL. Returns the torrent hash if known."""

    @abstractmethod
    def status(self, torrent_hash: str) -> Optional[TorrentStatus]:
        ...

    @abstractmethod
    def list(self, category: Optional[str] = None) -> list[TorrentStatus]:
        ...

    @abstractmethod
    def remove(self, torrent_hash: str, delete_files: bool = False) -> None:
        ...

    @abstractmethod
    def test(self) -> dict:
        ...
