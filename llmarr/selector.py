"""Score and filter releases according to the configured quality preferences.

This is intentionally a lightweight heuristic rather than Sonarr's full custom
formats: it rejects releases that violate hard constraints (ignored terms, min
seeders, size bounds, missing required terms) and ranks the rest by resolution
preference, preferred terms and seeder count.
"""

from __future__ import annotations

import re

from .config import QualityConfig
from .indexers.prowlarr import Release
from .parsing import parse_resolution


def _title_has(title: str, term: str) -> bool:
    return term.lower() in title.lower()


def _has_word(title: str, term: str) -> bool:
    """Whole-token match so a required/ignored term like 'ts' or 'cam' doesn't
    fire on 'Yellowjackets' / 'Camelot'."""
    return re.search(rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])", title.lower()) is not None


def passes(release: Release, q: QualityConfig) -> tuple[bool, str]:
    title = release.title
    for term in q.ignored_terms:
        if term and _has_word(title, term):
            return False, f"contains ignored term '{term}'"
    for term in q.required_terms:
        if term and not _has_word(title, term):
            return False, f"missing required term '{term}'"
    if release.seeders is not None and release.seeders < q.min_seeders:
        return False, f"seeders {release.seeders} < min {q.min_seeders}"
    if q.min_size_mb and release.size_mb and release.size_mb < q.min_size_mb:
        return False, f"size {release.size_mb:.0f}MB < min {q.min_size_mb}MB"
    if q.max_size_mb and release.size_mb and release.size_mb > q.max_size_mb:
        return False, f"size {release.size_mb:.0f}MB > max {q.max_size_mb}MB"
    return True, "ok"


def score(release: Release, q: QualityConfig) -> float:
    s = 0.0
    res = parse_resolution(release.title)
    if res and res in q.preferred_resolutions:
        # Higher rank for earlier entries in the preference list.
        s += (len(q.preferred_resolutions) - q.preferred_resolutions.index(res)) * 1000
    for term in q.prefer_terms:
        if term and _title_has(release.title, term):
            s += 100
    if release.seeders:
        s += min(release.seeders, 500)  # cap seeder influence
    return s


def rank(releases: list[Release], q: QualityConfig) -> list[tuple[Release, float, str]]:
    """Return releases that pass constraints, ranked best-first, with scores."""
    ranked = []
    for r in releases:
        ok, reason = passes(r, q)
        if ok:
            ranked.append((r, score(r, q), reason))
    ranked.sort(key=lambda t: t[1], reverse=True)
    return ranked


def best(releases: list[Release], q: QualityConfig) -> Release | None:
    ranked = rank(releases, q)
    return ranked[0][0] if ranked else None
