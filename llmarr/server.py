"""LLMarr MCP server — tool surface for LLM-driven media automation.

Tools are grouped into: configuration, connection tests, metadata/library,
release search & grabbing, download tracking, Plex, and RSS/auto-grab. Every
setting the system needs can be written through these tools, so the whole stack
is configurable from a chat with the LLM.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .auth import generate_token
from .config import (
    ConfigStore,
    DownloadClientConfig,
    PathMapping,
    RootFolder,
)
from .core import App
from .db import Database
from .indexers.prowlarr import CAT_MOVIE, CAT_TV
from .rss import RssPoller

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("llmarr")


class _State:
    app: App
    poller: RssPoller


state = _State()


@asynccontextmanager
async def lifespan(server: FastMCP):
    store = ConfigStore()
    db = Database()
    state.app = App(store, db)
    state.poller = RssPoller(state.app)
    if store.config.rss.enabled:
        state.poller.start()
    log.info("LLMarr ready (config=%s, db=%s)", store.path, db.path)
    try:
        yield {"app": state.app}
    finally:
        await state.poller.stop()


mcp = FastMCP("llmarr", lifespan=lifespan)


def app() -> App:
    return state.app


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@mcp.tool()
def get_config() -> dict:
    """Return the current configuration with secrets redacted."""
    return app().store.redacted()


@mcp.tool()
def configure_metadata(
    tmdb_api_key: Optional[str] = None,
    provider: Optional[str] = None,
    language: Optional[str] = None,
    anime_api_url: Optional[str] = None,
) -> dict:
    """Set the default metadata provider and its settings. ``provider`` is
    ``tmdb`` (TV+movies, needs ``tmdb_api_key``) or ``jikan`` (anime, no key).
    ``anime_api_url`` overrides the Jikan-compatible anime API base URL (defaults
    to Tenrai, api.tenrai.org/v1)."""
    def _m(c):
        if provider is not None:
            c.metadata.provider = provider
        if tmdb_api_key is not None:
            c.metadata.tmdb_api_key = tmdb_api_key
        if language is not None:
            c.metadata.language = language
        if anime_api_url is not None:
            c.metadata.anime_api_url = anime_api_url
    app().store.mutate(_m)
    return app().store.redacted()["metadata"]


@mcp.tool()
def configure_prowlarr(
    url: Optional[str] = None,
    api_key: Optional[str] = None,
    indexer_ids: Optional[list[int]] = None,
) -> dict:
    """Configure the Prowlarr connection. ``indexer_ids`` optionally restricts
    searches to specific indexers (empty list = all)."""
    def _m(c):
        if url is not None:
            c.prowlarr.url = url
        if api_key is not None:
            c.prowlarr.api_key = api_key
        if indexer_ids is not None:
            c.prowlarr.indexer_ids = indexer_ids
    app().store.mutate(_m)
    return app().store.redacted()["prowlarr"]


@mcp.tool()
def configure_download_client(
    name: str,
    url: Optional[str] = None,
    type: str = "qbittorrent",
    username: Optional[str] = None,
    password: Optional[str] = None,
    category: Optional[str] = None,
    save_path: Optional[str] = None,
    make_default: bool = True,
) -> dict:
    """Add or update a download client. ``save_path`` is the download directory
    as the *client* sees it; use path mappings to translate for Plex."""
    def _m(c):
        existing = c.download_clients.get(name)
        cfg = existing or DownloadClientConfig(type=type)
        cfg.type = type
        if url is not None:
            cfg.url = url
        if username is not None:
            cfg.username = username
        if password is not None:
            cfg.password = password
        if category is not None:
            cfg.category = category
        if save_path is not None:
            cfg.save_path = save_path
        c.download_clients[name] = cfg
        if make_default or c.default_download_client is None:
            c.default_download_client = name
    app().store.mutate(_m)
    return app().store.redacted()["download_clients"][name]


@mcp.tool()
def configure_plex(
    url: Optional[str] = None,
    token: Optional[str] = None,
    tv_section: Optional[str] = None,
    movie_section: Optional[str] = None,
) -> dict:
    """Configure the Plex connection and library section names."""
    def _m(c):
        if url is not None:
            c.plex.url = url
        if token is not None:
            c.plex.token = token
        if tv_section is not None:
            c.plex.tv_section = tv_section
        if movie_section is not None:
            c.plex.movie_section = movie_section
    app().store.mutate(_m)
    return app().store.redacted()["plex"]


@mcp.tool()
def add_path_mapping(group: str, context: str, path: str) -> list[dict]:
    """Add one leg of a path equivalence. Entries sharing a ``group`` refer to
    the same physical directory as seen by different containers. ``context`` is a
    label like ``qbittorrent``, ``plex`` or ``local``.

    Example — the download volume seen three ways::

        add_path_mapping("dl", "qbittorrent", "/downloads")
        add_path_mapping("dl", "plex", "/data/torrents")
        add_path_mapping("dl", "local", "/mnt/media/dl")
    """
    def _m(c):
        c.path_mappings = [
            m for m in c.path_mappings if not (m.group == group and m.context == context)
        ]
        c.path_mappings.append(PathMapping(group=group, context=context, path=path))
    app().store.mutate(_m)
    return [m.model_dump() for m in app().config.path_mappings]


@mcp.tool()
def list_path_mappings() -> list[dict]:
    """List all configured path mappings."""
    return [m.model_dump() for m in app().config.path_mappings]


@mcp.tool()
def remove_path_mapping(group: str, context: Optional[str] = None) -> list[dict]:
    """Remove a path-mapping group, or a single context leg within it."""
    def _m(c):
        c.path_mappings = [
            m
            for m in c.path_mappings
            if not (m.group == group and (context is None or m.context == context))
        ]
    app().store.mutate(_m)
    return [m.model_dump() for m in app().config.path_mappings]


@mcp.tool()
def translate_path(path: str, from_context: str, to_context: str) -> dict:
    """Translate a path between two contexts using the configured mappings."""
    from . import pathmap

    result = pathmap.translate(app().config, path, from_context, to_context)
    return {"input": path, "from": from_context, "to": to_context, "result": result}


@mcp.tool()
def configure_root_folder(
    name: str, path: str, media_type: str = "tv", context: str = "local"
) -> list[dict]:
    """Register a library root folder (where a media type lives, per context)."""
    def _m(c):
        c.root_folders = [r for r in c.root_folders if r.name != name]
        c.root_folders.append(
            RootFolder(name=name, path=path, media_type=media_type, context=context)
        )
    app().store.mutate(_m)
    return [r.model_dump() for r in app().config.root_folders]


@mcp.tool()
def configure_quality(
    preferred_resolutions: Optional[list[str]] = None,
    required_terms: Optional[list[str]] = None,
    ignored_terms: Optional[list[str]] = None,
    prefer_terms: Optional[list[str]] = None,
    min_seeders: Optional[int] = None,
    min_size_mb: Optional[int] = None,
    max_size_mb: Optional[int] = None,
) -> dict:
    """Update the release-selection preferences used for ranking and auto-grab."""
    def _m(c):
        q = c.quality
        if preferred_resolutions is not None:
            q.preferred_resolutions = preferred_resolutions
        if required_terms is not None:
            q.required_terms = required_terms
        if ignored_terms is not None:
            q.ignored_terms = ignored_terms
        if prefer_terms is not None:
            q.prefer_terms = prefer_terms
        if min_seeders is not None:
            q.min_seeders = min_seeders
        if min_size_mb is not None:
            q.min_size_mb = min_size_mb
        if max_size_mb is not None:
            q.max_size_mb = max_size_mb
    app().store.mutate(_m)
    return app().config.quality.model_dump()


@mcp.tool()
def configure_import(
    enabled: Optional[bool] = None,
    mode: Optional[str] = None,
    rename: Optional[bool] = None,
    work_context: Optional[str] = None,
    min_video_mb: Optional[int] = None,
) -> dict:
    """Configure how completed downloads are imported into the Plex library.
    ``mode`` is hardlink|copy|move; ``work_context`` is the path context LLMarr
    itself can read/write (where linking happens — must be reachable and on the
    same filesystem as the library root for hardlinks)."""
    def _m(c):
        i = c.importer
        if enabled is not None:
            i.enabled = enabled
        if mode is not None:
            i.mode = mode
        if rename is not None:
            i.rename = rename
        if work_context is not None:
            i.work_context = work_context
        if min_video_mb is not None:
            i.min_video_mb = min_video_mb
    app().store.mutate(_m)
    return app().config.importer.model_dump()


@mcp.tool()
def configure_rss(
    enabled: Optional[bool] = None,
    interval_minutes: Optional[int] = None,
    auto_grab: Optional[bool] = None,
) -> dict:
    """Configure the background RSS/auto-grab loop and (re)start or stop it."""
    def _m(c):
        if enabled is not None:
            c.rss.enabled = enabled
        if interval_minutes is not None:
            c.rss.interval_minutes = interval_minutes
        if auto_grab is not None:
            c.rss.auto_grab = auto_grab
    app().store.mutate(_m)
    if app().config.rss.enabled:
        state.poller.start()
    return state.poller.status()


# --------------------------------------------------------------------------- #
# Server / auth / deployment mode
# --------------------------------------------------------------------------- #
@mcp.tool()
def configure_server(
    single_host: Optional[bool] = None,
    require_auth: Optional[bool] = None,
    auth_mode: Optional[str] = None,
    public_url: Optional[str] = None,
    allowed_hosts: Optional[list[str]] = None,
    allowed_origins: Optional[list[str]] = None,
) -> dict:
    """Set deployment mode. ``single_host=true`` (default) means LLMarr,
    qBittorrent and Plex share the same filesystem paths, so no path mappings are
    needed; set it false for a split-container setup and define path mappings.
    ``auth_mode`` is ``token`` (static bearer, default), ``oauth`` (OAuth 2.1 +
    PKCE, required by claude.ai custom connectors / mobile apps), or ``none``.
    ``public_url`` is the external base URL (e.g. https://arr.example.com) used to
    build OAuth endpoints; leave unset to derive it from the request.
    ``allowed_hosts`` lists external hostnames to trust for the Host-header check
    behind a tunnel/proxy. All take effect on the next HTTP server restart."""
    if auth_mode is not None and auth_mode not in ("token", "oauth", "none"):
        return {"error": "auth_mode must be one of: token, oauth, none"}
    def _m(c):
        if single_host is not None:
            c.single_host = single_host
        if require_auth is not None:
            c.server.require_auth = require_auth
        if auth_mode is not None:
            c.server.auth_mode = auth_mode
            if auth_mode == "none":
                c.server.require_auth = False
            else:
                c.server.require_auth = True
        if public_url is not None:
            c.server.public_url = public_url
        if allowed_hosts is not None:
            c.server.allowed_hosts = allowed_hosts
        if allowed_origins is not None:
            c.server.allowed_origins = allowed_origins
    app().store.mutate(_m)
    c = app().config
    return {
        "single_host": c.single_host,
        "require_auth": c.server.require_auth,
        "auth_mode": c.server.auth_mode,
        "public_url": c.server.public_url,
        "allowed_hosts": c.server.allowed_hosts,
        "allowed_origins": c.server.allowed_origins,
    }


@mcp.tool()
def oauth_info() -> dict:
    """Show the OAuth endpoint URLs to expect once the server runs in ``oauth``
    mode, for adding LLMarr as a claude.ai custom connector. Requires
    ``public_url`` (or knowing your external URL). The connector URL is the
    ``resource`` value; approve access on the authorize page with your token."""
    sc = app().config.server
    base = (sc.public_url or "https://<your-public-url>").rstrip("/")
    return {
        "auth_mode": sc.auth_mode,
        "connector_url": base + "/mcp",
        "issuer": base,
        "authorization_endpoint": base + "/authorize",
        "token_endpoint": base + "/token",
        "registration_endpoint": base + "/register",
        "protected_resource_metadata": base + "/.well-known/oauth-protected-resource",
        "note": "Enter your LLMarr auth token on the authorize page to approve.",
    }


@mcp.tool()
def get_auth_token() -> dict:
    """Reveal the current HTTP bearer token (for configuring your MCP client).
    Returns null if none is set — one is generated automatically on first HTTP
    start. Only the TV/torrent stack is behind this; stdio needs no token."""
    token = app().config.server.auth_token
    return {
        "auth_token": token,
        "require_auth": app().config.server.require_auth,
        "configured": bool(token),
    }


@mcp.tool()
def set_auth_token(token: Optional[str] = None) -> dict:
    """Set the HTTP bearer token to a specific value, or generate a fresh one if
    omitted. Returns the token. Takes effect on the next HTTP server restart."""
    new = token or generate_token()
    app().store.mutate(lambda c: setattr(c.server, "auth_token", new))
    return {"auth_token": new}


@mcp.tool()
def rotate_auth_token() -> dict:
    """Generate a new random HTTP bearer token, invalidating the old one. Takes
    effect on the next HTTP server restart."""
    return set_auth_token(None)


# --------------------------------------------------------------------------- #
# Connection tests
# --------------------------------------------------------------------------- #
@mcp.tool()
async def test_connections() -> dict:
    """Test connectivity to every configured service (metadata, Prowlarr,
    download client, Plex) and report status for each."""
    import asyncio

    out: dict = {}
    # Metadata
    try:
        await app().provider().search_series("test")
        out["metadata"] = {"ok": True, "provider": app().config.metadata.provider}
    except Exception as exc:  # noqa: BLE001
        out["metadata"] = {"ok": False, "error": str(exc)}
    # Prowlarr
    try:
        out["prowlarr"] = await app().prowlarr().test()
    except Exception as exc:  # noqa: BLE001
        out["prowlarr"] = {"ok": False, "error": str(exc)}
    # Download clients
    out["download_clients"] = {}
    for name in app().config.download_clients:
        try:
            out["download_clients"][name] = await asyncio.to_thread(
                app().download_client(name).test
            )
        except Exception as exc:  # noqa: BLE001
            out["download_clients"][name] = {"ok": False, "error": str(exc)}
    # Plex
    try:
        out["plex"] = await asyncio.to_thread(app().plex().test)
    except Exception as exc:  # noqa: BLE001
        out["plex"] = {"ok": False, "error": str(exc)}
    return out


# --------------------------------------------------------------------------- #
# Metadata / library
# --------------------------------------------------------------------------- #
@mcp.tool()
async def search_series(query: str, provider: Optional[str] = None) -> list[dict]:
    """Search for TV series matching ``query``. ``provider`` overrides the default
    metadata source — use ``"jikan"`` for anime (MyAnimeList, no key) or
    ``"tmdb"`` for general TV. Each result carries its own ``provider``/
    ``provider_id`` to pass to add_series."""
    results = await app().provider(provider).search_series(query)
    return [r.model_dump() for r in results]


@mcp.tool()
async def add_series(
    provider_id: str,
    monitored: bool = True,
    quality_profile: Optional[str] = None,
    root_folder: Optional[str] = None,
    seasons: Optional[list[int]] = None,
    provider: Optional[str] = None,
) -> dict:
    """Add a series to the library by its metadata provider id, fetching its full
    episode list. Pass the same ``provider`` used to find it (e.g. ``"jikan"`` for
    an anime from MyAnimeList). ``seasons`` optionally limits which seasons are
    monitored for auto-grab (default: all). Note: anime is modelled as season 1
    with absolute episode numbers."""
    return await app().add_series(
        provider_id,
        monitored=monitored,
        quality_profile=quality_profile,
        root_folder=root_folder,
        seasons=seasons,
        provider=provider,
    )


@mcp.tool()
def list_series() -> list[dict]:
    """List all series in the library."""
    return app().db.list_series()


@mcp.tool()
def get_series(series_id: int, include_episodes: bool = False) -> dict:
    """Get one series, optionally with its episode list and a status summary."""
    series = app().db.get_series(series_id)
    if not series:
        return {"error": f"No series with id {series_id}"}
    episodes = app().db.list_episodes(series_id)
    summary = {"total": len(episodes)}
    for e in episodes:
        summary[e["status"]] = summary.get(e["status"], 0) + 1
    result = dict(series)
    result["episode_summary"] = summary
    if include_episodes:
        result["episodes"] = episodes
    return result


@mcp.tool()
def list_episodes(
    series_id: int, status: Optional[str] = None, monitored_only: bool = False
) -> list[dict]:
    """List episodes of a series, optionally filtered by status
    (missing|grabbed|downloaded) and monitored flag."""
    return app().db.list_episodes(
        series_id, status=status, monitored=True if monitored_only else None
    )


@mcp.tool()
def set_monitored(series_id: int, monitored: bool, season: Optional[int] = None) -> dict:
    """Monitor/unmonitor a whole series or a single season."""
    app().db.set_monitored(series_id, monitored, season=season)
    return get_series(series_id)


@mcp.tool()
def remove_series(series_id: int) -> dict:
    """Remove a series (and its episodes) from the library."""
    series = app().db.get_series(series_id)
    if not series:
        return {"error": f"No series with id {series_id}"}
    app().db.delete_series(series_id)
    return {"removed": series_id, "title": series["title"]}


# --------------------------------------------------------------------------- #
# Movies
# --------------------------------------------------------------------------- #
@mcp.tool()
async def search_movies(query: str, provider: Optional[str] = None) -> list[dict]:
    """Search for movies matching ``query``. ``provider`` overrides the default —
    ``"jikan"`` for anime films (MyAnimeList, no key), ``"tmdb"`` otherwise."""
    results = await app().provider(provider).search_movies(query)
    return [r.model_dump() for r in results]


@mcp.tool()
async def add_movie(
    provider_id: str,
    monitored: bool = True,
    quality_profile: Optional[str] = None,
    root_folder: Optional[str] = None,
    provider: Optional[str] = None,
) -> dict:
    """Add a movie to the library by its metadata provider id. Pass the same
    ``provider`` used to find it. A monitored movie is auto-grabbed by the RSS
    poller while it is still missing."""
    return await app().add_movie(
        provider_id,
        monitored=monitored,
        quality_profile=quality_profile,
        root_folder=root_folder,
        provider=provider,
    )


@mcp.tool()
def list_movies() -> list[dict]:
    """List all movies in the library."""
    return app().db.list_movies()


@mcp.tool()
def get_movie(movie_id: int) -> dict:
    """Get one movie from the library."""
    movie = app().db.get_movie(movie_id)
    return movie or {"error": f"No movie with id {movie_id}"}


@mcp.tool()
def set_movie_monitored(movie_id: int, monitored: bool) -> dict:
    """Monitor/unmonitor a movie for auto-grab."""
    if not app().db.get_movie(movie_id):
        return {"error": f"No movie with id {movie_id}"}
    app().db.set_movie_monitored(movie_id, monitored)
    return app().db.get_movie(movie_id)


@mcp.tool()
def remove_movie(movie_id: int) -> dict:
    """Remove a movie from the library."""
    movie = app().db.get_movie(movie_id)
    if not movie:
        return {"error": f"No movie with id {movie_id}"}
    app().db.delete_movie(movie_id)
    return {"removed": movie_id, "title": movie["title"]}


@mcp.tool()
async def search_movie_releases(movie_id: int, apply_quality: bool = True) -> list[dict]:
    """Search Prowlarr for releases of a library movie (by title + year)."""
    movie = app().db.get_movie(movie_id)
    if not movie:
        return [{"error": f"No movie with id {movie_id}"}]
    query = f"{movie['title']} {movie['year']}" if movie["year"] else movie["title"]
    return await app().search_releases(
        query, categories=[CAT_MOVIE], apply_quality=apply_quality
    )


@mcp.tool()
async def grab_movie(movie_id: int, client_name: Optional[str] = None) -> dict:
    """Auto-pick the best available release for a library movie and grab it."""
    movie = app().db.get_movie(movie_id)
    if not movie:
        return {"error": f"No movie with id {movie_id}"}
    releases = await search_movie_releases(movie_id)
    if not releases or "error" in releases[0]:
        return {"error": "No releases found", "movie": movie["title"]}
    best = releases[0]
    grab = await app().grab(
        best["grab_url"],
        title=best["title"],
        movie_id=movie_id,
        client_name=client_name,
    )
    return {"picked": best, **grab}


# --------------------------------------------------------------------------- #
# Release search & grabbing
# --------------------------------------------------------------------------- #
@mcp.tool()
async def search_releases(
    query: str,
    media_type: str = "tv",
    apply_quality: bool = True,
    indexer_ids: Optional[list[int]] = None,
) -> list[dict]:
    """Search Prowlarr for torrent releases. When ``apply_quality`` is true,
    results are filtered by the configured quality rules and ranked best-first."""
    cats = [CAT_MOVIE] if media_type == "movie" else [CAT_TV]
    return await app().search_releases(
        query, categories=cats, indexer_ids=indexer_ids, apply_quality=apply_quality
    )


@mcp.tool()
async def search_episode_releases(
    series_id: int, season: int, episode: int, apply_quality: bool = True
) -> dict:
    """Search for releases of a specific episode of a library series and return
    the ones that match that episode (single-episode or season pack). Anime
    series (absolute numbering) are queried and matched by absolute episode
    number instead of SxxExx."""
    from .parsing import title_matches_episode

    series = app().db.get_series(series_id)
    if not series:
        return {"error": f"No series with id {series_id}"}
    absolute = bool(series.get("absolute_numbering"))
    if absolute:
        query = f"{series['title']} {episode:02d}"
    else:
        query = f"{series['title']} S{season:02d}E{episode:02d}"
    all_releases = await app().search_releases(
        query, categories=[CAT_TV], apply_quality=apply_quality
    )
    matched = [
        r for r in all_releases if title_matches_episode(r["title"], season, episode, absolute)
    ]
    return {"query": query, "matched": matched, "other": [r for r in all_releases if r not in matched]}


@mcp.tool()
async def grab_release(
    grab_url: str,
    title: str = "manual grab",
    series_id: Optional[int] = None,
    episode_id: Optional[int] = None,
    movie_id: Optional[int] = None,
    client_name: Optional[str] = None,
    category: Optional[str] = None,
    save_path: Optional[str] = None,
) -> dict:
    """Send a release to the download client. ``grab_url`` may be a magnet link,
    a .torrent URL, or a Prowlarr download URL — this is how a user manually
    grabs an arbitrary torrent. Optionally link it to a library series/episode or
    movie so it is marked grabbed and imported into Plex on completion."""
    return await app().grab(
        grab_url,
        title=title,
        series_id=series_id,
        episode_id=episode_id,
        movie_id=movie_id,
        client_name=client_name,
        category=category,
        save_path=save_path,
    )


@mcp.tool()
async def grab_episode(
    series_id: int, season: int, episode: int, client_name: Optional[str] = None
) -> dict:
    """Auto-pick the best available release for one episode and grab it."""
    result = await search_episode_releases(series_id, season, episode)
    if "error" in result:
        return result
    if not result["matched"]:
        return {"error": "No matching releases found", "query": result["query"]}
    best = result["matched"][0]
    ep = app().db.query_one(
        "SELECT id FROM episodes WHERE series_id=? AND season=? AND episode=?",
        (series_id, season, episode),
    )
    grab = await app().grab(
        best["grab_url"],
        title=best["title"],
        series_id=series_id,
        episode_id=ep["id"] if ep else None,
        client_name=client_name,
    )
    return {"picked": best, **grab}


# --------------------------------------------------------------------------- #
# Download tracking
# --------------------------------------------------------------------------- #
@mcp.tool()
def list_downloads(status: Optional[str] = None) -> list[dict]:
    """List grabs recorded by LLMarr, optionally filtered by status."""
    return app().db.list_downloads(status=status)


@mcp.tool()
async def download_status(download_id: int) -> dict:
    """Get live status from the download client for a recorded grab."""
    import asyncio

    d = app().db.get_download(download_id)
    if not d:
        return {"error": f"No download with id {download_id}"}
    if not d["torrent_hash"]:
        return {"download": d, "live": None}
    st = await asyncio.to_thread(app().download_client(d["client"]).status, d["torrent_hash"])
    return {"download": d, "live": st.model_dump() if st else None}


@mcp.tool()
async def refresh_downloads() -> list[dict]:
    """Poll all active grabs, import completed ones into the library
    (hardlink/copy/move) and trigger a Plex scan. Returns what changed."""
    return await app().refresh_downloads()


@mcp.tool()
async def import_download(download_id: int, notify: bool = True) -> dict:
    """Manually (re)run the import for a completed download — useful after fixing
    a path mapping or root folder. Hardlinks/copies/moves its files into the
    library and, if ``notify``, scans Plex."""
    import asyncio

    d = app().db.get_download(download_id)
    if not d:
        return {"error": f"No download with id {download_id}"}
    content = d["save_path"]
    if d["torrent_hash"]:
        st = await asyncio.to_thread(
            app().download_client(d["client"]).status, d["torrent_hash"]
        )
        if st and (st.content_path or st.save_path):
            content = st.content_path or st.save_path
    result: dict = {"download_id": download_id, "notified": False}
    section = (
        app().config.plex.movie_section
        if d.get("movie_id")
        else app().config.plex.tv_section
    )
    await app()._import_and_notify(d, content, section, result, notify)
    return result


@mcp.tool()
async def remove_download(download_id: int, delete_files: bool = False) -> dict:
    """Remove a grab from the download client and mark it removed."""
    import asyncio

    d = app().db.get_download(download_id)
    if not d:
        return {"error": f"No download with id {download_id}"}
    if d["torrent_hash"]:
        await asyncio.to_thread(
            app().download_client(d["client"]).remove, d["torrent_hash"], delete_files
        )
    app().db.set_download_status(download_id, "removed")
    return {"removed": download_id}


# --------------------------------------------------------------------------- #
# Plex
# --------------------------------------------------------------------------- #
@mcp.tool()
async def scan_plex(section: Optional[str] = None, path: Optional[str] = None) -> dict:
    """Trigger a Plex library scan. ``path`` (in Plex's own namespace) narrows
    the scan to one directory."""
    import asyncio

    return await asyncio.to_thread(app().plex().scan, section, path)


# --------------------------------------------------------------------------- #
# RSS / auto-grab
# --------------------------------------------------------------------------- #
@mcp.tool()
def rss_status() -> dict:
    """Show the background auto-grab poller's status and last run result."""
    return state.poller.status()


@mcp.tool()
async def rss_poll_now() -> dict:
    """Run the RSS/auto-grab poll immediately (search monitored series for
    missing episodes and grab/collect candidates), plus refresh downloads."""
    return await state.poller.poll_once()


def run() -> None:
    mcp.run()
