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
  metadata/      MetadataProvider ABC + TMDB (TV + movies)
  indexers/      Prowlarr search client + Release model
  download/      DownloadClient ABC + qBittorrent
  notify/        Plex library scan
  importer.py    hardlink/copy/move into <root>/Show (Year)/Season NN/… + movie layout
  selector.py    quality filter + ranking (lightweight, NOT Sonarr custom formats)
  parsing.py     SxxExx / season-pack / resolution parsing
  core.py        App engine — the glue used by BOTH tools and the RSS poller
  rss/poller.py  asyncio background loop (auto-grab + import), reads config each tick
  auth.py        static bearer-token middleware for the HTTP transport
  server.py      FastMCP instance + all ~45 tools + lifespan (starts poller)
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
  mappings; unmapped paths then raise. The author runs single-host.
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
- `@mcp.tool()` returns the **plain function**, so server tools are called
  directly in tests after `monkeypatch.setattr(server.state, "app", app)`.
- The importer is tested against **real files + real hardlinks** (inode checks).

## Gotchas

- `db.upsert_episode` / `upsert_movie` deliberately do NOT overwrite
  `status`/`file_path` on metadata refresh — only title/air_date update.
- Secrets (`api_key`, `token`, `password`, `auth_token`) are masked by
  `get_config`; reveal the auth token via the dedicated `get_auth_token` tool.
- The RSS poller re-reads config each tick, so config changes apply without a
  restart (except the HTTP auth token, which is bound at server start).
- Metadata providers: `tmdb` (TV+movies, key) and `jikan` (anime, no key).
  `get_provider(config, name)` selects one; tools take a `provider=` arg. The
  `jikan` provider hits a Jikan-compatible API whose base URL is
  `metadata.anime_api_url` (default Tenrai `api.tenrai.org/v1` — a Jikan v4
  mirror, since Jikan is being discontinued). These MAL mirrors flake with 5xx —
  the provider retries with backoff and enforces 3/s + 60/min via a shared
  `_RateLimiter`. Anime = season 1 with absolute episode numbers.
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

- Full Sonarr custom-format quality profiles.
- Double-episode file parsing (`S01E01E02`).
- Download clients beyond qBittorrent (Transmission/Deluge) via `DownloadClient`.
- Lidarr-style music. OAuth for MCP clients that require it.

## Deploy / run

- Local MCP client: stdio, `{ "command": "llmarr" }`.
- Persistent HTTP service: `LLMARR_TRANSPORT=streamable-http llmarr` — binds
  `127.0.0.1:8000`, endpoint `/mcp`, bearer-token protected. See README's
  Cloudflare Tunnel section for remote exposure.
- Config: `$LLMARR_CONFIG` (default `~/.config/llmarr/config.yaml`); state:
  `$LLMARR_DB` (default `~/.local/share/llmarr/llmarr.db`).
