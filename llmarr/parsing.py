"""Parse release titles for season/episode numbers and quality tags."""

from __future__ import annotations

import re
from typing import Optional

# S01E02, s1e2, 1x02
_SXXEXX = re.compile(r"[Ss](\d{1,2})[\. _-]?[Ee](\d{1,3})")
_NxNN = re.compile(r"(?<![\dA-Za-z])(\d{1,2})[xX](\d{1,3})(?![\d])")
# Double/multi-episode files: S01E01E02, S01E01-E02, 1x01x02.
_MULTI_SXXEXX = re.compile(r"[Ss](\d{1,2})((?:[\. _-]?[Ee]\d{1,3}){2,})")
_MULTI_NxNN = re.compile(r"(?<![\dA-Za-z])(\d{1,2})((?:[xX]\d{1,3}){2,})(?![\d])")
# Whole-season packs: "Season 1", "S01" with no episode, "Complete". The boundary
# before S keeps audio tags like "DTS5.1" (T precedes S) from parsing as "Season 5".
_SEASON_ONLY = re.compile(r"(?<![A-Za-z0-9])[Ss](?:eason[ ._-]?)?(\d{1,2})(?![Ee\dxX])")

_RES = re.compile(r"\b(2160p|1080p|720p|480p|4k)\b", re.IGNORECASE)

# --- absolute (anime) numbering -------------------------------------------- #
# Anime is released with absolute episode numbers, not SxxExx. The fansub
# convention is "Title - 12 [tags]"; also "Episode 12" / "Ep 12" / "E12".
# These are intentionally only used for series flagged as absolute-numbered, so a
# false positive can't affect ordinary TV.
_ABS_EPWORD = re.compile(r"\b(?:episode|ep)\.?[\s._]*(\d{1,4})\b", re.IGNORECASE)
_ABS_E = re.compile(r"(?:^|[\s._])E(\d{1,3})(?![\dp])", re.IGNORECASE)
# A dash used as a token separator (space/dot/underscore on its left), then the
# number, optional "v2" version, not followed by another digit, a 'p'
# (resolutions) or ".5" (fractional/half specials).
_ABS_DASH = re.compile(r"[\s._][-–][\s._]*(\d{1,4})(?:v\d+)?(?![\dp])(?!\.\d)", re.IGNORECASE)


def _not_year(n: int) -> bool:
    return not (1900 <= n <= 2100)
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


def parse_multi_episode(title: str) -> list[tuple[int, int]]:
    """Return every (season, episode) a title names — handling double/multi-episode
    files like ``S01E01E02`` or ``S01E01-E02`` (→ [(1,1),(1,2)]). Falls back to the
    single-episode result, or ``[]`` if none is found."""
    m = _MULTI_SXXEXX.search(title)
    if m:
        season = int(m.group(1))
        nums = [int(n) for n in re.findall(r"[Ee](\d{1,3})", m.group(2))]
        return [(season, n) for n in nums]
    m = _MULTI_NxNN.search(title)
    if m:
        season = int(m.group(1))
        nums = [int(n) for n in re.findall(r"[xX](\d{1,3})", m.group(0))]
        return [(season, n) for n in nums]
    se = parse_episode(title)
    return [se] if se else []


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


# Ascending quality order for upgrade comparisons (G4). Higher index = better.
RESOLUTION_ORDER = ["480p", "720p", "1080p", "2160p"]


def resolution_rank(res: Optional[str]) -> int:
    """Numeric rank of a resolution for comparison; higher is better. An unknown
    or missing resolution ranks -1 (below every known one) so we never treat an
    unrecognised release as an upgrade target."""
    try:
        return RESOLUTION_ORDER.index(res)
    except ValueError:
        return -1


# --- pack layout: bundled sequels and specials ------------------------------ #
# A "complete series" pack routinely bundles more than the series that was
# grabbed: a differently-named sequel ("Amagami SS" + "Amagami SS+ Plus") and a
# pile of specials. Both are folded into the one series LLMarr grabbed for —
# sequels become later seasons, specials become season 0 — so the helpers below
# only need to answer "which show is this file from" and "is it a special".

# SP01, SP01-03, "Special 2", "OVA 3" — one special or a span of them.
_SPECIAL = re.compile(
    r"(?:^|[\s._\[\(-])(?:SP|Special|OVA)[\s._]*(\d{1,3})"
    r"(?:[\s._]*[-~][\s._]*(?:SP)?(\d{1,3}))?",
    re.IGNORECASE,
)

# Leading release-group tag: "[DB]", "(Group)".
_GROUP_TAG = re.compile(r"^\s*[\[\(][^\]\)]*[\]\)]\s*")
# Where the show name stops and the episode token starts: a dash separator
# followed by a number, optionally prefixed by SP/OVA/NCOP/NCED.
_SHOW_CUT = re.compile(
    r"[\s._]+[-–][\s._]*(?=(?:SP|OVA|Special|NC(?:OP|ED))?\d)", re.IGNORECASE
)


def parse_special(name: str) -> list[int]:
    """Return the special number(s) a filename names — ``SP01`` → ``[1]``,
    ``SP01-03`` → ``[1, 2, 3]``. Empty when it isn't a special."""
    m = _SPECIAL.search(name)
    if not m:
        return []
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else start
    if not start <= end <= start + 50:
        end = start
    return list(range(start, end + 1))


def show_group_key(name: str) -> str:
    """Normalised show-name prefix of a release filename, used to spot when one
    pack bundles several differently-named shows.

    ``[DB]Amagami SS+ Plus_-_03_(10bit_BD1080p_x265)`` → ``amagami ss+ plus``.
    """
    s = _GROUP_TAG.sub("", name)
    m = _SHOW_CUT.search(s)
    if m:
        s = s[: m.start()]
    s = re.sub(r"[._]+", " ", s)
    return re.sub(r"\s+", " ", s).strip(" -_").casefold()


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
        # Drop year-like values (a "- 2023" release-year token isn't an episode).
        nums = [n for n in (int(m) for m in rx.findall(title)) if _not_year(n)]
        if nums:
            return nums[-1]  # the episode number is the last such token
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
        if not _not_year(start) and not _not_year(end):
            continue  # a "2019-2020" year span, not an episode range
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


def matches_single_episode(title: str, season: int, episode: int, absolute: bool = False) -> bool:
    """Like :func:`title_matches_episode` but matches *only* a release for this
    one episode — season packs, batches and ranges are rejected. Used for quality
    upgrades, where replacing a whole pack in place would risk downgrading other
    episodes, so upgrades are limited to single-episode releases."""
    if absolute:
        if parse_absolute_range(title) or is_batch(title):
            return False
        se = parse_episode(title)
        if se:  # some anime still tag SxxExx — treat as season 1
            return se == (1, episode)
        return parse_absolute_episode(title) == episode
    return parse_episode(title) == (season, episode)
