"""LLMarr MCP server — tool surface for LLM-driven media automation.

Tools are grouped into: configuration, connection tests, metadata/library,
release search & grabbing, download tracking, Plex, and RSS/auto-grab. Every
setting the system needs can be written through these tools, so the whole stack
is configurable from a chat with the LLM.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Literal, Optional

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


INSTRUCTIONS = """\
LLMarr replicates Sonarr/Radarr-style media automation over MCP: pull metadata
(TMDB for TV/movies, Jikan/MyAnimeList for anime), search torrents via Prowlarr,
grab with qBittorrent, hardlink-import into an organised library, and notify Plex.

When configuring LLMarr or diagnosing what's missing, ALWAYS call `setup_status`
first. It returns an ordered checklist (done/pending) with the exact next tool to
call, enumerates the available metadata providers / download-client types / auth
modes, and — once Plex is linked — lists the detected libraries with suggested
root-folder commands. Walk the user through the pending steps in order.

Recommended flow: configure_metadata -> configure_prowlarr ->
configure_download_client -> link Plex (plex_login_start then plex_login_poll, or
configure_plex with a token) -> plex_discover_libraries -> configure_root_folder
per library -> test_connections. Then search_series/add_series (pass
provider="jikan" for anime), and monitored items auto-grab, import and scan Plex.
For anime, series use absolute episode numbers. Prefer the plex.tv browser login
over asking the user for a raw token."""

mcp = FastMCP("llmarr", instructions=INSTRUCTIONS, lifespan=lifespan)


def app() -> App:
    return state.app


# --------------------------------------------------------------------------- #
# Uniform error contract: every tool returns {"error", "hint"} on failure instead
# of surfacing a raw exception. `@tool` = `@mcp.tool()` composed with this guard.
# --------------------------------------------------------------------------- #
def _as_error(exc: Exception) -> dict:
    import httpx

    msg = str(exc) or exc.__class__.__name__
    hint = None
    module = exc.__class__.__module__ or ""
    if isinstance(exc, ValueError):
        hint = "A required setting is missing — run setup_status or the relevant configure_* tool."
    elif isinstance(exc, httpx.HTTPError):
        hint = "An external service (metadata/Prowlarr) is unreachable — check its URL/key with test_connections."
    elif module.startswith("plexapi") or "plex" in module:
        hint = "Plex request failed — verify the Plex URL/token (plex_login_start) and that Plex is running."
    elif "qbittorrent" in module:
        hint = "qBittorrent request failed — check the client URL/credentials with test_connections."
    return {"error": msg, **({"hint": hint} if hint else {})}


def _guard(fn):
    import functools
    import inspect

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def aw(*a, **k):
            try:
                return await fn(*a, **k)
            except Exception as exc:  # noqa: BLE001 - uniform error contract
                return _as_error(exc)
        return aw

    @functools.wraps(fn)
    def sw(*a, **k):
        try:
            return fn(*a, **k)
        except Exception as exc:  # noqa: BLE001
            return _as_error(exc)
    return sw


def tool(fn):
    """Register an MCP tool whose exceptions become {"error", "hint"} dicts."""
    return mcp.tool()(_guard(fn))


def _set_opt(obj, attr: str, value) -> None:
    """Partial-update helper for optional fields: ``None`` leaves the value
    unchanged, an empty string clears it (sets ``None``), anything else sets it."""
    if value is not None:
        setattr(obj, attr, None if value == "" else value)


def _set_or_default(obj, attr: str, value) -> None:
    """Partial-update helper for NON-optional fields (which can't hold ``None``):
    ``None`` leaves the value unchanged, an empty string resets it to the model's
    declared default, anything else sets it. Keeps "" consistently meaning
    "clear" across tools without letting a field become an invalid empty value."""
    if value is None:
        return
    if value == "":
        setattr(obj, attr, type(obj).model_fields[attr].default)
    else:
        setattr(obj, attr, value)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@tool
async def setup_status(check_connections: bool = False) -> dict:
    """Guided setup & diagnostics — CALL THIS FIRST when configuring LLMarr or
    figuring out what's missing. Returns an ordered checklist (each step marked
    done/pending with the exact tool to call next), enumerations of the available
    metadata providers / download-client types / auth & import modes, and — when
    Plex is linked — the detected libraries with suggested root-folder commands.
    Set ``check_connections=true`` to also live-test each configured service."""
    import asyncio

    from . import setup as setupmod

    plex_libraries = None
    cfg = app().config
    if cfg.plex.url and cfg.plex.token:
        try:
            plex_libraries = await asyncio.to_thread(app().plex().libraries)
        except Exception as exc:  # noqa: BLE001
            plex_libraries = {"error": str(exc)}
    conns = await test_connections() if check_connections else None
    return setupmod.build_status(app(), plex_libraries=plex_libraries, connections=conns)


@tool
def get_config() -> dict:
    """Return the current configuration with secrets redacted."""
    return app().store.redacted()


@tool
def configure_metadata(
    tmdb_api_key: Optional[str] = None,
    provider: Optional[Literal["tmdb", "jikan"]] = None,
    language: Optional[str] = None,
    anime_api_url: Optional[str] = None,
) -> dict:
    """Set the default metadata provider and its settings. ``provider`` is
    ``tmdb`` (TV+movies, needs ``tmdb_api_key``) or ``jikan`` (anime, no key).
    ``anime_api_url`` overrides the Jikan-compatible anime API base URL (defaults
    to Tenrai, api.tenrai.org/v1). Pass "" for tmdb_api_key to clear it; pass "" for
    language or anime_api_url to reset them to their defaults."""
    def _m(c):
        if provider is not None:
            c.metadata.provider = provider
        _set_opt(c.metadata, "tmdb_api_key", tmdb_api_key)
        _set_or_default(c.metadata, "language", language)
        _set_or_default(c.metadata, "anime_api_url", anime_api_url)
    app().store.mutate(_m)
    return app().store.redacted()["metadata"]


@tool
def configure_prowlarr(
    url: Optional[str] = None,
    api_key: Optional[str] = None,
    indexer_ids: Optional[list[int]] = None,
) -> dict:
    """Configure the Prowlarr connection. ``indexer_ids`` optionally restricts
    searches to specific indexers (empty list = all)."""
    def _m(c):
        _set_opt(c.prowlarr, "url", url)
        _set_opt(c.prowlarr, "api_key", api_key)
        if indexer_ids is not None:
            c.prowlarr.indexer_ids = indexer_ids
    app().store.mutate(_m)
    return app().store.redacted()["prowlarr"]


@tool
def configure_download_client(
    name: str,
    url: Optional[str] = None,
    type: Literal["qbittorrent"] = "qbittorrent",
    username: Optional[str] = None,
    password: Optional[str] = None,
    category: Optional[str] = None,
    save_path: Optional[str] = None,
    make_default: bool = False,
) -> dict:
    """Add or update a download client. ``save_path`` is the download directory
    as the *client* sees it; use path mappings to translate for Plex. The first
    client added becomes the default automatically; pass ``make_default=true`` to
    force an existing setup to switch. Pass "" for category to reset it to the
    default; "" for url/username/password/save_path to clear them."""
    def _m(c):
        existing = c.download_clients.get(name)
        cfg = existing or DownloadClientConfig(type=type)
        cfg.type = type
        _set_opt(cfg, "url", url)
        _set_opt(cfg, "username", username)
        _set_opt(cfg, "password", password)
        _set_or_default(cfg, "category", category)
        _set_opt(cfg, "save_path", save_path)
        c.download_clients[name] = cfg
        if make_default or c.default_download_client is None:
            c.default_download_client = name
    app().store.mutate(_m)
    return app().store.redacted()["download_clients"][name]


@tool
def remove_download_client(name: str) -> dict:
    """Remove a configured download client by name. If it was the default, the
    default is cleared (or reassigned to the sole remaining client). Returns the
    remaining clients and the current default."""
    if name not in app().config.download_clients:
        return {"error": f"No download client named {name}"}

    def _m(c):
        c.download_clients.pop(name, None)
        if c.default_download_client == name:
            # Reassign to the only remaining client, else clear.
            c.default_download_client = (
                next(iter(c.download_clients)) if len(c.download_clients) == 1 else None
            )
    app().store.mutate(_m)
    return {
        "removed": name,
        "download_clients": list(app().config.download_clients),
        "default_download_client": app().config.default_download_client,
    }


@tool
def configure_plex(
    url: Optional[str] = None,
    token: Optional[str] = None,
    tv_section: Optional[str] = None,
    movie_section: Optional[str] = None,
    anime_section: Optional[str] = None,
) -> dict:
    """Configure the Plex connection and library section names. ``anime_section``
    (optional) marks a section whose imported shows use absolute (anime)
    numbering; pass "" to clear it."""
    def _m(c):
        _set_opt(c.plex, "url", url)
        _set_opt(c.plex, "token", token)
        if tv_section:
            c.plex.tv_section = tv_section
        if movie_section:
            c.plex.movie_section = movie_section
        _set_opt(c.plex, "anime_section", anime_section)
    app().store.mutate(_m)
    return app().store.redacted()["plex"]


@tool
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


@tool
def list_path_mappings() -> list[dict]:
    """List all configured path mappings."""
    return [m.model_dump() for m in app().config.path_mappings]


@tool
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


@tool
def translate_path(path: str, from_context: str, to_context: str) -> dict:
    """Translate a path between two contexts using the configured mappings."""
    from . import pathmap

    result = pathmap.translate(app().config, path, from_context, to_context)
    return {"input": path, "from": from_context, "to": to_context, "result": result}


@tool
def configure_root_folder(
    name: str, path: str, media_type: Literal["tv", "movie"] = "tv", context: str = "local"
) -> list[dict]:
    """Add or replace a library root folder (where a media type lives, per
    context). Returns all root folders."""
    def _m(c):
        c.root_folders = [r for r in c.root_folders if r.name != name]
        c.root_folders.append(
            RootFolder(name=name, path=path, media_type=media_type, context=context)
        )
    app().store.mutate(_m)
    return [r.model_dump() for r in app().config.root_folders]


@tool
def list_root_folders() -> list[dict]:
    """List configured library root folders."""
    return [r.model_dump() for r in app().config.root_folders]


@tool
def remove_root_folder(name: str) -> list[dict]:
    """Remove a library root folder by name. Returns the remaining folders."""
    def _m(c):
        c.root_folders = [r for r in c.root_folders if r.name != name]
    app().store.mutate(_m)
    return [r.model_dump() for r in app().config.root_folders]


@tool
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


@tool
def configure_import(
    enabled: Optional[bool] = None,
    mode: Optional[Literal["hardlink", "copy", "move"]] = None,
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


@tool
def configure_rss(
    enabled: Optional[bool] = None,
    interval_minutes: Optional[int] = None,
    auto_grab: Optional[bool] = None,
    refresh_interval_hours: Optional[int] = None,
) -> dict:
    """Configure the background RSS/auto-grab loop and (re)start or stop it.
    ``refresh_interval_hours`` controls how often the poller re-fetches metadata
    for still-airing monitored series to pick up new episodes (0 disables it)."""
    def _m(c):
        if enabled is not None:
            c.rss.enabled = enabled
        if interval_minutes is not None:
            c.rss.interval_minutes = interval_minutes
        if auto_grab is not None:
            c.rss.auto_grab = auto_grab
        if refresh_interval_hours is not None:
            c.rss.refresh_interval_hours = refresh_interval_hours
    app().store.mutate(_m)
    if app().config.rss.enabled:
        state.poller.start()
    return state.poller.status()


# --------------------------------------------------------------------------- #
# Server / auth / deployment mode
# --------------------------------------------------------------------------- #
@tool
def configure_server(
    single_host: Optional[bool] = None,
    require_auth: Optional[bool] = None,
    auth_mode: Optional[Literal["token", "oauth", "none"]] = None,
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
    def _m(c):
        if single_host is not None:
            c.single_host = single_host
        # auth_mode implies require_auth; apply it FIRST so an explicit
        # require_auth in the same call wins.
        if auth_mode is not None:
            c.server.auth_mode = auth_mode
            c.server.require_auth = auth_mode != "none"
        if require_auth is not None:
            c.server.require_auth = require_auth
        _set_opt(c.server, "public_url", public_url)
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


@tool
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


@tool
def auth_token(
    action: Literal["get", "set", "rotate"] = "get", token: Optional[str] = None
) -> dict:
    """Manage the HTTP bearer token (for configuring your MCP client; stdio needs
    none). ``get`` reveals the current token; ``set`` stores ``token`` (or
    generates one if omitted); ``rotate`` generates a fresh random token.
    WARNING: ``set``/``rotate`` take effect on the next HTTP server restart and
    lock out any client still using the old token."""
    server = app().config.server
    if action == "get":
        return {"auth_token": server.auth_token, "require_auth": server.require_auth,
                "configured": bool(server.auth_token)}
    new = (token if action == "set" and token else generate_token())
    app().store.mutate(lambda c: setattr(c.server, "auth_token", new))
    return {"auth_token": new, "action": action}


@tool
def rotate_oauth_keys(clear_clients: bool = True) -> dict:
    """Rotate the OAuth signing key, invalidating EVERY issued OAuth access and
    refresh token (all claude.ai / mobile connector sessions must re-authorize).
    This is the OAuth counterpart to auth_token('rotate') — the static bearer
    token is unaffected. ``clear_clients=true`` (default) also forgets every
    dynamically-registered OAuth client, so they must re-register (DCR) to
    reconnect. A deliberate lockout action; takes effect immediately for token
    verification and on the next HTTP server restart for the persisted key."""
    import secrets

    new_key = secrets.token_urlsafe(48)
    app().store.mutate(lambda c: setattr(c.server, "oauth_signing_key", new_key))
    cleared = app().db.clear_oauth_clients() if clear_clients else 0
    return {
        "rotated": True,
        "oauth_signing_key": "***set***",
        "clients_cleared": cleared,
        "note": "All OAuth tokens are now invalid; connectors must re-authorize"
        + (" and re-register." if clear_clients else "."),
    }


# --------------------------------------------------------------------------- #
# Connection tests
# --------------------------------------------------------------------------- #
@tool
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
@tool
async def search_series(query: str, provider: Optional[Literal["tmdb", "jikan"]] = None) -> list[dict]:
    """Search for TV series matching ``query``. ``provider`` overrides the default
    metadata source — use ``"jikan"`` for anime (MyAnimeList, no key) or
    ``"tmdb"`` for general TV. Each result carries its own ``provider``/
    ``provider_id`` to pass to add_series."""
    results = await app().provider(provider).search_series(query)
    return [r.model_dump() for r in results]


@tool
async def add_series(
    provider_id: str,
    monitored: bool = True,
    root_folder: Optional[str] = None,
    seasons: Optional[list[int]] = None,
    provider: Optional[Literal["tmdb", "jikan"]] = None,
) -> dict:
    """Add a series to the library by its metadata provider id, fetching its full
    episode list. Pass the same ``provider`` used to find it (e.g. ``"jikan"`` for
    an anime from MyAnimeList). ``seasons`` optionally limits which seasons are
    monitored for auto-grab (default: all). Re-adding an existing series refreshes
    metadata without resetting your monitored choices. Note: anime is modelled as
    season 1 with absolute episode numbers."""
    return await app().add_series(
        provider_id,
        monitored=monitored,
        root_folder=root_folder,
        seasons=seasons,
        provider=provider,
    )


_SERIES_COMPACT = ("id", "title", "year", "monitored", "status", "absolute_numbering", "provider")
_MOVIE_COMPACT = ("id", "title", "year", "monitored", "movie_status", "provider")


def _compact(rows: list[dict], keys, limit: Optional[int]) -> list[dict]:
    out = [{k: r.get(k) for k in keys} for r in rows]
    return out[:limit] if limit else out


@tool
def list_series(limit: Optional[int] = None, full: bool = False) -> list[dict]:
    """List series in the library. Compact rows by default (id/title/year/
    monitored/status); pass ``full=true`` for all fields or ``limit`` to cap.
    Use get_series for one series' episode summary."""
    rows = app().db.list_series()
    return rows[: limit or None] if full else _compact(rows, _SERIES_COMPACT, limit)


