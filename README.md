# Radio Station

A self-hosted internet radio station in a single Docker container. Uses your Plex library as the music source, streams via Icecast, and is controlled through a Matrix room with an AI DJ personality.

## Architecture

```
Plex Media Server  (music library)
        │
        ▼
  Liquidsoap        (audio engine: queues, playlists, smart radio)
        │
        ▼
  Icecast2          (stream server, internal port 8000)
        │
        ▼
  nginx             (public port 80 — web player, /api, /stream, auth)

  radio-service     (Python/FastAPI, internal port 8081)
    ├── track-change webhook from Liquidsoap → Matrix announcements
    ├── Matrix bot: !commands + AI DJ (natural language via litellm)
    ├── Session auth: Matrix login → signed cookie
    └── /api/now-playing, /api/album-art for the web player
```

All four processes run under **supervisord** in a single container.

---

## Prerequisites

### Plex token

1. Log in to [plex.tv](https://plex.tv) in a browser
2. Open any media item → `...` menu → **Get Info** → **View XML**
3. Copy `X-Plex-Token=` from the URL

### Matrix bot account

The bot needs its own Matrix account (separate from your personal one).

1. Register a new user on your homeserver (e.g. `@radiobot:example.com`)
2. Get an access token:
   ```bash
   curl -X POST 'https://matrix.example.com/_matrix/client/v3/login' \
     -H 'Content-Type: application/json' \
     -d '{"type":"m.login.password","identifier":{"type":"m.id.user","user":"radiobot"},"password":"yourpassword"}'
   ```
   Copy the `access_token` from the response.
3. Create a Matrix room for announcements — **disable encryption** (the bot uses `matrix-nio` without the E2E extra)
4. Invite the bot account and get the room's internal ID (Element: room settings → Advanced → Internal room ID, format `!abc123:example.com`)

### Last.fm API key (optional)

Required only for `!similar <artist>` smart radio. Get a free read-only key at [last.fm/api/account/create](https://www.last.fm/api/account/create).

---

## Quick start

```bash
cp .env.example .env
# Edit .env — fill in MUSIC_PATH, PLEX_*, MATRIX_*, and anything else you want
docker-compose up --build -d
```

The web player is at `http://localhost:8080`. On first load you'll be redirected to `/auth/login` — sign in with any Matrix account on your homeserver.

On startup the bot shuffles your full library and announces itself in the Matrix room.

---

## Configuration

All configuration is in `.env`. Copy `.env.example` to get started.

### Required

| Variable | Description |
|----------|-------------|
| `MUSIC_PATH` | Absolute path to your music library on the host |
| `PLEX_URL` | Plex server URL reachable from inside the container |
| `PLEX_TOKEN` | Your Plex authentication token |
| `MATRIX_HOMESERVER` | Your Matrix homeserver URL |
| `MATRIX_TOKEN` | Bot account access token |
| `MATRIX_USER_ID` | Bot's Matrix ID, e.g. `@radiobot:example.com` |
| `MATRIX_ROOM_ID` | Room ID for announcements, e.g. `!abc123:example.com` |
| `ALLOWED_MATRIX_USERS` | Comma-separated Matrix IDs allowed to send bot commands |

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `RADIO_WEB_PORT` | `8080` | Host port the web player is served on |
| `STREAM_URL` | — | Public stream URL shown in the bot greeting (use your reverse-proxy URL if applicable) |
| `STREAM_NAME` | — | Icecast stream name |
| `STREAM_BITRATE` | `320` | MP3 bitrate in kbps |
| `LASTFM_API_KEY` | — | Last.fm key for `!similar` smart radio |
| `SESSION_SECRET` | auto | HMAC key for session cookies; auto-generated into `/data/` if blank |
| `AI_MODEL` | — | litellm model string — see [AI DJ](#ai-dj) below |
| `AI_API_KEY` | — | API key for cloud AI providers |
| `AI_BASE_URL` | — | Custom endpoint URL (e.g. Ollama) |

---

## AI DJ

When `AI_MODEL` is set, the bot gains a natural-language personality (DJ Wahl). Mention the bot in the Matrix room to chat, ask what's playing, or control the radio conversationally. `!commands` continue to work as-is.

### Option A — Bundled Ollama (recommended for self-hosting)

Add to `.env`:

```
OLLAMA_MODEL=qwen2.5:3b
AI_MODEL=ollama/qwen2.5:3b
AI_BASE_URL=http://ollama:11434
```

Start with the `ai` profile — Ollama will pull the model on first run (~2 GB, cached in a Docker volume):

```bash
docker-compose --profile ai up --build -d
```

Uncomment the `deploy.resources` block in `docker-compose.yml` to enable NVIDIA GPU acceleration.

**Recommended models** (good tool-calling at small size):
- `qwen2.5:3b` — best tool-calling accuracy, ~2 GB
- `llama3.2:3b` — slightly more conversational, ~2 GB
- `qwen2.5:1.5b` — smallest option, ~1 GB

### Option B — External Ollama

```
AI_MODEL=ollama/qwen2.5:3b
AI_BASE_URL=http://192.168.1.x:11434
```

### Option C — Cloud provider

```
AI_MODEL=anthropic/claude-haiku-4-5-20251001
AI_API_KEY=sk-ant-...
```

Any [litellm-supported provider](https://docs.litellm.ai/docs/providers) works.

---

## Authentication

All routes (web player, `/api/`, `/stream`) require a session cookie. On first visit you're redirected to `/auth/login`.

Sign in with **any Matrix account on your homeserver** — the login page calls `/_matrix/client/v3/login` on your homeserver and issues a 30-day HMAC-signed session cookie on success.

To sign out: `http://yourhost:8080/auth/logout`

---

## Matrix commands

All commands require your Matrix user ID to be in `ALLOWED_MATRIX_USERS`.

| Command | Description |
|---------|-------------|
| `!np` | Show what's currently playing |
| `!request <query>` | Search Plex and queue the top result |
| `!skip` | Skip the current track |
| `!playlist <name>` | Switch to a Plex or shared playlist |
| `!similar <artist>` | Smart radio: similar artists via Last.fm (requires `LASTFM_API_KEY`) |
| `!genre <genre>` | Play tracks filtered by genre |
| `!random` | Shuffle your full library |
| `!playlists` | List all playlists (Plex + shared) |
| `!mode` | Show current playback mode |
| `!help` | Show all commands |

**Per-user playlist:**

| Command | Description |
|---------|-------------|
| `!save` | Save the currently playing track to your personal playlist |
| `!mylist` | Show your personal playlist |
| `!mylist play` | Start playing your personal playlist |
| `!mylist clear` | Clear your personal playlist |
| `!mylist remove <N>` | Remove track N from your personal playlist |

**Shared playlists** (any allowed user can manage):

| Command | Description |
|---------|-------------|
| `!createplaylist <name>` | Create a new shared playlist |
| `!addto <name> \| <query>` | Search Plex and add the top result to a shared playlist |
| `!removefrom <name> \| <N>` | Remove track N from a shared playlist |
| `!showplaylist <name>` | List tracks in a shared playlist |
| `!deleteplaylist <name>` | Delete a shared playlist |

Shared playlists are mirrored to Plex as `matrix_<name>` so they appear in Plexamp. If the Plex token lacks write permission the local playlist still works; the mirror is best-effort.

Track changes are announced automatically. If AI is enabled, **@-mention the bot** to chat naturally — it can control the radio through conversation too.

---

## Development

### Running tests

Tests live in `src/radio-service/tests/` and are built into the container image. Run them inside the container where all dependencies are installed:

```bash
docker exec radio python3 -m pytest /app/tests/ -v
```

The test suite covers import-time errors (catches Python version incompatibilities), auth token logic, request parsing, and Plex path translation.

### Linting and formatting

`flake8` and `autopep8` are included in `requirements.txt` and available in the container:

```bash
# check for violations
docker exec radio python3 -m flake8 /app/app/ /app/tests/

# auto-fix formatting in place (local dev)
python3 -m autopep8 --in-place --recursive src/radio-service/app/
```

Config: `src/radio-service/.flake8` (max line 100, E203/E501 suppressed) and `src/radio-service/setup.cfg` (autopep8 matching line length).

### Pre-commit hook

A git pre-commit hook is checked in at `hooks/pre-commit`. Enable it once per checkout:

```bash
git config core.hooksPath hooks
```

On each commit it auto-formats staged `.py` files with autopep8 (re-staging any changes) then runs flake8. The commit is blocked if flake8 finds any violations. The hook is a no-op if the tools aren't installed locally.

---

## Ansible deployment

### 1. Configure inventory

Edit `ansible/inventory/hosts.yml`:

```yaml
all:
  children:
    radio_station:
      hosts:
        myserver:
          ansible_host: 192.168.1.x
          ansible_user: youruser
```

Edit `ansible/inventory/group_vars/all.yml` for non-secret config.

### 2. Configure secrets

```bash
cp ansible/inventory/group_vars/vault.yml.example ansible/inventory/group_vars/vault.yml
ansible-vault encrypt ansible/inventory/group_vars/vault.yml
ansible-vault edit ansible/inventory/group_vars/vault.yml
```

### 3. Deploy

```bash
cd ansible
ansible-playbook playbook.yml --ask-vault-pass
```

Re-running is safe — rebuilds and recreates the container with updated config.

**Requirements:** Docker on the target host; Ansible collections:
```bash
ansible-galaxy collection install community.docker ansible.posix
```

---

## Operations

### Logs

```bash
docker logs -f radio-station_radio-station_1
```

### Process status

```bash
docker exec radio-station_radio-station_1 supervisorctl -s unix:///tmp/supervisor.sock status
```

### Restart a single process

```bash
docker exec radio-station_radio-station_1 supervisorctl -s unix:///tmp/supervisor.sock restart radio-service
docker exec radio-station_radio-station_1 supervisorctl -s unix:///tmp/supervisor.sock restart liquidsoap
```

### Icecast admin (internal only)

```bash
docker exec radio-station_radio-station_1 \
  wget -qO- http://localhost:8000/admin/stats.xml \
  --user admin --password "$ICECAST_ADMIN_PASSWORD"
```

---

## Project structure

```
radio-station/
├── docker-compose.yml        # local deployment (--profile ai adds Ollama)
├── .env.example              # copy to .env
├── hooks/
│   └── pre-commit            # autopep8 + flake8 on staged .py files
├── src/
│   ├── Dockerfile            # single-container image
│   ├── entrypoint.sh         # renders config templates from env, starts supervisord
│   ├── supervisord.conf      # icecast → radio-service → liquidsoap → nginx
│   ├── config/               # templates rendered at container startup via envsubst
│   │   ├── icecast.xml.tmpl
│   │   ├── radio.liq.tmpl
│   │   └── nginx.conf.tmpl
│   ├── radio-service/
│   │   ├── requirements.txt
│   │   ├── pytest.ini              # testpaths, pythonpath, asyncio_mode=auto
│   │   ├── .flake8                 # max-line-length=100, ignore E203/E501
│   │   ├── setup.cfg               # autopep8 max-line-length=100
│   │   ├── app/
│   │   │   ├── main.py             # FastAPI app, bot command handlers, track webhook
│   │   │   ├── matrix_bot.py       # matrix-nio client
│   │   │   ├── ai_client.py        # litellm wrapper, DJ personality, tool loop
│   │   │   ├── auth.py             # Matrix login validation, session cookie signing
│   │   │   ├── plex_client.py      # Plex API: search, playlists, album art, path translation
│   │   │   ├── liquidsoap_client.py # telnet client: skip, push request
│   │   │   ├── lastfm_client.py    # similar-artist lookup
│   │   │   ├── config.py           # pydantic-settings from env
│   │   │   └── models.py           # Pydantic models
│   │   └── tests/
│   │       ├── conftest.py         # env vars + stubbed external deps (plexapi, nio, litellm)
│   │       ├── test_imports.py     # import-time checks for all modules (catches annotation errors)
│   │       ├── test_auth.py        # token roundtrip, expiry, tampering
│   │       ├── test_main.py        # _parse_request_query, _path_to_label, _find_shared
│   │       └── test_plex_client.py # to_liquidsoap_path path translation
│   └── web/
│       ├── index.html         # web player: search, playlists, album art, ambient background
│       └── login.html         # Matrix auth login page
└── ansible/
    ├── playbook.yml
    ├── inventory/
    │   ├── hosts.yml
    │   └── group_vars/
    │       ├── all.yml              # non-secret config
    │       └── vault.yml.example
    └── roles/radio_station/
        ├── defaults/main.yml
        ├── tasks/main.yml
        └── handlers/main.yml
```

---

## License

GPL v3 — see [LICENSE](LICENSE).
