# AGENTS.md — guidance for coding agents working on LLMarr

LLMarr is an MCP server that replicates Sonarr/Radarr-style media automation but
is driven entirely by an LLM: TMDB metadata → Prowlarr torrent search →
qBittorrent grab → hardlink import into an organised library → Plex scan, plus a
background RSS auto-grab loop. TV and movies are both supported. Prowlarr is
assumed to be available.

## Ground rules

- **Python 3.11+**, stdlib style. Match the surrounding code: type hints,
  `from __future__ import annotations`, module-level docstrings explaining *why*.
- **Everything is configurable through MCP tools.** If you add a setting, add the
  config field *and* a tool to change it, and make sure it round-trips through
  the YAML store.
- **Never break offline testability.** All network I/O goes through a small
  number of seams (see below) that tests fake. Don't call `httpx`/clients from
  places that can't be mocked.
- Run `pytest` before finishing. Keep it green and add tests for new behaviour.

## Layout

```
llmarr/
  config.py      pydantic models + ConfigStore (YAML, thread-safe, secret redaction)
  db.py          SQLite: series, episodes, movies, downloads, grab_history, kv (+ migrations)
  pathmap.py     translate() paths between container namespaces (single-host = passthrough)
  metadata/      MetadataProvider ABC + TMDB (TV+movies) + Jikan/Tenrai (anime)
  indexers/      Prowlarr search client + Release model
  download/      DownloadClient ABC + qBittorrent (resolves .torrent host-side)
  notify/        Plex library scan + catalog + show_episodes
  importer.py    hardlink/copy/move into <root>/Show (Year)/Season NN/… + movie layout
  selector.py    quality filter + ranking (lightweight, NOT Sonarr custom formats)
  parsing.py     SxxExx / season-pack / resolution / anime absolute+range parsing
  core.py        App engine — the glue used by BOTH tools and the RSS poller
  rss/poller.py  asyncio background loop (auto-grab + import), reads config each tick
  auth.py        mode-aware bearer/OAuth middleware for the HTTP transport
  oauth.py       self-contained OAuth 2.1 authorization + resource server
  plexauth.py    plex.tv PIN (browser) login flow
  setup.py       build_status() — powers the setup_status onboarding tool
  server.py      FastMCP instance + all ~54 tools + lifespan (starts poller)
  __main__.py    stdio (default) or streamable-http entrypoint
```

`core.App` is the heart. Tools in `server.py` are thin wrappers over `App`
methods; the poller calls the same `App` methods. Put logic in `App`, not in
tool functions, so it's reachable from both and testable without the MCP layer.

Onboarding: `setup.py` `build_status(app, plex_libraries=, connections=)` powers
the `setup_status` tool — an ordered done/pending checklist + capability
enumerations (providers/client-types/auth+import modes) + detected Plex libraries
with suggested root-folder commands. The FastMCP `instructions` string
(`server.INSTRUCTIONS`, sent on connect) tells LLMs to call it first. Keep both
in sync when adding a config step or provider/client type.

## Key design decisions (don't relitigate without reason)

- **Single-host by default.** `config.single_host=True` → `pathmap.translate`
  passes unmapped paths through unchanged, so a normal one-host install needs
  **zero path mappings**. Split-container installs set it false and define
  mappings; unmapped paths then raise.
- **Path contexts** are arbitrary labels grouped per physical dir. The download
  client's context label is always `"qbittorrent"`; the importer works in
  `importer.work_context` (default `"local"`) — the namespace LLMarr itself can
  read/write. Hardlinks need the download dir and library root on the same
  filesystem *in that context*.
- **Auth modes** (`server.auth_mode`, HTTP transport only; stdio is trusted):
  `token` (static bearer, default), `oauth` (full OAuth 2.1 + PKCE for claude.ai
  connectors / mobile), `none`. The static token is auto-generated + persisted on
  first HTTP start. `auth.py` `AuthMiddleware` is the mode-aware guard; in oauth
  mode it accepts EITHER the static token OR an OAuth JWT, so Claude Code keeps
  working. `oauth.py` is a self-contained authorization server (DCR, discovery
  metadata, authorize page that reuses the static token as the approval
  credential, token endpoint, JWT access/refresh) — no external IdP. Tokens are
  HS256 JWTs signed with `server.oauth_signing_key`; only the single-use auth-code
  jti set is in-memory. `public_url` (or request-derived) builds endpoint URLs.
  `rotate_oauth_keys(clear_clients=True)` (G9) regenerates the signing key —
  invalidating every issued OAuth access/refresh token — and optionally wipes the
  registered `oauth_clients` (DB `clear_oauth_clients`); the static bearer token is
  unaffected (rotate that with `auth_token("rotate")`).
