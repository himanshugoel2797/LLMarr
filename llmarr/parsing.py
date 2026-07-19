"""Parse release titles for season/episode numbers and quality tags."""

from __future__ import annotations

import re
from typing import Optional

# S01E02, s1e2, 1x02
_SXXEXX = re.compile(r"[Ss](\d{1,2})[\. _-]?[Ee](\d{1,3})")
_NxNN = re.compile(r"(?<![\dA-Za-z])(\d{1,2})[xX](\d{1,3})(?![\d])")
# Whole-season packs: "Season 1", "S01" with no episode, "Complete"
_SEASON_ONLY = re.compile(r"[Ss](?:eason[ ._-]?)?(\d{1,2})(?![Ee\dxX])")

_RES = re.compile(r"\b(2160p|1080p|720p|480p|4k)\b", re.IGNORECASE)


def parse_episode(title: str) -> Optional[tuple[int, int]]:
    """Return (season, episode) if the title names a single episode."""
    m = _SXXEXX.search(title)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _NxNN.search(title)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def parse_season_pack(title: str) -> Optional[int]:
    """Return the season number if the title looks like a full-season pack."""
    if parse_episode(title):
        return None
    m = _SEASON_ONLY.search(title)
    if m:
        return int(m.group(1))
    if re.search(r"\bcomplete\b", title, re.IGNORECASE):
        # Ambiguous complete series pack — caller decides.
        return None
    return None


def parse_resolution(title: str) -> Optional[str]:
    m = _RES.search(title)
    if not m:
        return None
    val = m.group(1).lower()
    return "2160p" if val == "4k" else val


def matches_episode(title: str, season: int, episode: int) -> bool:
    """True if ``title`` covers this specific episode (single ep or its season pack)."""
    se = parse_episode(title)
    if se:
        return se == (season, episode)
    pack = parse_season_pack(title)
    return pack == season
