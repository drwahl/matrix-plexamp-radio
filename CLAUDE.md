# CLAUDE.md — Radio Station

## What this is

A self-hosted internet radio station in a single Docker container. Plex is the music library. Liquidsoap is the audio engine. Icecast2 serves the stream. A Python/FastAPI service handles the Matrix bot and now-playing state. nginx is the public-facing reverse proxy.

## Container internals

Four processes run under supervisord (started in priority order):

| Process | Port | Role |
|---------|------|------|
| icecast2 | 8000 (internal) | stream server |
| radio-service (uvicorn) | 8081 (internal) | Python API + Matrix bot |
| liquidsoap | 1234 (internal, telnet) | audio engine |
| nginx | 80 (exposed) | web player, /api proxy, /stream proxy |

Only port 80 is exposed. All inter-process communication is over localhost.

## Config templating

Configs are **not** baked into the image. At container startup, `entrypoint.sh` runs `envsubst` to render three `*.tmpl` files from environment variables:

```
src/config/icecast.xml.tmpl  → /etc/icecast2/icecast.xml
src/config/radio.liq.tmpl    → /etc/radio/radio.liq
src/config/nginx.conf.tmpl   → /etc/nginx/conf.d/default.conf
```

**Important**: `envsubst` in `nginx.conf.tmpl` uses an explicit variable list (`'${STREAM_MOUNT}'`) to avoid substituting nginx's own variables like `$host` and `$uri`. If you add new env vars to `nginx.conf.tmpl`, add them to that list in `entrypoint.sh`.

## Audio pipeline

```
request.queue (id="requests")  ← !request command pushes here
        │
        ▼ (fallback, track_sensitive=true)
playlist("/data/background.m3u")  ← radio-service rewrites this file on mode change
        │
        ▼
   on_metadata hook → HTTP POST to radio-service:8081/internal/track-changed
        │
        ▼
   output.icecast → icecast:8000/stream
```

