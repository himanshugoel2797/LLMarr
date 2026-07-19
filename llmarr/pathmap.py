"""Translate a path between container namespaces using configured mappings.

The download client, Plex, and this server may each mount the same physical
directory at a different path. A :class:`PathMapping` group records those
equivalences; :func:`translate` rewrites a path from one context into another by
finding the group whose ``from`` entry is a prefix of the path and swapping in
the ``to`` entry's prefix.
"""

from __future__ import annotations

import posixpath
from typing import Optional

from .config import Config, PathMapping


def _normalize(path: str) -> str:
    # Work in POSIX terms — containers are Linux. Strip trailing slashes but keep
    # the leading one.
    path = path.replace("\\", "/")
    if len(path) > 1:
        path = path.rstrip("/")
    return path


def _is_prefix(prefix: str, path: str) -> bool:
    prefix = _normalize(prefix)
    path = _normalize(path)
    if prefix == path:
        return True
    return path.startswith(prefix + "/")


def translate(
    config: Config, path: str, from_context: str, to_context: str
) -> Optional[str]:
    """Return ``path`` as seen from ``to_context``.

    If the two contexts are identical the path is returned unchanged. When no
    mapping applies the result depends on ``config.single_host``: in single-host
    mode (the default, non-containerised case) the path passes through unchanged;
    otherwise ``None`` is returned so the caller can surface a missing-mapping
    error for the split-container deployment."""
    if from_context == to_context:
        return _normalize(path)

    path = _normalize(path)
    # Index mappings by group.
    groups: dict[str, dict[str, str]] = {}
    for m in config.path_mappings:
        groups.setdefault(m.group, {})[m.context] = m.path

    # Find the group whose from-entry is the longest matching prefix.
    best: Optional[tuple[int, str, str]] = None  # (prefix_len, from_path, to_path)
    for _group, ctxs in groups.items():
        src = ctxs.get(from_context)
        dst = ctxs.get(to_context)
        if not src or not dst:
            continue
        if _is_prefix(src, path):
            length = len(_normalize(src))
            if best is None or length > best[0]:
                best = (length, _normalize(src), _normalize(dst))

    if best is None:
        # No mapping connects these contexts for this path.
        return path if getattr(config, "single_host", False) else None
    _, src, dst = best
    remainder = path[len(src):].lstrip("/")
    return posixpath.join(dst, remainder) if remainder else dst


def contexts(config: Config) -> set[str]:
    return {m.context for m in config.path_mappings}
