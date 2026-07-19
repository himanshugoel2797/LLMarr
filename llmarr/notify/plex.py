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

    def show_episodes(self, rating_key=None, title=None, section=None) -> list[tuple]:
        """Return (season, episode) pairs Plex already has for a show, found by
        rating key or (fallback) by title within the TV section."""
        show = None
        if rating_key:
            try:
                show = self.server.fetchItem(int(rating_key))
            except Exception:  # noqa: BLE001
                show = None
        if show is None and title:
            try:
                sec = self.server.library.section(section or self.cfg.tv_section)
                matches = sec.search(title=title, libtype="show")
                show = matches[0] if matches else None
            except Exception:  # noqa: BLE001
                show = None
        if show is None:
            return []
        out = []
        for ep in show.episodes():
            s = getattr(ep, "seasonNumber", None)
            if s is None:
                s = getattr(ep, "parentIndex", None)
            i = getattr(ep, "index", None)
            if i is not None:
                out.append((s, i))
        return out

    def catalog(self) -> list[dict]:
        """Enumerate every show/movie already in the Plex libraries with whatever
        external ids Plex knows (tmdb/tvdb/imdb/anidb/…), for importing the
        existing collection into LLMarr."""
        items = []
        for section in self.server.library.sections():
            if section.type not in ("show", "movie"):
                continue
            for it in section.all():
                guids = {}
                for g in getattr(it, "guids", None) or []:
                    gid = getattr(g, "id", "") or ""
                    if "://" in gid:
                        k, v = gid.split("://", 1)
                        guids[k] = v
                items.append({
                    "type": section.type,
                    "section": section.title,
                    "title": it.title,
                    "year": getattr(it, "year", None),
                    "rating_key": str(it.ratingKey),
                    "guids": guids,
                })
        return items

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
