"""Configuration model + on-disk persistence.

Config is stored as a YAML file so it is human-readable, but every field is also
mutatable through MCP tools so the whole system can be configured by the LLM.
Library/history/RSS state lives in SQLite (see ``db.py``) — this file only holds
connection settings, path maps and quality defaults.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Sub-models
# --------------------------------------------------------------------------- #
class MetadataConfig(BaseModel):
    # tmdb (TV + movies, needs a free key) | jikan (MyAnimeList/anime, no key).
    provider: Literal["tmdb", "jikan"] = "tmdb"
    tmdb_api_key: Optional[str] = None
    language: str = "en-US"


class ProwlarrConfig(BaseModel):
    url: Optional[str] = None  # e.g. http://prowlarr:9696
    api_key: Optional[str] = None
    # Restrict searches to these indexer ids (empty = all configured indexers).
    indexer_ids: list[int] = Field(default_factory=list)


class DownloadClientConfig(BaseModel):
    type: Literal["qbittorrent"] = "qbittorrent"
    url: Optional[str] = None  # e.g. http://qbittorrent:8080
    username: Optional[str] = None
    password: Optional[str] = None
    # Category applied to grabs so they are easy to identify in the client and
    # so qBittorrent can route them to a category-specific save path.
    category: str = "llmarr"
    # Save path *as the download client sees it*. Path maps translate this to
    # the contexts Plex / this server see (see PathMapping).
    save_path: Optional[str] = None


class PlexConfig(BaseModel):
    url: Optional[str] = None  # e.g. http://plex:32400
    token: Optional[str] = None
    # Library section name to scan after a TV grab lands.
    tv_section: str = "TV Shows"
    movie_section: str = "Movies"


class PathMapping(BaseModel):
    """A single equivalence between how two containers see the same directory.

    Example: qBittorrent writes to ``/downloads`` but Plex mounts the same
    volume at ``/data/torrents`` and this server sees it at ``/mnt/media/dl``::

        - context: qbittorrent
          path: /downloads
        - context: plex
          path: /data/torrents
        - context: local
          path: /mnt/media/dl

    Mappings are grouped by ``group`` — every entry sharing a group id refers to
    the same physical directory in a different container's namespace.
    """

    group: str
    context: str  # "qbittorrent" | "plex" | "local" | any custom label
    path: str


class RootFolder(BaseModel):
    """Where a media type's library lives, per context.

    ``context`` maps onto the same labels used by PathMapping so that a grab's
    final destination can be resolved for the download client, and the scan
    target resolved for Plex.
    """

    name: str  # friendly id, e.g. "tv-main"
    media_type: Literal["tv", "movie"] = "tv"
    context: str = "local"
    path: str


class QualityConfig(BaseModel):
    """Lightweight release-selection preferences (not full custom formats)."""

    preferred_resolutions: list[str] = Field(
        default_factory=lambda: ["1080p", "720p"]
    )
    required_terms: list[str] = Field(default_factory=list)
    ignored_terms: list[str] = Field(
        default_factory=lambda: ["cam", "ts", "telesync"]
    )
    min_seeders: int = 1
    min_size_mb: int = 0
    max_size_mb: int = 0  # 0 = unlimited
    prefer_terms: list[str] = Field(
        default_factory=lambda: ["web-dl", "webrip", "bluray", "x265", "hevc"]
    )


class RssConfig(BaseModel):
    enabled: bool = True
    interval_minutes: int = 30
    # Grab automatically when a monitored+missing episode is matched, otherwise
    # only record candidates for the LLM to review.
    auto_grab: bool = True


class ImportConfig(BaseModel):
    """How completed downloads are moved into the organised Plex library.

    Import runs in ``work_context`` — the namespace LLMarr itself sees. Both the
    download's files and the destination root folder are translated into that
    context first, so ``hardlink`` requires the download dir and the library root
    to sit on the same filesystem there (the usual Sonarr/Radarr caveat).
    """

    enabled: bool = True
    mode: Literal["hardlink", "copy", "move"] = "hardlink"
    rename: bool = True
    work_context: str = "local"
    min_video_mb: int = 50  # skip samples / stray small files
    video_extensions: list[str] = Field(
        default_factory=lambda: [
            ".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov", ".wmv", ".mpg", ".mpeg", ".flv",
        ]
    )


class ServerConfig(BaseModel):
    """MCP server transport + authentication.

    stdio (the default) needs no auth — the MCP client spawns the process and the
    channel is inherently local/trusted. For the HTTP transport, a single static
    bearer token acts as a persistent login: it is generated once on first HTTP
    start, saved here, and reused across restarts. Clients send it as
    ``Authorization: Bearer <token>``.
    """

    auth_token: Optional[str] = None
    require_auth: bool = True
    # "token" = static bearer token (default; used by Claude Code and simple
    # clients). "oauth" = full OAuth 2.1 authorization-code + PKCE flow, required
    # by claude.ai custom connectors (and hence the mobile apps). "none" is also
    # reachable via require_auth=false. In oauth mode the static token still works
    # as a direct bearer *and* as the credential entered on the authorize page.
    auth_mode: Literal["token", "oauth", "none"] = "token"
    # External base URL (e.g. https://arr.example.com) used to build OAuth issuer
    # and endpoint URLs. If unset, it is derived from the request's Host and
    # X-Forwarded-Proto — correct for a standard tunnel/reverse-proxy setup.
    public_url: Optional[str] = None
    # HS256 signing key for OAuth tokens; generated once on first oauth start.
    oauth_signing_key: Optional[str] = None
    # MCP's DNS-rebinding protection only trusts localhost by default, which
    # rejects access through a tunnel/reverse proxy with "Invalid Host header".
    # List the external hostnames to trust (e.g. "arr.example.com"). Left empty,
    # the protection is disabled — fine for a headless service whose real
    # boundary is the bearer token behind Cloudflare, not the Host header.
    allowed_hosts: list[str] = Field(default_factory=list)
    allowed_origins: list[str] = Field(default_factory=list)


class Config(BaseModel):
    # When true (default, the non-containerised case) LLMarr, qBittorrent and
    # Plex all see the same filesystem paths, so path translation is a no-op and
    # no path mappings are needed. Set false for a split-container deployment and
    # define path_mappings; unmapped paths then raise instead of passing through.
    single_host: bool = True
    server: ServerConfig = Field(default_factory=ServerConfig)
    metadata: MetadataConfig = Field(default_factory=MetadataConfig)
    prowlarr: ProwlarrConfig = Field(default_factory=ProwlarrConfig)
    download_clients: dict[str, DownloadClientConfig] = Field(default_factory=dict)
    default_download_client: Optional[str] = None
    plex: PlexConfig = Field(default_factory=PlexConfig)
    path_mappings: list[PathMapping] = Field(default_factory=list)
    root_folders: list[RootFolder] = Field(default_factory=list)
    quality: QualityConfig = Field(default_factory=QualityConfig)
    rss: RssConfig = Field(default_factory=RssConfig)
    importer: ImportConfig = Field(default_factory=ImportConfig)


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
_SECRET_KEYS = {
    "api_key", "tmdb_api_key", "password", "token", "auth_token", "oauth_signing_key",
}


def default_config_path() -> Path:
    env = os.environ.get("LLMARR_CONFIG")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(base) / "llmarr" / "config.yaml"


class ConfigStore:
    """Thread-safe load/save wrapper around the YAML config file."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else default_config_path()
        self._lock = threading.RLock()
        self._config = self._load()

    def _load(self) -> Config:
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            return Config.model_validate(data)
        return Config()

    @property
    def config(self) -> Config:
        return self._config

    def reload(self) -> Config:
        with self._lock:
            self._config = self._load()
            return self._config

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = self._config.model_dump(mode="json")
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                fh.write("# LLMarr configuration — managed by the MCP server.\n")
                yaml.safe_dump(data, fh, sort_keys=False, default_flow_style=False)
            tmp.replace(self.path)

    def mutate(self, fn) -> Config:
        """Apply ``fn(config)`` under lock and persist."""
        with self._lock:
            fn(self._config)
            self.save()
            return self._config

    def redacted(self) -> dict:
        """Config dump with secrets masked, safe to show a user/LLM."""
        data = self._config.model_dump(mode="json")
        return _redact(data)


def _redact(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _SECRET_KEYS and v:
                out[k] = "***set***"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj
