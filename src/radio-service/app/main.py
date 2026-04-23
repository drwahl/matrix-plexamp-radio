import asyncio
import logging
import random
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Form, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse

from app import auth
from app.ai_client import AIClient
from app.config import settings
from app.lastfm_client import LastFMClient
from app.liquidsoap_client import LiquidsoapClient
from app.matrix_bot import MatrixBot
from app.models import NowPlaying
from app.plex_client import PlexClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PLAYLIST_FILE = "/data/background.m3u"
MODE_FILE = "/data/mode"
MAX_PLAYLIST_TRACKS = 1500  # Liquidsoap's OCaml List.map blows the stack on very large playlists

LOGIN_HTML = (Path(__file__).parent.parent / "web" / "login.html").read_text()

# Shared state
now_playing = NowPlaying()
now_playing_thumb: str = ""
current_mode: str = "random"
_secret: bytes = b""

# Services
plex = PlexClient(settings.plex_url, settings.plex_token)
liquidsoap = LiquidsoapClient(settings.liquidsoap_host, settings.liquidsoap_port)
lastfm = LastFMClient(settings.lastfm_api_key) if settings.lastfm_api_key else None
ai = AIClient(settings.ai_model, settings.ai_api_key, settings.ai_base_url) if settings.ai_model else None
bot = MatrixBot(
    settings.matrix_homeserver,
    settings.matrix_token,
    settings.matrix_user_id,
    settings.matrix_room_id,
    settings.allowed_users_list,
)


def write_playlist(paths: list[str]) -> None:
    with open(PLAYLIST_FILE, "w") as f:
        f.write("\n".join(paths[:MAX_PLAYLIST_TRACKS]))


def set_mode(mode: str) -> None:
    global current_mode
    current_mode = mode
    with open(MODE_FILE, "w") as f:
        f.write(mode)