@tool
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


@tool
def list_episodes(
    series_id: int,
    status: Optional[Literal["missing", "grabbed", "downloaded"]] = None,
    monitored_only: bool = False,
    limit: Optional[int] = None,
) -> list[dict]:
    """List episodes of a series, optionally filtered by status and monitored
    flag, capped by ``limit``."""
    rows = app().db.list_episodes(
        series_id, status=status, monitored=True if monitored_only else None
    )
    return rows[:limit] if limit else rows


@tool
def set_series_monitored(series_id: int, monitored: bool, season: Optional[int] = None) -> dict:
    """Monitor/unmonitor a whole series or a single season (mirrors
    set_movie_monitored)."""
    app().db.set_monitored(series_id, monitored, season=season)
    return get_series(series_id)


@tool
def set_episode_monitored(episode_id: int, monitored: bool) -> dict:
    """Monitor/unmonitor a single episode for auto-grab (finer-grained than
    set_series_monitored, which does a whole series/season). Only monitored,
    still-missing episodes are picked up by the RSS poller."""
    ep = app().db.get_episode(episode_id)
    if not ep:
        return {"error": f"No episode with id {episode_id}"}
    app().db.set_episode_monitored(episode_id, monitored)
    return app().db.get_episode(episode_id)


@tool
async def activate_series(
    series_id: int,
    provider: Optional[Literal["tmdb", "jikan"]] = None,
    provider_id: Optional[str] = None,
    mark_downloaded: bool = True,
) -> dict:
    """Turn a catalogued series (e.g. one registered by import_plex_library) into
    a fully monitored one: fetch its episode list from a metadata provider and
    mark the episodes already in Plex as downloaded, so only genuinely-missing
    episodes get auto-grabbed. For anime pass provider='jikan' with the
    MyAnimeList id (a Plex-catalogued show's TMDB id won't match jikan). Returns
    episode / marked-downloaded / still-missing counts."""
    return await app().activate_series(
        series_id, provider=provider, provider_id=provider_id, mark_downloaded=mark_downloaded
    )


