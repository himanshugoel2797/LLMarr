# LLMarr

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

An MCP server that replicates the core of Sonarr/Radarr/Lidarr — but driven
entirely by an LLM. It pulls metadata from a configurable provider (TMDB by
default), finds torrents through **Prowlarr**, grabs them with a configured
torrent client (**qBittorrent** first), notifies **Plex** when a download lands,
and runs a background loop that auto-grabs new episodes of monitored series.

The whole point is flexibility: everything — providers, credentials, quality
rules, path mappings, monitoring — is configurable through MCP tools, and a user
can always hand it a raw magnet/torrent URL to grab manually.

## Why an MCP server

Sonarr/Radarr are great but rigid. Exposing these primitives as MCP tools lets an
LLM do the fuzzy parts (disambiguating a show, deciding which release looks
right, reacting to "just grab this link") while LLMarr does the mechanical parts
(search, grab, track, import).

## Single-host by default, container-aware when you need it

Most people (including the author) run LLMarr, qBittorrent, and Plex on one host
where they all see the same paths. That's the default — **`single_host: true`** —
and it needs **no path mappings**: paths pass through untranslated.

For a split-container deployment, set `single_host: false` (via
`configure_server(single_host=false)`) and describe how each container sees the
same volume with **path mappings** — entries sharing a `group` are the same
physical directory. When a download completes, the qBittorrent save path is
translated into Plex's namespace before the targeted scan. In this mode an
unmapped path raises instead of silently passing through.

```
add_path_mapping("dl", "qbittorrent", "/downloads")
add_path_mapping("dl", "plex",        "/data/torrents")
add_path_mapping("dl", "local",       "/mnt/media/dl")
```

## Install

```bash
pip install -e .
```

## Run

Stdio (what most MCP clients expect):

```bash
llmarr
```

HTTP (run it once as a persistent service a client logs into):

```bash
LLMARR_TRANSPORT=streamable-http LLMARR_HOST=0.0.0.0 LLMARR_PORT=8000 llmarr
```

Config is stored at `$LLMARR_CONFIG` (default `~/.config/llmarr/config.yaml`) and
the library/history/RSS state at `$LLMARR_DB`
(default `~/.local/share/llmarr/llmarr.db`).

### Register with an MCP client

Stdio — the client spawns the process (no auth needed, it's local):

```json
{
  "mcpServers": {
    "llmarr": { "command": "llmarr" }
  }
}
```

HTTP — point the client at the URL with the bearer token (see below):

```json
{
  "mcpServers": {
    "llmarr": {
      "url": "http://your-host:8000/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

## Authentication (single persistent login)

stdio needs no auth — the MCP client launches LLMarr directly. The **HTTP
transport** is protected by one **static bearer token**, deliberately simple for
a single-user homelab service: a persistent login rather than a per-session OAuth
dance.

- On first HTTP start, if no token is set, LLMarr **generates one, saves it to
  config, and prints it** (with the URL) to stderr. The same token is reused on
  every restart.
- Clients send `Authorization: Bearer <token>` on every request; anything else
  gets `401`.
- Manage it with the `get_auth_token`, `set_auth_token`, and `rotate_auth_token`
  tools, or set your own value in `config.yaml`. Disable auth entirely (e.g.
  behind your own reverse proxy) with `configure_server(require_auth=false)`.

## First-run setup (all via tools)

1. `configure_metadata(tmdb_api_key="…")`
2. `configure_prowlarr(url="http://localhost:9696", api_key="…")`
3. `configure_download_client("qbit", url="http://localhost:8080", username="…", password="…", save_path="/data/downloads")`
4. `configure_plex(url="http://localhost:32400", token="…", tv_section="TV Shows")`
5. `configure_root_folder("tv-main", "/data/media/tv")` (and a `movie` one)
6. Single host? You're done — skip path mappings. Split containers?
   `configure_server(single_host=false)` then `add_path_mapping(...)` per namespace.
7. `test_connections()` to confirm everything is reachable

## Typical flow

```
search_series("severance")            -> pick a TMDB id
add_series("95396", seasons=[1,2])    -> library + episode list, monitored
search_releases("severance S02E01")   -> ranked torrents (quality rules applied)
grab_release(grab_url=…, series_id=1, episode_id=42)
refresh_downloads()                   -> marks completed, scans Plex
```

The background poller (`configure_rss`) does the `search → grab → import` loop
automatically for monitored, still-missing episodes. Trigger it on demand with
`rss_poll_now()` and inspect it with `rss_status()`.

## Import / hardlink

When a download completes, LLMarr organises it into a Sonarr/Radarr-style
library instead of just scanning the download folder:

```
<root>/Series Title (Year)/Season 01/Series Title - S01E01 - Ep Title.mkv
<root>/Movie Title (Year)/Movie Title (Year).mkv
```

Files are **hardlinked** by default (falling back to a copy across filesystems),
so the torrent keeps seeding while Plex sees a clean library. Configure with
`configure_import(mode="hardlink|copy|move", work_context="local", …)`.

Importing runs in `work_context` — the namespace **LLMarr itself** can read and
write. The download's path and the destination root are both translated into
that context first, so hardlinks require the download dir and the library root to
be on the same filesystem there. After linking, the resulting library folder is
translated into Plex's namespace and a targeted scan is fired. `import_download`
re-runs the import for a completed grab (handy after fixing a mapping).

## Movies

Movies work like series: `search_movies` → `add_movie` (monitored) → the RSS
poller auto-grabs by title+year while the movie is missing, or grab on demand
with `grab_movie` / `search_movie_releases`. Completed movie downloads import and
scan the Plex movie section.

## Tool surface

| Area | Tools |
| --- | --- |
| Config | `get_config`, `configure_metadata`, `configure_prowlarr`, `configure_download_client`, `configure_plex`, `configure_root_folder`, `configure_quality`, `configure_rss`, `configure_import` |
| Server / auth | `configure_server`, `get_auth_token`, `set_auth_token`, `rotate_auth_token` |
| Path maps | `add_path_mapping`, `list_path_mappings`, `remove_path_mapping`, `translate_path` |
| Diagnostics | `test_connections` |
| Series | `search_series`, `add_series`, `list_series`, `get_series`, `list_episodes`, `set_monitored`, `remove_series` |
| Movies | `search_movies`, `add_movie`, `list_movies`, `get_movie`, `set_movie_monitored`, `remove_movie`, `search_movie_releases`, `grab_movie` |
| Releases | `search_releases`, `search_episode_releases`, `grab_release`, `grab_episode` |
| Downloads | `list_downloads`, `download_status`, `refresh_downloads`, `import_download`, `remove_download` |
| Plex | `scan_plex` |
| RSS | `rss_status`, `rss_poll_now` |

## Quality selection

Not full Sonarr custom formats — a lightweight, predictable heuristic
(`configure_quality`): hard filters (ignored/required terms, min seeders, size
bounds) then ranking by resolution preference, preferred terms, and seeders.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The suite is fully offline: metadata/Prowlarr HTTP is driven by
`httpx.MockTransport`, qBittorrent/Plex are faked, and the DB, config, path
mapping and importer (real hardlinks in a temp dir) are exercised directly. No
credentials or running services are required.

## Scope / status

Supports **TV and movies** via qBittorrent + Prowlarr + Plex + TMDB, with
hardlink/copy/move import into an organised library. The
provider/indexer/download-client/notifier interfaces are abstract so other
clients (Transmission, Deluge) and metadata sources can be added without touching
the core. Not yet implemented: full Sonarr custom-format quality profiles,
multi-episode-file (double-episode) parsing, and clients other than qBittorrent.