The background playlist file is watched by Liquidsoap with `reload_mode="watch"` — changes are picked up immediately via inotify. The radio-service writes the file synchronously (it's small); Liquidsoap picks it up on the next track boundary.

## Now-playing push flow

1. Liquidsoap `on_metadata` fires (may fire multiple times per track — deduplicated in radio-service)
2. Liquidsoap POSTs form-encoded data to `http://localhost:8081/internal/track-changed`
3. `radio-service/app/main.py::track_changed()` checks for duplicates, updates `now_playing` state, fetches album art from Plex, calls `bot.send_now_playing()`
4. Matrix bot sends announcement to the room
5. Web player polls `/api/now-playing` every 5s and updates the UI

## Plex path translation

Plex reports file paths as it sees them on its own disk (e.g. `/mnt/nas/music/Artist/track.flac`). Liquidsoap needs paths as seen inside the container (e.g. `/music/Artist/track.flac`). The translation happens in `plex_client.py::to_liquidsoap_path()`: at startup, `PlexClient` queries Plex for its library locations (`self._music.locations`) and caches them as `_plex_roots`. Any matching prefix is stripped and `/music/` is prepended.

If Plex's reported library root doesn't match `/music` in the container, tracks will fail to queue silently. Check with `docker exec radio-station cat /data/background.m3u | head -3`.

## Key files

| File | Purpose |
|------|---------|
| `src/radio-service/app/main.py` | FastAPI app, all bot command handlers, track-changed webhook |
| `src/radio-service/app/matrix_bot.py` | matrix-nio bot — sends messages, dispatches commands |
| `src/radio-service/app/plex_client.py` | Plex API — search, playlists, path translation, thumbnails, Plex sync |
| `src/radio-service/app/lastfm_client.py` | Last.fm — similar artist lookup for `!similar` |
| `src/radio-service/app/liquidsoap_client.py` | Telnet client — skip, push request, query on-air |
| `src/radio-service/app/ai_client.py` | AI DJ engine — litellm-backed chat with agentic tool loop |
| `src/radio-service/app/auth.py` | Session tokens — HMAC-SHA256 signed cookies, Matrix login validation |
| `src/radio-service/app/config.py` | Pydantic settings — all env vars and their defaults |
| `src/config/radio.liq.tmpl` | Liquidsoap script — sources, fallback chain, on_metadata hook, Icecast output |
| `src/entrypoint.sh` | Container init — renders configs, seeds background.m3u, execs supervisord |
| `src/supervisord.conf` | Process definitions and startup ordering |
| `src/radio-service/tests/conftest.py` | pytest fixtures — env vars + stubbed external deps |
| `src/radio-service/.flake8` | flake8 config (max-line-length=100, E203/E501 suppressed) |
| `src/radio-service/setup.cfg` | autopep8 config (matching max-line-length) |
| `hooks/pre-commit` | Git pre-commit hook — autopep8 auto-fix then flake8 lint |

## Three-tier playlist system

There are three distinct playlist types — never mix them up:

| Type | Storage | Who can modify | Plex mirror |
|------|---------|----------------|-------------|
| **Plex native** | Plex server | Nobody (read-only via this system) | Authoritative source |
| **User** | `/data/user_playlists.json`, keyed by Matrix user ID | Owner only (identified by session cookie) | No |
| **Shared** | `/data/shared_playlists.json` | Any authenticated user | Yes — `matrix_<name>` in Plex |

`!playlist <name>` checks shared playlists first, then Plex native. `!playlists` shows both with labels. `list_playlists()` in `plex_client.py` filters out `matrix_` prefixed playlists so they don't appear as a separate Plex entry in listings.

Shared playlist mutations always call `_sync_to_plex()` (full delete + recreate of `matrix_<name>`). Plex sync failures are logged and non-fatal — local state is always authoritative.

Track dicts include a `key` field (the Plex item key, e.g. `/library/metadata/12345`). This is required for Plex sync — `PlexServer.fetchItem(key)` resolves the track object. Keys are stored in both user and shared playlist JSON.

## Adding a new bot command

All commands are handled in `main.py::handle_command()`. The pattern is:

```python
elif cmd == "!mycommand":
    # args is everything after the command, already stripped
    tracks = plex.some_method(args)
    write_playlist(tracks)
    set_mode(f"mymode:{args}")   # persists to /data/mode; survives restart
    await bot.send_message(f"Done: {len(tracks)} tracks")
```

`write_playlist()` writes to `/data/background.m3u`. Liquidsoap picks it up automatically. Use `set_mode()` (not direct assignment) so the mode persists across container restarts. Add the command to `!help` too.

## Testing

Tests are built into the container image. Run them inside the container where all dependencies are installed:

```bash
docker exec radio python3 -m pytest /app/tests/ -v
```

**Test files:**

- `test_imports.py` — one test per app module; catches annotation errors and any import-time crash (e.g. Python 3.9 `str | None` syntax)
- `test_auth.py` — token roundtrip, wrong secret, tampered payload/signature, expired token, malformed tokens, Matrix IDs with colons, secret file persistence
- `test_main.py` — `_parse_request_query`, `_path_to_label`, `_find_shared`
- `test_plex_client.py` — `to_liquidsoap_path` path translation (single root, trailing slash, no match, multiple roots, no double-slash)

`conftest.py` sets required env vars and stubs `plexapi`, `nio`, `litellm`, and `pylast` via `sys.modules.setdefault()` so tests run without live services. The PlexServer mock is wired so `PlexClient.__init__` succeeds with a `locations = ["/mnt/music"]` default.

**Python version note:** All app modules use `from __future__ import annotations` at the top. This defers annotation evaluation so modern union syntax (`X | None`, `list[str]`) works on Python 3.9 (the version in the container). Do not remove this import and do not use bare `str | None` annotations in new files without it.

## Linting and formatting

`flake8` and `autopep8` are installed in the container alongside app dependencies:

```bash
# check for violations
docker exec radio python3 -m flake8 /app/app/ /app/tests/

# auto-fix formatting in place (local dev)
python3 -m autopep8 --in-place --recursive src/radio-service/app/
```

Config lives in `src/radio-service/.flake8` (max line 100, E203/E501 suppressed) and `src/radio-service/setup.cfg` (autopep8 matching line length).

A git pre-commit hook at `hooks/pre-commit` auto-runs both tools on staged Python files. Enable it once per checkout:

```bash
git config core.hooksPath hooks
```

The hook auto-formats with autopep8 (re-staging any changes), then runs flake8 and blocks the commit on violations. It's a no-op if the tools aren't installed locally.

## AI DJ (optional)

Set `AI_MODEL` to enable the AI DJ feature. The model string is a litellm provider/model pair:

```
AI_MODEL=anthropic/claude-haiku-4-5-20251001   # Anthropic API (set AI_API_KEY)
AI_MODEL=ollama/llama3.2                        # local Ollama (set AI_BASE_URL)
AI_MODEL=openai/gpt-4o-mini                     # OpenAI (set AI_API_KEY)
```

When enabled, any Matrix message that @-mentions the bot (by full user ID or localpart) is routed to `handle_ai_message()` instead of the command parser. The AI responds as "DJ Wahl" and has access to all the same radio controls as the bot commands (request, skip, playlist, similar, genre, random, list playlists) via an agentic tool loop in `ai_client.py`. Conversation history is bounded to the last 20 messages (10 turns) — it's shared across all room members.

The bot detects @-mentions by checking whether the bot's full Matrix ID or its localpart (the part before the `:`) appears in the message body.

**Critical — AI handler must not block the sync loop.** The matrix-nio sync loop awaits callbacks inline. If `ai_handler` were awaited directly, a slow or hanging AI call would freeze all Matrix event processing (including `!commands`). The handler is therefore fired as `asyncio.create_task(self._run_ai(...))` so the sync loop continues immediately. `ai_client.py` uses `timeout=30, num_retries=0` on all litellm calls to prevent runaway retries if the backend (e.g. Ollama) is killed.

## Authentication

All web routes except `/auth/*` are protected by nginx's `auth_request` directive, which calls `/internal/auth-check` on every request. `auth.py` issues HMAC-SHA256 signed session cookies (30-day expiry, cookie name `radio_session`). Login at `/auth/login` validates credentials against the Matrix homeserver's `/_matrix/client/v3/login` endpoint — no separate user database.

`SESSION_SECRET` is the HMAC key. If not set, one is auto-generated on first startup and persisted to `/data/session_secret` so it survives container restarts. If you want to invalidate all sessions, delete that file or change the env var.

If you add a new nginx route that should be public (no login required), add it before the `auth_request` block or mirror the `/auth/` location block.

## Liquidsoap syntax notes

The image uses `savonet/liquidsoap:main` (Liquidsoap 2.x). Syntax differences from 1.x that matter here:

- Settings use `:=` not `set()`  → `settings.server.telnet := true`
- `thread.run(fast=false, f)` where `f` is `unit -> unit` — used to run HTTP calls off the audio thread
- `url.encode()` for percent-encoding form data values
- `http.post()` is synchronous — always wrap in `thread.run` when called from `on_metadata`

## Deployment

**Local:**
```bash
cp .env.example .env  # fill in values
docker compose up --build
```

To run with a local Ollama instance for the AI DJ:
```bash
docker compose --profile ai up --build
```

**Ansible:**
```bash
cd ansible
ansible-playbook playbook.yml --ask-vault-pass
```

Secrets live in `ansible/inventory/group_vars/vault.yml` (ansible-vault encrypted). Non-secret config is in `ansible/inventory/group_vars/all.yml`.

## Iterating locally

After changing Python code, rebuild is required:
```bash
docker compose up --build
```

After changing a `.tmpl` config, rebuild is also required (they're baked into the image). Alternatively, exec into the container and re-run `entrypoint.sh` logic manually:
```bash
docker exec -e STREAM_MOUNT=/stream radio-station \
  envsubst '${STREAM_MOUNT}' < /etc/radio/nginx.conf.tmpl > /etc/nginx/conf.d/default.conf
docker exec radio-station supervisorctl restart nginx
```

## Volumes

- `/music` — bind-mounted music library (read-only). The path inside the container must match what `to_liquidsoap_path()` produces — it always prepends `/music/`.
- `/data` — named Docker volume. Contains `background.m3u` (the live playlist), `mode` (persisted playback mode), and `session_secret` (HMAC key). All three survive container restarts.

## Gotchas

- **Empty background.m3u on first run**: Liquidsoap will start but produce silence until a mode command is sent (`!random`, `!playlist`, etc.). The entrypoint creates an empty file so Liquidsoap doesn't crash, but it won't play anything useful until the file has content.
- **on_metadata fires multiple times**: Liquidsoap can emit metadata events more than once for the same track (e.g. once when buffered, once when playing). The dedup check in `track_changed()` compares title+artist and drops duplicates.
- **Album art proxying**: Plex thumbnail URLs require the Plex token. The token is never sent to the browser — `/api/album-art` proxies the image server-side. Don't change this to a redirect.
- **Matrix E2E encryption**: `matrix-nio` is installed without the `[e2e]` extra. The bot will not work in E2E-encrypted rooms. The radio room should have encryption disabled.
- **ALLOWED_MATRIX_USERS must be set**: If the env var is empty (the default), the bot's allowed-users set is empty and it will silently ignore all messages — including `!help`. Set it to a comma-separated list of full Matrix IDs.
- **Playlist size is capped at 1500 tracks** (`MAX_PLAYLIST_TRACKS` in `main.py`). Liquidsoap's OCaml runtime blows the stack on very large in-memory lists; silently truncating at 1500 avoids the crash.
- **`!skip` has dual behavior**: if a `!request`-queued track is currently on air, it sends `requests.skip`; otherwise it sends `background.skip`. This is handled in `liquidsoap_client.py::skip()`.