@tool
async def refresh_series(series_id: int) -> dict:
    """Re-fetch provider metadata for a library series and add any newly-aired
    episodes (new regular episodes are monitored iff the series is monitored;
    specials stay unmonitored). Existing episodes' status/monitored flags and the
    series' monitored/root-folder choices are never touched. The RSS poller does
    this automatically for still-airing monitored series (see
    rss.refresh_interval_hours); call this to force it now."""
    return await app().refresh_series(series_id)


@tool
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
@tool
async def search_movies(query: str, provider: Optional[Literal["tmdb", "jikan"]] = None) -> list[dict]:
    """Search for movies matching ``query``. ``provider`` overrides the default —
    ``"jikan"`` for anime films (MyAnimeList, no key), ``"tmdb"`` otherwise."""
    results = await app().provider(provider).search_movies(query)
    return [r.model_dump() for r in results]


@tool
async def add_movie(
    provider_id: str,
    monitored: bool = True,
    root_folder: Optional[str] = None,
    provider: Optional[Literal["tmdb", "jikan"]] = None,
) -> dict:
    """Add a movie to the library by its metadata provider id. Pass the same
    ``provider`` used to find it. A monitored movie is auto-grabbed by the RSS
    poller while it is still missing."""
    return await app().add_movie(
        provider_id,
        monitored=monitored,
        root_folder=root_folder,
        provider=provider,
    )