def load_mode() -> str:
    try:
        with open(MODE_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


async def _ai_tool_handler(name: str, inputs: dict) -> str:
    if name == "request_track":
        results = plex.search_tracks(inputs["query"])
        if not results:
            return f"No tracks found for: {inputs['query']}"
        track = results[0]
        ok = await liquidsoap.push_request(track["path"])
        return f"Queued: {track['artist']} — {track['title']}" if ok else "Failed to queue track"

    if name == "skip_track":
        await liquidsoap.skip()
        return "Skipped"

    if name == "play_playlist":
        tracks = plex.get_playlist_tracks(inputs["name"])
        if not tracks:
            playlists = plex.list_playlists()
            return f"Playlist not found. Available: {', '.join(playlists)}"
        write_playlist(tracks)
        set_mode(f"playlist:{inputs['name']}")
        return f"Switched to playlist: {inputs['name']} ({len(tracks)} tracks)"

    if name == "similar_artist_radio":
        if lastfm is None:
            return "Last.fm not configured — similar artist radio unavailable"
        similar_artists = lastfm.get_similar_artists(inputs["artist"])
        if not similar_artists:
            return f"No similar artists found for: {inputs['artist']}"
        all_tracks: list[str] = []
        for artist in similar_artists:
            all_tracks.extend(plex.get_tracks_by_artist(artist))
        if not all_tracks:
            return "Found similar artists but none are in your library"
        random.shuffle(all_tracks)
        write_playlist(all_tracks)
        set_mode(f"similar:{inputs['artist']}")
        return f"Smart radio: similar to {inputs['artist']} — {len(all_tracks)} tracks"

    if name == "genre_radio":
        tracks = plex.get_tracks_by_genre(inputs["genre"])
        if not tracks:
            return f"No tracks found for genre: {inputs['genre']}"
        random.shuffle(tracks)
        write_playlist(tracks)
        set_mode(f"genre:{inputs['genre']}")
        return f"Playing genre: {inputs['genre']} ({len(tracks)} tracks)"

    if name == "random_shuffle":
        tracks = plex.get_all_tracks()
        random.shuffle(tracks)
        write_playlist(tracks)
        set_mode("random")
        return f"Full library shuffle ({len(tracks)} tracks)"

    if name == "list_playlists":
        playlists = plex.list_playlists()
        return "Available playlists: " + (", ".join(playlists) or "none")

    return f"Unknown tool: {name}"


async def handle_ai_message(sender: str, body: str) -> None:
    if ai is None:
        return
    ctx_parts: list[str] = []
    if now_playing.title:
        ctx_parts.append(f"{now_playing.artist} — {now_playing.title}")
        if now_playing.album:
            ctx_parts.append(f"from {now_playing.album}")
    if current_mode:
        ctx_parts.append(f"mode: {current_mode}")
    np_context = ", ".join(ctx_parts)

    response = await ai.chat(body, np_context, _ai_tool_handler)
    if response:
        await bot.send_message(response)


ALIASES: dict[str, str] = {
    "!next":    "!skip",
    "!shuffle": "!random",
    "!play":    "!request",
    "!playing": "!np",
}


async def handle_command(sender: str, cmd: str, args: str) -> None:
    cmd = ALIASES.get(cmd, cmd)

    if cmd == "!np":
        if now_playing.title:
            msg = f"Now Playing: {now_playing.artist} — {now_playing.title}"
            if now_playing.album:
                msg += f"\n  {now_playing.album}"
            msg += f"\n  Mode: {current_mode}"
        else:
            msg = "Nothing playing yet."
        await bot.send_message(msg)

    elif cmd == "!request":
        if not args:
            await bot.send_message("Usage: !request <search terms>")
            return
        results = plex.search_tracks(args)
        if not results:
            await bot.send_message(f"No tracks found: {args}")
            return
        track = results[0]
        ok = await liquidsoap.push_request(track["path"])
        if ok:
            await bot.send_message(f"Queued: {track['artist']} — {track['title']}")
        else:
            await bot.send_message("Failed to queue track.")

    elif cmd == "!skip":
        await liquidsoap.skip()
        await bot.send_message("Skipped.")

    elif cmd == "!playlist":
        if not args:
            playlists = plex.list_playlists()
            await bot.send_message("Available playlists: " + (", ".join(playlists) or "none"))
            return
        tracks = plex.get_playlist_tracks(args)
        if not tracks:
            playlists = plex.list_playlists()
            await bot.send_message(
                f"Playlist '{args}' not found.\nAvailable: " + (", ".join(playlists) or "none")
            )
            return
        write_playlist(tracks)
        set_mode(f"playlist:{args}")
        await bot.send_message(f"Switched to playlist: {args} ({len(tracks)} tracks)")

    elif cmd == "!similar":
        if lastfm is None:
            await bot.send_message("!similar is not available (no LASTFM_API_KEY configured).")
            return
        if not args:
            await bot.send_message("Usage: !similar <artist>")
            return
        await bot.send_message(f"Building smart radio for: {args}...")
        similar_artists = lastfm.get_similar_artists(args)
        if not similar_artists:
            await bot.send_message(f"Couldn't find similar artists for: {args}")
            return
        all_tracks: list[str] = []
        for artist in similar_artists:
            all_tracks.extend(plex.get_tracks_by_artist(artist))
        if not all_tracks:
            await bot.send_message(
                f"Found {len(similar_artists)} similar artists but none are in your library."
            )
            return
        random.shuffle(all_tracks)
        write_playlist(all_tracks)
        set_mode(f"similar:{args}")
        await bot.send_message(
            f"Smart radio: similar to {args} — {len(all_tracks)} tracks from {len(similar_artists)} artists"
        )

    elif cmd == "!genre":
        if not args:
            await bot.send_message("Usage: !genre <genre>")
            return
        tracks = plex.get_tracks_by_genre(args)
        if not tracks:
            await bot.send_message(f"No tracks found for genre: {args}")
            return
        random.shuffle(tracks)
        write_playlist(tracks)
        set_mode(f"genre:{args}")
        await bot.send_message(f"Genre: {args} ({len(tracks)} tracks)")

    elif cmd == "!random":
        await bot.send_message("Shuffling full library...")
        tracks = plex.get_all_tracks()
        random.shuffle(tracks)
        write_playlist(tracks)
        set_mode("random")
        await bot.send_message(f"Full library shuffle ({len(tracks)} tracks)")

    elif cmd == "!mode":
        await bot.send_message(f"Current mode: {current_mode}")

    elif cmd == "!playlists":
        playlists = plex.list_playlists()
        await bot.send_message("Playlists: " + (", ".join(playlists) or "none"))

    elif cmd == "!help":
        await bot.send_message(HELP_TEXT)


HELP_TEXT = (
    "Commands:\n"
    "  !np / !playing        — now playing\n"
    "  !request / !play      — queue a track\n"
    "  !skip / !next         — skip current track\n"
    "  !playlist <name>      — switch to Plex playlist\n"
    "  !similar <artist>     — smart radio similar to artist\n"
    "  !genre <genre>        — play by genre\n"
    "  !random / !shuffle    — shuffle full library\n"
    "  !playlists            — list Plex playlists\n"
    "  !mode                 — show current mode\n"
    + ("  Or just @-mention me and chat!" if ai else "")
)

bot.command_handler = handle_command
bot.ai_handler = handle_ai_message


async def _on_bot_ready() -> None:
    saved_mode = load_mode()
    if saved_mode and saved_mode != "random":
        set_mode(saved_mode)
        resume_msg = f"Resuming: {saved_mode}"
    else:
        tracks = plex.get_all_tracks()
        if tracks:
            random.shuffle(tracks)
            write_playlist(tracks)
        set_mode("random")
        resume_msg = f"Playing: random shuffle ({len(tracks) if tracks else 0} tracks)"
    listen_line = f"\nListen in: {settings.stream_url}" if settings.stream_url else ""
    ai_line = "\nAI DJ is online — @-mention me to chat." if ai else ""
    await bot.send_message(f"Radio bot online!{listen_line}\n{resume_msg}{ai_line}\n\n{HELP_TEXT}")


bot.on_ready = _on_bot_ready


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _secret
    _secret = auth.load_or_create_secret(settings.session_secret)
    bot_task = asyncio.create_task(bot.run())
    logger.info("Matrix bot started")
    yield
    bot_task.cancel()
    try:
        await bot_task
    except asyncio.CancelledError:
        pass


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.get("/auth/login", response_class=HTMLResponse)
async def login_page() -> HTMLResponse:
    return HTMLResponse(LOGIN_HTML)


@app.post("/auth/login")
async def do_login(username: str = Form(...), password: str = Form(...)) -> Response:
    user_id = await auth.matrix_login(settings.matrix_homeserver, username, password)
    if not user_id:
        return RedirectResponse("/auth/login?error=Invalid+credentials", status_code=302)
    token = auth.make_token(user_id, _secret)
    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        auth.COOKIE_NAME,
        token,
        max_age=auth.SESSION_DURATION,
        httponly=True,
        samesite="lax",
    )
    return response


