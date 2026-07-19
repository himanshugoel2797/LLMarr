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
- Manage it with the `auth_token` tool (`auth_token("get"|"set"|"rotate")`), or
  set your own value in `config.yaml`. Disable auth entirely (e.g. behind your own
  reverse proxy) with `configure_server(require_auth=false)`.

### OAuth mode (for claude.ai custom connectors / mobile apps)

claude.ai custom connectors — and therefore the Claude iOS/Android apps —
authenticate with **OAuth 2.1**, not a static header. LLMarr can act as its own
OAuth authorization server for this:

```
configure_server(auth_mode="oauth", public_url="https://arr.example.com")
# then restart the HTTP server
```

The flow keeps the single-login idea: when Claude sends you to the authorize
page, you **enter the same LLMarr token** to approve. Under the hood LLMarr
implements dynamic client registration (RFC 7591), discovery metadata (RFC
8414 + 9728), authorization-code + PKCE (S256), and refresh tokens — all signed
JWTs, no external identity provider. The static token still works as a direct
bearer header too, so Claude Code keeps connecting unchanged.

To add it on mobile: claude.ai → **Settings → Connectors → Add custom
connector**, URL `https://arr.example.com/mcp`. Claude discovers the OAuth
endpoints automatically; approve with your token. `oauth_info` prints the exact
URLs. `public_url` must be set (or derivable from the request) so the issued
endpoint URLs are correct.

## Remote access via Cloudflare Tunnel

The HTTP server's MCP endpoint is at **`/mcp`**. Cloudflare terminates TLS and
the bearer token authenticates the client, so no inbound ports are opened to the
internet. What `cloudflared`'s ingress points at depends on where it runs.

**Bind address matters.** The default `LLMARR_HOST=127.0.0.1` only accepts
connections from the *same* machine. Set the host to something the cloudflared
process can reach.

### cloudflared on the same machine

```bash
LLMARR_TRANSPORT=streamable-http llmarr            # binds 127.0.0.1:8000
cloudflared tunnel --url http://localhost:8000     # quick tunnel, or a named tunnel
```

### cloudflared on a different host / VM (common)

Bind an interface the other host can reach, and point ingress at *this* host's IP
— `localhost` in the tunnel config would resolve to the cloudflared box, not
LLMarr:

```bash
LLMARR_TRANSPORT=streamable-http LLMARR_HOST=0.0.0.0 LLMARR_PORT=8000 llmarr
```

```yaml
# cloudflared ingress (on the other VM). Replace with this host's LAN IP.
ingress:
  - hostname: llmarr.example.com
    service: http://10.0.0.10:8000
  - service: http_status:404
```

`0.0.0.0` exposes port 8000 to the LAN — the bearer token is the protection.
Tighten it by binding the specific NIC (`LLMARR_HOST=10.0.0.10`) and/or a
firewall rule allowing only the cloudflared host, e.g.
`ufw allow from <cloudflared-ip> to any port 8000`.

### Client config

Whatever the topology, the URL is the tunnel hostname **+ `/mcp`**, with the
token from the server's startup banner:

```json
{
  "mcpServers": {
    "llmarr": {
      "url": "https://llmarr.example.com/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

## First-run setup (all via tools)

The fastest path is to let the LLM drive it: **`setup_status`** returns an ordered
checklist (each step done/pending with the exact next tool to call), enumerates the
available metadata providers / download-client types / auth & import modes, and —
once Plex is linked — lists your detected libraries with ready-to-run
`configure_root_folder` commands. The server also ships these instructions to the
client on connect, so a capable LLM will start there on its own.

1. `configure_metadata(tmdb_api_key="…")`
2. `configure_prowlarr(url="http://localhost:9696", api_key="…")`
3. `configure_download_client("qbit", url="http://localhost:8080", username="…", password="…", save_path="/data/downloads")`
4. **Plex** — either paste a token with `configure_plex(url=…, token=…)`, or sign
   in via the browser: `plex_login_start` → open https://plex.tv/link, enter the
   code → `plex_login_poll`. Then `plex_discover_libraries` shows your sections +
   paths so you can set the right section names and root folders.
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

## Metadata providers (incl. anime)

The metadata source is pluggable per lookup:

- **`tmdb`** (default) — TV + movies. Needs a free API key.
- **`jikan`** — anime via [MyAnimeList](https://myanimelist.net) through a
  Jikan-compatible API. **No API key.** Per-episode titles included. Defaults to
  [Tenrai](https://tenrai.org) (`api.tenrai.org/v1`), a 1:1 Jikan v4 mirror, as
  the original Jikan is being discontinued; override with
  `configure_metadata(anime_api_url=…)`.

Pass `provider="jikan"` to `search_series`/`add_series` (or the movie variants)
for a single anime lookup, or make it the default with
`configure_metadata(provider="jikan")`. Each search result carries its own
`provider`/`provider_id`, so mix sources freely in one library.

> **Anime episode numbering:** MyAnimeList models each cour/season as one entry
> with absolute episode numbers, so LLMarr stores anime as **season 1, episodes
> 1..N** and flags the series as absolute-numbered. Release matching and import
> then understand absolute-numbered names (`[Group] Show - 12 [1080p]`), the
> `Episode 12` / `E12` forms, and batches/ranges (`(01-28)`, `[Batch]`), so
> `grab_episode`, RSS auto-grab and hardlink import all work for anime. Absolute
> matching is applied **only** to anime series, so it can't cause false matches
> on ordinary TV.

## Tool surface

| Area | Tools |
| --- | --- |
| Setup | `setup_status` (guided checklist + enumerations — call first) |
| Config | `get_config`, `configure_metadata`, `configure_prowlarr`, `configure_download_client`, `configure_plex`, `configure_root_folder`, `configure_quality`, `configure_rss`, `configure_import` |
| Server / auth | `configure_server`, `auth_token`, `oauth_info` |
| Path maps | `add_path_mapping`, `list_path_mappings`, `remove_path_mapping`, `translate_path` |
| Diagnostics | `test_connections` |
| Series | `search_series`, `add_series`, `activate_series`, `list_series`, `get_series`, `list_episodes`, `set_series_monitored`, `remove_series` |
| Movies | `search_movies`, `add_movie`, `list_movies`, `get_movie`, `set_movie_monitored`, `remove_movie`, `search_movie_releases`, `grab_movie` |
| Releases | `search_releases`, `search_episode_releases`, `grab_release`, `grab_episode`, `grab_season` |
| Downloads | `list_downloads`, `download_queue`, `get_download`, `refresh_downloads`, `import_download`, `remove_download` (cancel) |
| Plex | `plex_login_start`, `plex_login_poll`, `plex_discover_libraries`, `import_plex_library`, `plex_scan` |
| Root folders | `configure_root_folder`, `list_root_folders`, `remove_root_folder` |
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