@tool
def list_movies(limit: Optional[int] = None, full: bool = False) -> list[dict]:
    """List movies in the library. Compact rows by default; ``full=true`` for all
    fields, ``limit`` to cap."""
    rows = app().db.list_movies()
    return rows[: limit or None] if full else _compact(rows, _MOVIE_COMPACT, limit)


@tool
def get_movie(movie_id: int) -> dict:
    """Get one movie from the library."""
    movie = app().db.get_movie(movie_id)
    return movie or {"error": f"No movie with id {movie_id}"}


@tool
def set_movie_monitored(movie_id: int, monitored: bool) -> dict:
    """Monitor/unmonitor a movie for auto-grab."""
    if not app().db.get_movie(movie_id):
        return {"error": f"No movie with id {movie_id}"}
    app().db.set_movie_monitored(movie_id, monitored)
    return app().db.get_movie(movie_id)


@tool
def remove_movie(movie_id: int) -> dict:
    """Remove a movie from the library."""
    movie = app().db.get_movie(movie_id)
    if not movie:
        return {"error": f"No movie with id {movie_id}"}
    app().db.delete_movie(movie_id)
    return {"removed": movie_id, "title": movie["title"]}


@tool
async def search_movie_releases(movie_id: int, apply_quality: bool = True) -> dict:
    """Search Prowlarr for releases of a library movie (by title + year). Returns
    an envelope: {query, count, releases}."""
    movie = app().db.get_movie(movie_id)
    if not movie:
        return {"error": f"No movie with id {movie_id}"}
    query = f"{movie['title']} {movie['year']}" if movie["year"] else movie["title"]
    releases = await app().search_releases(
        query, categories=[CAT_MOVIE], apply_quality=apply_quality
    )
    return {"query": query, "count": len(releases), "releases": releases}


