"""Guided setup/diagnostics: inspect the config and report an ordered checklist
with the exact next action, plus enumerations of the available options so an LLM
can walk a user through configuring LLMarr conversationally.
"""

from __future__ import annotations

from typing import Optional

# Static capability catalogue — what LLMarr can be configured with.
METADATA_PROVIDERS = [
    {
        "name": "tmdb",
        "media": ["tv", "movie"],
        "needs_api_key": True,
        "note": "The Movie Database — general TV & movies. Free key from themoviedb.org.",
        "configure": 'configure_metadata(provider="tmdb", tmdb_api_key="…")',
    },
    {
        "name": "jikan",
        "media": ["anime-tv", "anime-movie"],
        "needs_api_key": False,
        "note": "MyAnimeList (Jikan-compatible). No key. Absolute episode numbering. "
        "Base URL defaults to Tenrai; override with anime_api_url.",
        "configure": 'search_series(query, provider="jikan") / add_series(id, provider="jikan")',
    },
]
DOWNLOAD_CLIENT_TYPES = ["qbittorrent"]
AUTH_MODES = ["token", "oauth", "none"]
IMPORT_MODES = ["hardlink", "copy", "move"]


def build_status(
    app,
    plex_libraries: Optional[list] = None,
    connections: Optional[dict] = None,
) -> dict:
    c = app.config
    steps = []

    def add(id, title, done, detail, actions, required=True):
        steps.append({
            "id": id, "title": title, "done": done, "required": required,
            "detail": detail, "actions": actions,
        })

    # 1. Metadata
    tmdb_ready = bool(c.metadata.tmdb_api_key)
    meta_done = tmdb_ready or c.metadata.provider == "jikan"
    add(
        "metadata", "Metadata provider", meta_done,
        f"default provider={c.metadata.provider}; tmdb key {'set' if tmdb_ready else 'MISSING'}; "
        f"jikan needs no key (anime).",
        ['configure_metadata(provider="tmdb", tmdb_api_key="…")',
         'configure_metadata(provider="jikan")  # anime default, no key'],
    )

    # 2. Prowlarr
    prowlarr_done = bool(c.prowlarr.url and c.prowlarr.api_key)
    add(
        "prowlarr", "Prowlarr (indexer search)", prowlarr_done,
        f"url {'set' if c.prowlarr.url else 'MISSING'}, api_key "
        f"{'set' if c.prowlarr.api_key else 'MISSING'}.",
        ['configure_prowlarr(url="http://localhost:9696", api_key="…")'],
    )

    # 3. Download client
    dc_done = len(c.download_clients) > 0
    add(
        "download_client", "Download client (qBittorrent)", dc_done,
        f"configured: {list(c.download_clients)} default={c.default_download_client}",
        ['configure_download_client("qbit", url="http://localhost:8080", '
         'username="…", password="…", save_path="/downloads")'],
    )

    # 4. Plex
    plex_done = bool(c.plex.url and c.plex.token)
    add(
        "plex", "Plex connection", plex_done,
        f"url {'set' if c.plex.url else 'MISSING'}, token "
        f"{'linked' if c.plex.token else 'MISSING'}; sections tv={c.plex.tv_section!r} "
        f"movie={c.plex.movie_section!r}",
        ["plex_login_start()  then plex_login_poll()  # browser login",
         'configure_plex(url="…", token="…")  # or paste a token',
         "plex_discover_libraries()  # list sections + paths"],
    )

    # 5. Root folders
    rf_tv = [r for r in c.root_folders if r.media_type == "tv"]
    rf_movie = [r for r in c.root_folders if r.media_type == "movie"]
    rf_done = bool(rf_tv or rf_movie)
    add(
        "root_folders", "Library root folders", rf_done,
        f"tv={[r.path for r in rf_tv]} movie={[r.path for r in rf_movie]}",
        ['configure_root_folder("tv", "/path/to/tv", media_type="tv")',
         'configure_root_folder("movies", "/path/to/movies", media_type="movie")'],
    )

    # 6. Path mappings (only needed for split containers)
    split = not c.single_host
    pm_needed = split
    pm_done = (not pm_needed) or len(c.path_mappings) > 0
    add(
        "path_mappings", "Path mappings", pm_done,
        f"single_host={c.single_host} (true = paths shared, no mappings needed). "
        f"{len(c.path_mappings)} mapping(s) configured.",
        ['add_path_mapping(group, context, path)  # only if containers see different paths'],
        required=pm_needed,
    )

    required = [s for s in steps if s["required"]]
    done_count = sum(1 for s in required if s["done"])
    next_step = next((s for s in required if not s["done"]), None)

    detected = {}
    if plex_libraries is not None:
        detected["plex_libraries"] = plex_libraries
        # Suggest root folders for show/movie libraries not yet covered.
        if isinstance(plex_libraries, list):
            covered = {r.path for r in c.root_folders}
            suggestions = []
            for lib in plex_libraries:
                if lib.get("type") not in ("show", "movie"):
                    continue
                for loc in lib.get("locations", []):
                    if loc not in covered:
                        mt = "tv" if lib["type"] == "show" else "movie"
                        suggestions.append(
                            f'configure_root_folder("{lib["title"].lower().replace(" ", "-")}", '
                            f'"{loc}", media_type="{mt}")'
                        )
            if suggestions:
                detected["suggested_root_folders"] = suggestions

    return {
        "summary": f"{done_count}/{len(required)} required steps complete"
        + ("" if not next_step else f" — next: {next_step['title']}"),
        "next": next_step,
        "steps": steps,
        "options": {
            "metadata_providers": METADATA_PROVIDERS,
            "download_client_types": DOWNLOAD_CLIENT_TYPES,
            "auth_modes": AUTH_MODES,
            "import_modes": IMPORT_MODES,
        },
        "detected": detected,
        "connections": connections,  # populated when check_connections=True
        "recommended_order": [
            "metadata", "prowlarr", "download_client", "plex",
            "root_folders", "path_mappings",
        ],
    }