@app.get("/auth/logout")
async def logout() -> Response:
    response = RedirectResponse("/auth/login", status_code=302)
    response.delete_cookie(auth.COOKIE_NAME)
    return response


@app.get("/internal/auth-check")
async def auth_check(request: Request) -> Response:
    token = request.cookies.get(auth.COOKIE_NAME)
    if token and auth.verify_token(token, _secret):
        return Response(status_code=200)
    return Response(status_code=401)


# ── Internal webhook (called by Liquidsoap directly on :8081, not via nginx) ──

@app.post("/internal/track-changed")
async def track_changed(
    title: str = Form(default=""),
    artist: str = Form(default=""),
    album: str = Form(default=""),
    filename: str = Form(default=""),
) -> dict:
    global now_playing, now_playing_thumb

    if title == now_playing.title and artist == now_playing.artist:
        return {"ok": True}

    now_playing = NowPlaying(title=title, artist=artist, album=album, mode=current_mode)
    now_playing_thumb = ""

    try:
        candidates = plex.search_tracks(title, limit=10)
        if candidates:
            norm = lambda s: s.lower().replace(" ", "").replace("-", "")
            artist_norm = norm(artist)
            match = next(
                (t for t in candidates if norm(t["artist"]) in artist_norm or artist_norm in norm(t["artist"])),
                candidates[0],
            )
            if match.get("thumb"):
                now_playing_thumb = match["thumb"]
                now_playing.has_album_art = True
    except Exception:
        logger.warning("Album art lookup failed for: %s - %s", artist, title)

    try:
        await bot.send_now_playing(title, artist, album)
    except Exception:
        logger.exception("Matrix announcement failed")

    return {"ok": True}


# ── Public API ────────────────────────────────────────────────────────────────

@app.get("/api/now-playing")
async def get_now_playing() -> NowPlaying:
    return now_playing


@app.get("/api/album-art")
async def get_album_art() -> Response:
    if not now_playing_thumb:
        return Response(status_code=404)
    url = plex.get_thumbnail_url(now_playing_thumb)
    async with httpx.AsyncClient() as client:
        r = await client.get(url)
    return Response(content=r.content, media_type=r.headers.get("content-type", "image/jpeg"))