@tool
async def grab_movie(movie_id: int, client_name: Optional[str] = None) -> dict:
    """Auto-pick the best available release for a library movie and grab it."""
    movie = app().db.get_movie(movie_id)
    if not movie:
        return {"error": f"No movie with id {movie_id}"}
    result = await search_movie_releases(movie_id)
    if "error" in result:
        return result  # propagate a Prowlarr outage, don't mask it
    releases = result.get("releases", [])
    if not releases:
        return {"error": "No releases found", "movie": movie["title"]}
    best = releases[0]
    grab = await app().grab(
        best["grab_url"],
        title=best["title"],
        movie_id=movie_id,
        indexer=best.get("indexer"),
        guid=best.get("guid"),
        client_name=client_name,
    )
    return {"picked": best, **grab}


# --------------------------------------------------------------------------- #
# Release search & grabbing
# --------------------------------------------------------------------------- #
@tool
async def search_releases(
    query: str,
    media_type: Literal["tv", "movie"] = "tv",
    apply_quality: bool = True,
    indexer_ids: Optional[list[int]] = None,
) -> dict:
    """Search Prowlarr for torrent releases. When ``apply_quality`` is true,
    results are filtered by the configured quality rules and ranked best-first.
    Returns an envelope: {query, count, releases}."""
    cats = [CAT_MOVIE] if media_type == "movie" else [CAT_TV]
    releases = await app().search_releases(
        query, categories=cats, indexer_ids=indexer_ids, apply_quality=apply_quality
    )
    return {"query": query, "count": len(releases), "releases": releases}