- **Quality selection is a heuristic**, not custom formats: hard filters
  (ignored/required terms, seeders, size) then rank by resolution/preferred
  terms/seeders. Keep it predictable.

## Testing conventions (`tests/`)

- `pip install -e ".[dev]"; pytest`. `asyncio_mode = "auto"` (no `@pytest.mark`).
- Network seams are faked in `conftest.py`: `FakeProvider`, `FakeProwlarr`,
  `FakeDownloadClient`, `FakePlex`. For code that must exercise the real HTTP
  client (TMDB, Prowlarr), use the `mock_httpx` fixture (`httpx.MockTransport`).
- `store`/`db`/`app` fixtures build real instances in `tmp_path`.
- To fake a service on an `App`, monkeypatch the *factory method*
  (`app.provider`, `app.prowlarr`, `app.plex`) — except `grab`/`refresh` which
  construct the download client via `core.get_client`, so patch
  `llmarr.core.get_client` there.
- Tools register via `@tool` (`server.py`, = `@mcp.tool()` + `_guard`), which
  returns a **plain callable**, so server tools are invoked directly in tests
  after `monkeypatch.setattr(server.state, "app", app)`.
- The importer is tested against **real files + real hardlinks** (inode checks).

## Specials & download progress

- Specials = season 0 (OVAs/specials). TMDB `get_series` fetches season 0 (was
  skipped); `add_series` leaves specials UNMONITORED by default — opt in with
  `seasons=[0, …]`. Importer already names them `Season 00/`. Anime OVAs are
  usually separate MAL entries (add them individually via jikan).
- Progress/cancel: `download_queue` (live %, speed, ETA, seeds for in-flight
  grabs), `get_download` (one), `remove_download(delete_files=)` cancels.
  `TorrentStatus` carries dl_speed/eta/num_seeds/size/ratio.
- Recovery (G2): `reset_episode`/`reset_movie` force an item back to 'missing';
  `mark_download_failed` fails a download and conservatively frees its
  still-'grabbed' items; `retry_download` force-resets all linked items
  regardless of status; `forget_release(guid)`/`clear_grab_history` drop grab
  history so a guid can be re-tried. App methods of the same names hold the logic.

## Gotchas

- `db.upsert_episode` / `upsert_movie` deliberately do NOT overwrite
  `status`/`file_path` on metadata refresh — only title/air_date update.
- Secrets (`api_key`, `token`, `password`, `auth_token`) are masked by
  `get_config`; reveal the HTTP bearer token via `auth_token("get")`.
- Download links: `qBittorrentClient.add` resolves non-magnet URLs host-side
  (`_resolve_torrent`) — follows a redirect to a magnet, else fetches the
  `.torrent` bytes and adds them as a FILE. This is because the client is often
  containerised (e.g. behind a VPN) and can't reach the indexer/Prowlarr URL that
  LLMarr can. Keep new download clients doing the same.
- The RSS poller re-reads config each tick, so config changes apply without a
  restart (except the HTTP auth token, which is bound at server start).
