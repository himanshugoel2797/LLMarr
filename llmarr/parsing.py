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

# --- absolute (anime) numbering -------------------------------------------- #
# Anime is released with absolute episode numbers, not SxxExx. The fansub
# convention is "Title - 12 [tags]"; also "Episode 12" / "Ep 12" / "E12".
# These are intentionally only used for series flagged as absolute-numbered, so a
# false positive can't affect ordinary TV.
_ABS_EPWORD = re.compile(r"\b(?:episode|ep)\.?[\s._]*(\d{1,4})\b", re.IGNORECASE)
_ABS_E = re.compile(r"(?:^|[\s._])E(\d{1,3})(?![\dp])", re.IGNORECASE)
# A dash used as a token separator (space/dot/underscore on its left), then the
# number, optional "v2" version, not followed by another digit or a 'p'
# (so " - 1080p" resolutions are excluded).
_ABS_DASH = re.compile(r"[\s._][-–][\s._]*(\d{1,4})(?:v\d+)?(?![\dp])", re.IGNORECASE)
# Batch/range: "(01-28)", "01~28", "E01-E12".
_ABS_RANGE = re.compile(
    r"(?:^|[\s._\[\(])E?(\d{1,4})[\s._]*[-~][\s._]*E?(\d{1,4})(?=[\s._\]\)v]|$)"
)
_BATCH = re.compile(r"\b(?:batch|complete)\b", re.IGNORECASE)


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


def parse_absolute_episode(title: str) -> Optional[int]:
    """Return the absolute episode number from an anime-style release/filename,
    e.g. ``[Group] Show - 12 [1080p]`` → 12. ``None`` if none is found."""
    for rx in (_ABS_EPWORD, _ABS_E, _ABS_DASH):
        matches = rx.findall(title)
        if matches:
            # The episode number is the last such token (name may contain others).
            return int(matches[-1])
    return None


# "Season 1-2" / "Seasons 1-3" is a span of seasons, not an episode range.
_SEASON_SPAN = re.compile(r"seasons?\s*[\[\(]?\s*$", re.IGNORECASE)


def parse_absolute_range(title: str) -> Optional[tuple[int, int]]:
    """Return (start, end) for an anime episode batch/range like ``(01-28)``.
    Ignores "Season N-M" spans, which mean seasons rather than episodes."""
    for m in _ABS_RANGE.finditer(title):
        start, end = int(m.group(1)), int(m.group(2))
        if not (0 < start < end and end - start <= 400):
            continue
        if _SEASON_SPAN.search(title[: m.start() + 1]):
            continue  # preceded by "Season" — a season span, not episodes
        return start, end
    return None


def is_batch(title: str) -> bool:
    return bool(_BATCH.search(title))


def matches_episode_absolute(title: str, episode: int) -> bool:
    """True if an anime release/pack covers this absolute episode number."""
    se = parse_episode(title)
    if se:  # some anime still use SxxExx; treat as season 1
        return se[0] == 1 and se[1] == episode
    rng = parse_absolute_range(title)
    if rng:
        return rng[0] <= episode <= rng[1]
    n = parse_absolute_episode(title)
    if n is not None:
        return n == episode
    return is_batch(title)


def title_matches_episode(title: str, season: int, episode: int, absolute: bool = False) -> bool:
    """Dispatch to absolute (anime) or standard SxxExx matching."""
    if absolute:
        return matches_episode_absolute(title, episode)
    return matches_episode(title, season, episode)