@tool
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
    other = [r for r in all_releases if r not in matched]
    return {
        "query": query,
        "matched": matched,
        "other_count": len(other),
        "other_sample": other[:5],  # capped — full list is context waste
    }


@tool
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
    movie so it is imported into Plex on completion. When linked to a series, a
    season/batch release marks every episode it covers as grabbed (so RSS won't
    double-grab), and multi-file packs are split per-episode on import."""
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


@tool
async def grab_season(series_id: int, season: int, client_name: Optional[str] = None) -> dict:
    """Find and grab the best season/batch pack for a series and link it, so every
    episode is split into place on import and marked grabbed. For anime (single
    entry) any season resolves to the whole-series batch."""
    return await app().grab_season(series_id, season, client_name=client_name)


@tool
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
        indexer=best.get("indexer"),
        guid=best.get("guid"),
        client_name=client_name,
    )
    return {"picked": best, **grab}


# --------------------------------------------------------------------------- #
# Download tracking
# --------------------------------------------------------------------------- #
@tool
def list_downloads(
    status: Optional[
        Literal["grabbed", "downloading", "completed", "imported", "failed", "removed"]
    ] = None,
) -> list[dict]:
    """List grabs recorded by LLMarr, optionally filtered by status."""
    return app().db.list_downloads(status=status)


@tool
async def download_queue() -> dict:
    """Live progress for all in-flight grabs — name, %, download speed, ETA and
    seed count for each download still in a client (not yet imported/removed).
    Use get_download for one download, remove_download to cancel one."""
    items = await app().download_queue()
    return {"count": len(items), "downloads": items}


@tool
async def get_download(download_id: int) -> dict:
    """Get one recorded grab plus live status (progress, speed, ETA, seeds) from
    its download client."""
    import asyncio

    d = app().db.get_download(download_id)
    if not d:
        return {"error": f"No download with id {download_id}"}
    if not d["torrent_hash"]:
        return {"download": d, "live": None}
    st = await asyncio.to_thread(app().download_client(d["client"]).status, d["torrent_hash"])
    return {"download": d, "live": st.model_dump() if st else None}


@tool
async def refresh_downloads() -> list[dict]:
    """Poll all active grabs, import completed ones into the library
    (hardlink/copy/move) and trigger a Plex scan. Returns what changed."""
    return await app().refresh_downloads()


@tool
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


@tool
async def remove_download(download_id: int, delete_files: bool = False) -> dict:
    """Cancel/remove a grab: delete the torrent from the download client and mark
    it removed in LLMarr. ``delete_files=true`` also deletes the downloaded data
    (use this to fully cancel an in-progress download); false keeps any files."""
    import asyncio

    d = app().db.get_download(download_id)
    if not d:
        return {"error": f"No download with id {download_id}"}
    if d["torrent_hash"]:
        await asyncio.to_thread(
            app().download_client(d["client"]).remove, d["torrent_hash"], delete_files
        )
    app().db.set_download_status(download_id, "removed")
    # Put its episodes/movie back to 'missing' so RSS can re-grab.
    reset = app().reset_grab_to_missing(d)
    return {"removed": download_id, "reset_to_missing": len(reset)}


# --------------------------------------------------------------------------- #
# Recovery — unstick wedged state
# --------------------------------------------------------------------------- #
@tool
def reset_episode(episode_id: int) -> dict:
    """Force an episode back to 'missing' so the RSS poller re-grabs it. Use this
    to unstick an episode wedged in 'grabbed' (e.g. after a download you gave up
    on) or to re-download a 'downloaded' one."""
    return app().reset_episode(episode_id)


@tool
def reset_movie(movie_id: int) -> dict:
    """Force a movie back to 'missing' so the RSS poller re-grabs it."""
    return app().reset_movie(movie_id)


@tool
def mark_download_failed(download_id: int) -> dict:
    """Mark a download failed and free the episodes/movie it covered (only those
    still 'grabbed') back to 'missing', so RSS can try a different release."""
    return app().mark_download_failed(download_id)


@tool
def retry_download(download_id: int) -> dict:
    """Retry a stuck/failed grab: mark it failed and force every linked episode/
    movie back to 'missing' (regardless of current status) so the RSS poller
    grabs a fresh release next tick. To also re-allow the same release, pair with
    forget_release(guid)."""
    return app().retry_download(download_id)


@tool
def forget_release(guid: str) -> dict:
    """Remove one release guid from grab history so it can be grabbed again (a bad
    release is otherwise never re-tried). Find the guid via search results."""
    removed = app().db.forget_guid(guid)
    return {"guid": guid, "forgotten": removed}


@tool
def clear_grab_history() -> dict:
    """Wipe the entire grab history (every remembered release guid). After this,
    previously-grabbed releases become eligible again — use sparingly."""
    return {"cleared": app().db.clear_grab_history()}


# --------------------------------------------------------------------------- #
# Plex
# --------------------------------------------------------------------------- #
@tool
async def plex_login_start(product: str = "LLMarr") -> dict:
    """Begin browser-based Plex sign-in (no manual token needed). Returns a short
    code to enter at https://plex.tv/link; then call ``plex_login_poll`` to finish.
    Reuses a persistent device identity across logins."""
    import uuid

    from . import plexauth

    cid = app().config.plex.client_id
    if not cid:
        cid = str(uuid.uuid4())
        app().store.mutate(lambda c: setattr(c.plex, "client_id", cid))
    pin = await plexauth.request_pin(cid, product)
    app().db.set_kv("plex_pin_id", str(pin["id"]))
    return {
        "link_url": plexauth.LINK_URL,
        "code": pin["code"],
        "instructions": f"Open {plexauth.LINK_URL}, sign in if asked, and enter code "
        f"{pin['code']}. Then call plex_login_poll.",
    }


@tool
async def plex_login_poll(
    url: Optional[str] = None, max_wait_seconds: int = 30
) -> dict:
    """Wait for the pending Plex login to be approved, then store the token.
    Blocks up to ``max_wait_seconds`` (call again if not yet approved). ``url`` is
    only used to set the Plex URL when none is configured yet (default
    http://localhost:32400); it never overwrites an existing one."""
    import asyncio

    from . import plexauth

    pin_id = app().db.get_kv("plex_pin_id")
    cid = app().config.plex.client_id
    if not pin_id or not cid:
        return {"error": "No pending login — call plex_login_start first."}

    waited = 0
    while waited <= max_wait_seconds:
        token = await plexauth.poll_token(pin_id, cid)
        if token:
            def _m(c):
                c.plex.token = token
                if not c.plex.url:  # don't clobber an already-configured URL
                    c.plex.url = url or "http://localhost:32400"
            app().store.mutate(_m)
            app().db.set_kv("plex_pin_id", "")
            return {"authorized": True, "url": app().config.plex.url,
                    "message": "Plex linked. Use plex_discover_libraries to set root folders."}
        await asyncio.sleep(3)
        waited += 3
    return {"authorized": False, "message": "Not approved yet — enter the code, then poll again."}


@tool
async def import_plex_library(
    dry_run: bool = True,
    monitored: bool = False,
    media_type: Literal["all", "tv", "movie"] = "all",
    sections: Optional[list[str]] = None,
) -> dict:
    """Autodetect existing shows & movies in your Plex libraries and register them
    in LLMarr as owned catalog entries (so they aren't re-downloaded), using Plex's
    external ids (TMDB when available, else a Plex key). ``dry_run=true`` (default)
    previews without writing and lists ``sections_available`` — review it, then
    re-run with ``sections=["Anime","Movies"]`` to include only the libraries you
    want and ``dry_run=false``. Series are catalogued WITHOUT episodes; to enable
    episode monitoring/auto-grab for a show, add_series it afterwards with its
    provider id."""
    return await app().import_from_plex(
        dry_run=dry_run, monitored=monitored, media_type=media_type, sections=sections
    )


@tool
async def bulk_activate_series(mark_downloaded: bool = True, limit: Optional[int] = None) -> dict:
    """Activate ALL catalogued (episode-less) series that can be activated safely
    in one call — non-anime, tmdb-keyed shows imported from Plex — fetching their
    episodes and marking the ones Plex already has as downloaded. Anime and
    plex-only entries are skipped and reported (a Plex tmdb id is not a MAL id, so
    activate anime individually with activate_series + the jikan id). Runs
    sequentially to respect provider rate limits; ``limit`` caps how many are
    activated this call."""
    return await app().bulk_activate_series(mark_downloaded=mark_downloaded, limit=limit)


@tool
async def plex_discover_libraries() -> list[dict]:
    """List Plex library sections with their on-disk paths — use this to pick the
    section names for configure_plex and the paths for configure_root_folder."""
    import asyncio

    return await asyncio.to_thread(app().plex().libraries)


@tool
async def plex_scan(section: Optional[str] = None, path: Optional[str] = None) -> dict:
    """Trigger a Plex library scan. ``path`` (in Plex's own namespace) narrows
    the scan to one directory."""
    import asyncio

    return await asyncio.to_thread(app().plex().scan, section, path)


# --------------------------------------------------------------------------- #
# RSS / auto-grab
# --------------------------------------------------------------------------- #
@tool
def rss_status() -> dict:
    """Show the background auto-grab poller's status and last run result."""
    return state.poller.status()


@tool
async def rss_poll_now() -> dict:
    """Run the RSS/auto-grab poll immediately (search monitored series for
    missing episodes and grab/collect candidates), plus refresh downloads."""
    return await state.poller.poll_once()


def run() -> None:
    mcp.run()
