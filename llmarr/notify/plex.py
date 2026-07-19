"""Plex library refresh via plexapi.

After a grab lands, Plex needs to scan the library so the new file shows up. We
scan the specific section (and, when possible, only the changed directory) rather
than the whole server.
"""

from __future__ import annotations

from typing import Optional

from plexapi.server import PlexServer

from ..config import PlexConfig


class PlexNotifier:
    def __init__(self, cfg: PlexConfig):
        if not cfg.url or not cfg.token:
            raise ValueError("Plex URL and token must be configured.")
        self.cfg = cfg
        self._server: Optional[PlexServer] = None

    @property
    def server(self) -> PlexServer:
        if self._server is None:
            self._server = PlexServer(self.cfg.url, self.cfg.token)
        return self._server

    def scan(self, section_name: Optional[str] = None, path: Optional[str] = None) -> dict:
        section_name = section_name or self.cfg.tv_section
        section = self.server.library.section(section_name)
        if path:
            section.update(path=path)
        else:
            section.update()
        return {"ok": True, "section": section_name, "path": path}

    def libraries(self) -> list[dict]:
        """List library sections with their on-disk locations (for choosing root
        folders and section names)."""
        out = []
        for s in self.server.library.sections():
            out.append(
                {
                    "title": s.title,
                    "type": s.type,  # "show" | "movie" | "artist" | ...
                    "locations": list(getattr(s, "locations", []) or []),
                }
            )
        return out

    def test(self) -> dict:
        sections = [
            {"title": s.title, "type": s.type} for s in self.server.library.sections()
        ]
        return {
            "ok": True,
            "friendly_name": self.server.friendlyName,
            "version": self.server.version,
            "sections": sections,
        }