- Periodic metadata refresh (G1): `App.refresh_series(series_id)` re-fetches
  provider metadata and upserts newly-aired episodes (new regular eps monitored
  iff the series is monitored, specials left unmonitored; existing episodes' and
  the series' status/monitored/root_folder are never touched). `poll_once` calls
  `App.refresh_stale_series` first, refreshing monitored non-ended series
  (`status` not in `App.ENDED_STATUSES`) not refreshed within
  `rss.refresh_interval_hours` (default 12, 0 disables); `series.last_refresh`
  stamps the time. Tool: `refresh_series`.
- Metadata providers: `tmdb` (TV+movies, key) and `jikan` (anime, no key).
  `get_provider(config, name)` selects one; tools take a `provider=` arg. The
  `jikan` provider hits a Jikan-compatible API whose base URL is
  `metadata.anime_api_url` (default Tenrai `api.tenrai.org/v1` — a Jikan v4
  mirror, since Jikan is being discontinued). These MAL mirrors flake with 5xx —
  the provider retries with backoff and enforces 3/s + 60/min via a shared
  `_RateLimiter`. Anime = season 1 with absolute episode numbers.
- Importer completeness (G7): subtitle sidecars (`SUBTITLE_EXTENSIONS`) are
  imported next to their video, matching its renamed base name and keeping any
  language/flag suffix; double/multi-episode files (`S01E01E02` / `S01E01-E02`,
  via `parsing.parse_multi_episode`) hardlink/copy the single file once and mark
  every episode it spans downloaded; movie packs import every feature-sized file
  (>= half the largest), the largest keeping the clean `Title (Year)` name.
- Season/batch packs: multi-file downloads are split per-episode on import
  (SxxExx or anime absolute). `grab()` marks EVERY episode a pack covers as
  grabbed via `_covered_episode_ids` (conservative: single ep, whole-season pack,
  anime range/batch only) so `rss_poll` (which re-checks episode status each
  iteration) won't double-grab singles while a pack downloads. `grab_season`
  finds the best pack (covers >=2 of the season's episodes).
- Anime absolute numbering: a provider sets `absolute_numbering=True`; add_series
  stores it on `series.absolute_numbering`. `parsing.title_matches_episode(...,
  absolute=)` and the importer dispatch to absolute parsing
  (`parse_absolute_episode` / `parse_absolute_range` / `is_batch`) ONLY for
  flagged series — never for standard TV, to avoid false `Show - 12` matches.
  Absolute parser is regex-heavy; if you touch it, keep the false-positive tests
  in test_parsing.py green (resolutions/years must not parse as episodes).
- Import existing library: `import_plex_library` / `App.import_from_plex` +
  `PlexNotifier.catalog()` register Plex shows/movies as owned catalog entries
  (provider=tmdb from Plex guids, else provider="plex"+ratingKey; movies marked
  downloaded; anime-section shows get absolute_numbering). Series have NO episodes
  until add_series activates them. `sections=[…]` scopes which libraries; dry_run
  returns `sections_available`.
- Tool-interface conventions (from a design review): config Literals appear in
  BOTH the pydantic model and the tool signature (FastMCP emits enum schema);
  config models set `validate_assignment=True` as a backstop. Search tools return
  `{query,count,releases}` envelopes; list_series/list_movies return compact rows
  (full=true for all fields, limit to cap). configure_* return the section they
  set. Renames done: set_monitored→set_series_monitored, scan_plex→plex_scan,
  download_status→get_download. Error contract unified: tools use `@tool`
  (= `@mcp.tool()` + `_guard`) so any exception becomes `{"error", "hint"}` (hint
  keyed off exception type); FastMCP emits no output schema so this is safe for
  list-returning tools too. Auth tools consolidated into one
  `auth_token(action, token)`. Optional config fields clear on `""` via
  `_set_opt` (None still = leave unchanged).
- Plex identity persistence (G5): `import_from_plex` stores each show's
  `series.plex_rating_key`/`plex_section`. `activate_series` prefers the stored
  rating key for `show_episodes` (reliable lookup instead of a title search) and
  forces `absolute_numbering` when `plex_section == plex.anime_section`, even if
  the metadata provider itself doesn't declare absolute numbering.
- `activate_series(series_id, provider, provider_id)`: converts a catalogued
  (plex-imported, no-episode) series into a monitored one — fetches episodes from
  the provider, re-keys the series row to that provider/id, and marks episodes
  Plex already has as downloaded (`PlexNotifier.show_episodes`; absolute anime =
  Plex file count → mark eps 1..N, standard = match (season,episode)).
- Plex auth: either a manual token (`configure_plex`) or browser login
  (`plexauth.py` PIN flow → `plex_login_start`/`plex_login_poll`, persistent
  `plex.client_id`, pending pin id in the `kv` table). `plex_discover_libraries`
  lists sections + on-disk paths for choosing root folders. When Plex runs
  natively (not a container) local == plex namespace, so no local↔plex mapping.
- Only qBittorrent, Prowlarr, Plex are implemented for their layers. The ABCs
  exist so new providers/clients slot in without touching `core`.

## Not yet implemented (good next tasks)

- Full Sonarr custom-format quality profiles + quality-upgrade replacement.
- Download clients beyond qBittorrent (Transmission/Deluge) via `DownloadClient`.
- Lidarr-style music.
- (Done — G6) Bulk-activate catalogued Plex imports: `bulk_activate_series`
  activates every episode-less, non-anime, tmdb-keyed series in one call
  (sequential, rate-limit-friendly) and reports anime/plex-only entries as
  skipped with the reason (a Plex tmdb id is not a MAL id).

## Deploy / run

- Local MCP client: stdio, `{ "command": "llmarr" }`.
- Persistent HTTP service: `LLMARR_TRANSPORT=streamable-http llmarr` — binds
  `127.0.0.1:8000`, endpoint `/mcp`, bearer-token protected. See README's
  Cloudflare Tunnel section for remote exposure.
- Config: `$LLMARR_CONFIG` (default `~/.config/llmarr/config.yaml`); state:
  `$LLMARR_DB` (default `~/.local/share/llmarr/llmarr.db`).
