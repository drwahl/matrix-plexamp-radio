from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

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
LAST_PLAYED_FILE = "/data/last_played"
USER_PLAYLISTS_FILE = "/data/user_playlists.json"
SHARED_PLAYLISTS_FILE = "/data/shared_playlists.json"
MAX_PLAYLIST_TRACKS = 1500  # Liquidsoap's OCaml List.map blows the stack on very large playlists

LOGIN_HTML = (Path(__file__).parent.parent / "web" / "login.html").read_text()

# Shared state
now_playing = NowPlaying()
now_playing_thumb: str = ""
current_track: dict = {}   # full Plex track dict for the current track; empty if unknown
current_mode: str = "random"
current_filename: str = ""
_secret: bytes = b""

# Services
plex = PlexClient(settings.plex_url, settings.plex_token)
liquidsoap = LiquidsoapClient(settings.liquidsoap_host, settings.liquidsoap_port)
lastfm = LastFMClient(settings.lastfm_api_key) if settings.lastfm_api_key else None
ai = AIClient(settings.ai_model, settings.ai_api_key,
              settings.ai_base_url) if settings.ai_model else None
bot = MatrixBot(
    settings.matrix_homeserver,
    settings.matrix_token,
    settings.matrix_user_id,
    settings.matrix_room_id,
    settings.allowed_users_list,
)


def _parse_request_query(query: str) -> tuple[str, str]:
    """Return (artist, title). Either may be empty."""
    if " - " in query:
        artist, _, title = query.partition(" - ")
        return artist.strip(), title.strip()
    lower = query.lower()
    # "title by artist" — only match " by " not at the very start
    idx = lower.rfind(" by ")
    if idx > 0:
        return query[idx + 4:].strip(), query[:idx].strip()
    return "", query.strip()


def _path_to_label(path: str) -> str:
    stem = Path(path).stem
    if " - " in stem:
        artist_part, _, title_part = stem.partition(" - ")
        return f"{artist_part} — {title_part}"
    return stem


def write_playlist(paths: list[str]) -> None:
    with open(PLAYLIST_FILE, "w") as f:
        f.write("\n".join(paths[:MAX_PLAYLIST_TRACKS]))


def restore_queue_position() -> None:
    """On startup, trim background.m3u to resume near the last known track.

    Called once from lifespan startup — never during live playback.
    Modifying background.m3u while Liquidsoap is actively playing causes it
    to reload and replay the current track (double-play bug).
    """
    try:
        with open(LAST_PLAYED_FILE) as f:
            last_played = f.read().strip()
    except FileNotFoundError:
        return
    if not last_played:
        return
    try:
        with open(PLAYLIST_FILE) as f:
            lines = [ln.strip() for ln in f if ln.strip()]
    except FileNotFoundError:
        return
    if last_played not in lines:
        logger.info("restore_queue_position: %r not in queue, skipping", last_played)
        return
    idx = lines.index(last_played)
    if idx == 0:
        return
    write_playlist(lines[idx:])
    logger.info("Restored queue position: dropped %d played tracks, resuming at %r",
                idx, last_played)


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


def _load_user_playlists() -> dict:
    try:
        with open(USER_PLAYLISTS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_user_playlists(data: dict) -> None:
    with open(USER_PLAYLISTS_FILE, "w") as f:
        json.dump(data, f)


def _session_user(request: Request) -> Optional[str]:
    token = request.cookies.get(auth.COOKIE_NAME)
    return auth.verify_token(token, _secret) if token else None


def _load_shared_playlists() -> dict:
    try:
        with open(SHARED_PLAYLISTS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_shared_playlists(data: dict) -> None:
    with open(SHARED_PLAYLISTS_FILE, "w") as f:
        json.dump(data, f)


def _find_shared(name: str, shared: dict) -> Optional[str]:
    """Case-insensitive key lookup into the shared playlists dict."""
    return next((k for k in shared if k.lower() == name.lower()), None)


def _sync_to_plex(name: str, tracks: list[dict]) -> None:
    """Fire-and-forget Plex mirror sync — logs on failure, never raises."""
    try:
        plex.sync_shared_playlist_to_plex(name, tracks)
    except Exception:
        logger.warning("Plex sync failed for shared playlist %r", name)


def _delete_from_plex(name: str) -> None:
    try:
        plex.delete_plex_playlist(name)
    except Exception:
        logger.warning("Plex delete failed for shared playlist %r", name)


async def _ai_tool_handler(name: str, inputs: dict) -> str:
    if name == "request_track":
        query = inputs["query"]
        artist_hint, title_query = _parse_request_query(query)
        track = None
        if artist_hint and title_query:
            results = plex.search_tracks(title_query, artist_filter=artist_hint)
            track = results[0] if results else None
        elif artist_hint:
            track = plex.get_random_track_by_artist(artist_hint)
        else:
            track = plex.get_random_track_by_artist(title_query)
            if not track:
                results = plex.search_tracks(title_query)
                track = results[0] if results else None
        if not track:
            return f"No tracks found for: {query}"
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
        random.shuffle(tracks)
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

    if name == "stop_playback":
        write_playlist([])
        await liquidsoap.skip()
        set_mode("stopped")
        return "Station stopped"

    if name == "start_playback":
        tracks = plex.get_all_tracks()
        random.shuffle(tracks)
        write_playlist(tracks)
        set_mode("random")
        await liquidsoap.reload_playlist()
        await liquidsoap.skip()
        return f"Started: random shuffle ({len(tracks)} tracks)"

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
    "!play":    "!request",
    "!playing": "!np",
}


async def handle_command(sender: str, cmd: str, args: str) -> None:
    cmd = ALIASES.get(cmd, cmd)

    if cmd == "!np":
        if now_playing.title:
            lines = []
            if now_playing.artist:
                lines.append(f"Artist: {now_playing.artist}")
            lines.append(f"Track:  {now_playing.title}")
            if now_playing.album:
                lines.append(f"Album:  {now_playing.album}")
            lines.append(f"Mode:   {current_mode}")
            msg = "\n".join(lines)
        else:
            msg = "Nothing playing yet."
        await bot.send_message(msg)

    elif cmd == "!request":
        if not args:
            await bot.send_message(
                "Usage:\n"
                "  !request <track>            — search by title\n"
                "  !request <artist>           — random track by artist\n"
                "  !request <artist> - <track> — specific artist + title\n"
                "  !request <track> by <artist>"
            )
            return
        artist_hint, title_query = _parse_request_query(args)
        track = None
        if artist_hint and title_query:
            results = plex.search_tracks(title_query, artist_filter=artist_hint)
            track = results[0] if results else None
            if not track:
                await bot.send_message(f"No tracks found: {artist_hint} — {title_query}")
                return
        elif artist_hint:
            track = plex.get_random_track_by_artist(artist_hint)
            if not track:
                await bot.send_message(f"Artist not found: {artist_hint}")
                return
        else:
            # No separator — try artist search first, fall back to track search
            track = plex.get_random_track_by_artist(title_query)
            if not track:
                results = plex.search_tracks(title_query)
                track = results[0] if results else None
            if not track:
                await bot.send_message(f"No tracks found: {title_query}")
                return
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
            plex_pls = plex.list_playlists()
            shared = _load_shared_playlists()
            lines = []
            if plex_pls:
                lines.append("Plex (play only): " + ", ".join(plex_pls))
            if shared:
                lines.append("Shared: " + ", ".join(
                    f"{k} ({len(v.get('tracks', []))})" for k, v in shared.items()
                ))
            await bot.send_message("\n".join(lines) if lines else "No playlists found.")
            return
        # Check shared playlists first, then Plex
        shared = _load_shared_playlists()
        match = _find_shared(args, shared)
        if match:
            tracks = [t["path"] for t in shared[match].get("tracks", [])]
            if not tracks:
                await bot.send_message(f"Shared playlist '{match}' is empty.")
                return
            random.shuffle(tracks)
            write_playlist(tracks)
            set_mode(f"shared:{match}")
            await bot.send_message(f"Switched to shared playlist: {match} ({len(tracks)} tracks)")
            return
        tracks = plex.get_playlist_tracks(args)
        if not tracks:
            plex_pls = plex.list_playlists()
            all_names = list(shared.keys()) + plex_pls
            await bot.send_message(
                f"Playlist '{args}' not found.\nAvailable: " + (", ".join(all_names) or "none")
            )
            return
        random.shuffle(tracks)
        write_playlist(tracks)
        set_mode(f"playlist:{args}")
        await bot.send_message(f"Switched to Plex playlist: {args} ({len(tracks)} tracks)")

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
        n_tracks = len(all_tracks)
        n_artists = len(similar_artists)
        await bot.send_message(
            f"Smart radio: similar to {args} — {n_tracks} tracks from {n_artists} artists"
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

    elif cmd == "!stop":
        write_playlist([])
        await liquidsoap.skip()
        set_mode("stopped")
        await bot.send_message("Station stopped.")

    elif cmd == "!start":
        await bot.send_message("Starting up...")
        tracks = plex.get_all_tracks()
        random.shuffle(tracks)
        write_playlist(tracks)
        set_mode("random")
        await liquidsoap.reload_playlist()
        await liquidsoap.skip()
        await bot.send_message(f"Playing: random shuffle ({len(tracks)} tracks)")

    elif cmd == "!shuffle":
        try:
            with open(PLAYLIST_FILE) as f:
                tracks = [ln.strip() for ln in f if ln.strip()]
        except FileNotFoundError:
            tracks = []
        if not tracks:
            await bot.send_message("Nothing in the queue to shuffle.")
            return
        random.shuffle(tracks)
        write_playlist(tracks)
        await bot.send_message(f"Queue reshuffled ({len(tracks)} tracks)")

    elif cmd == "!random":
        await bot.send_message("Shuffling full library...")
        tracks = plex.get_all_tracks()
        random.shuffle(tracks)
        write_playlist(tracks)
        set_mode("random")
        await bot.send_message(f"Full library shuffle ({len(tracks)} tracks)")

    elif cmd == "!mode":
        await bot.send_message(f"Current mode: {current_mode}")

    elif cmd == "!queue":
        lines: list[str] = []

        pending = await liquidsoap.get_request_queue()
        if pending:
            lines.append("Queued requests:")
            for t in pending:
                lines.append(f"  {t.get('artist', '?')} — {t.get('title', '?')}")

        try:
            with open(PLAYLIST_FILE) as f:
                playlist_lines = [ln.strip() for ln in f if ln.strip()]
            if current_filename and current_filename in playlist_lines:
                idx = playlist_lines.index(current_filename)
                upcoming = playlist_lines[idx + 1:idx + 6]
            else:
                upcoming = playlist_lines[:5]
            if upcoming:
                lines.append("Up next:")
                for path in upcoming:
                    lines.append(f"  {_path_to_label(path)}")
        except FileNotFoundError:
            pass

        await bot.send_message("\n".join(lines) if lines else "Queue is empty.")

    elif cmd == "!playlists":
        plex_pls = plex.list_playlists()
        shared = _load_shared_playlists()
        lines = []
        if plex_pls:
            lines.append("Plex (play only):\n" + "\n".join(f"  {p}" for p in plex_pls))
        if shared:
            lines.append("Shared (anyone can edit):\n" + "\n".join(
                "  {}  ({} tracks, created by {})".format(
                    k,
                    len(v.get('tracks', [])),
                    v.get('created_by', '?').split(':')[0].lstrip('@'),
                )
                for k, v in shared.items()
            ))
        await bot.send_message("\n".join(lines) if lines else "No playlists found.")

    elif cmd == "!createplaylist":
        if not args or " " in args.strip():
            await bot.send_message("Usage: !createplaylist <name>  (no spaces in name)")
            return
        name = args.strip()
        shared = _load_shared_playlists()
        if _find_shared(name, shared):
            await bot.send_message(f"Shared playlist '{name}' already exists.")
            return
        if name.lower() in [p.lower() for p in plex.list_playlists()]:
            await bot.send_message(f"'{name}' conflicts with a Plex playlist name — choose another.")
            return
        shared[name] = {"created_by": sender, "tracks": []}
        _save_shared_playlists(shared)
        await bot.send_message(f"Created shared playlist: {name}")

    elif cmd == "!addto":
        if not args:
            await bot.send_message("Usage: !addto <playlist>  or  !addto <playlist> | <track query>")
            return
        if "|" in args:
            pl_name, _, track_query = args.partition("|")
            pl_name = pl_name.strip()
            track_query = track_query.strip()
        else:
            pl_name = args.strip()
            track_query = ""
        shared = _load_shared_playlists()
        match = _find_shared(pl_name, shared)
        if not match:
            await bot.send_message(
                f"Shared playlist '{pl_name}' not found. Use !createplaylist to make one."
            )
            return
        if track_query:
            artist_hint, title_query = _parse_request_query(track_query)
            if artist_hint and title_query:
                results = plex.search_tracks(title_query, artist_filter=artist_hint)
            else:
                results = plex.search_tracks(track_query, limit=1)
            if not results:
                await bot.send_message(f"No tracks found: {track_query}")
                return
            track = results[0]
        else:
            if not now_playing.title:
                await bot.send_message("Nothing is playing right now.")
                return
            results = plex.search_tracks(
                now_playing.title, artist_filter=now_playing.artist, limit=1)
            if results:
                track = results[0]
            else:
                track = {
                    "title": now_playing.title,
                    "artist": now_playing.artist or "",
                    "album": now_playing.album or "",
                    "path": current_filename,
                    "thumb": now_playing_thumb or "",
                    "key": "",
                }
        tracks = shared[match].setdefault("tracks", [])
        if any(t["path"] == track["path"] for t in tracks):
            await bot.send_message(f"Already in '{match}': {track['artist']} — {track['title']}")
            return
        tracks.append(track)
        _save_shared_playlists(shared)
        _sync_to_plex(match, tracks)
        await bot.send_message(
            "Added to '{}': {} — {} (#{} of {})".format(
                match, track['artist'], track['title'], len(tracks), len(tracks)
            )
        )

    elif cmd == "!removefrom":
        if not args or "|" not in args:
            await bot.send_message("Usage: !removefrom <playlist> | <track number>")
            return
        pl_name, _, num_str = args.partition("|")
        pl_name = pl_name.strip()
        num_str = num_str.strip()
        shared = _load_shared_playlists()
        match = _find_shared(pl_name, shared)
        if not match:
            await bot.send_message(f"Shared playlist '{pl_name}' not found.")
            return
        tracks = shared[match].get("tracks", [])
        try:
            idx = int(num_str) - 1
            if idx < 0 or idx >= len(tracks):
                await bot.send_message(f"Invalid number — '{match}' has {len(tracks)} tracks.")
                return
            removed = tracks.pop(idx)
            _save_shared_playlists(shared)
            _sync_to_plex(match, tracks)
            await bot.send_message(
                f"Removed from '{match}': {removed['artist']} — {removed['title']}"
            )
        except ValueError:
            await bot.send_message("Usage: !removefrom <playlist> | <track number>")

    elif cmd == "!showplaylist":
        if not args:
            await bot.send_message("Usage: !showplaylist <name>")
            return
        shared = _load_shared_playlists()
        match = _find_shared(args, shared)
        if not match:
            await bot.send_message(f"Shared playlist '{args}' not found.")
            return
        tracks = shared[match].get("tracks", [])
        if not tracks:
            await bot.send_message(f"'{match}' is empty.")
            return
        lines = [f"Shared playlist: {match} ({len(tracks)} tracks)"]
        for i, t in enumerate(tracks[:20], 1):
            lines.append(f"  {i}. {t['artist']} — {t['title']}")
        if len(tracks) > 20:
            lines.append(f"  … and {len(tracks) - 20} more")
        await bot.send_message("\n".join(lines))

    elif cmd == "!deleteplaylist":
        if not args:
            await bot.send_message("Usage: !deleteplaylist <name>")
            return
        shared = _load_shared_playlists()
        match = _find_shared(args, shared)
        if not match:
            await bot.send_message(f"Shared playlist '{args}' not found.")
            return
        del shared[match]
        _save_shared_playlists(shared)
        _delete_from_plex(match)
        await bot.send_message(f"Deleted shared playlist: {match}")

    elif cmd == "!save":
        if not args:
            if not now_playing.title:
                await bot.send_message("Nothing is playing right now.")
                return
            results = plex.search_tracks(
                now_playing.title, artist_filter=now_playing.artist, limit=1)
            if results:
                track = results[0]
            else:
                track = {
                    "title": now_playing.title,
                    "artist": now_playing.artist or "",
                    "album": now_playing.album or "",
                    "path": current_filename,
                    "thumb": now_playing_thumb or "",
                    "key": "",
                }
        else:
            artist_hint, title_query = _parse_request_query(args)
            if artist_hint and title_query:
                results = plex.search_tracks(title_query, artist_filter=artist_hint)
            else:
                results = plex.search_tracks(args, limit=1)
            if not results:
                await bot.send_message(f"No tracks found: {args}")
                return
            track = results[0]
        playlists = _load_user_playlists()
        tracks = playlists.setdefault(sender, [])
        if any(t["path"] == track["path"] for t in tracks):
            await bot.send_message(f"Already in your playlist: {track['artist']} — {track['title']}")
            return
        tracks.append(track)
        _save_user_playlists(playlists)
        await bot.send_message(
            "Saved to your playlist: {} — {} (#{} of {})".format(
                track['artist'], track['title'], len(tracks), len(tracks)
            )
        )

    elif cmd == "!mylist":
        playlists = _load_user_playlists()
        tracks = playlists.get(sender, [])
        sub = args.split(None, 1) if args else []
        subcmd = sub[0].lower() if sub else ""
        subargs = sub[1].strip() if len(sub) > 1 else ""

        if not subcmd:
            if not tracks:
                await bot.send_message("Your playlist is empty. Use !save <track> to add tracks.")
                return
            lines = [f"Your playlist ({len(tracks)} tracks):"]
            for i, t in enumerate(tracks[:20], 1):
                lines.append(f"  {i}. {t['artist']} — {t['title']}")
            if len(tracks) > 20:
                lines.append(f"  … and {len(tracks) - 20} more")
            await bot.send_message("\n".join(lines))

        elif subcmd == "play":
            if not tracks:
                await bot.send_message("Your playlist is empty.")
                return
            paths = [t["path"] for t in tracks]
            random.shuffle(paths)
            write_playlist(paths)
            set_mode(f"user:{sender}")
            await bot.send_message(f"Playing your playlist ({len(paths)} tracks)")

        elif subcmd == "clear":
            playlists[sender] = []
            _save_user_playlists(playlists)
            await bot.send_message("Your playlist cleared.")

        elif subcmd == "remove":
            if not subargs:
                await bot.send_message("Usage: !mylist remove <number>")
                return
            try:
                idx = int(subargs) - 1
                if idx < 0 or idx >= len(tracks):
                    await bot.send_message(f"Invalid number — your playlist has {len(tracks)} tracks.")
                    return
                removed = tracks.pop(idx)
                _save_user_playlists(playlists)
                await bot.send_message(f"Removed: {removed['artist']} — {removed['title']}")
            except ValueError:
                await bot.send_message("Usage: !mylist remove <number>")

        else:
            await bot.send_message("Usage: !mylist [play|clear|remove <N>]")

    elif cmd == "!help":
        await bot.send_message(HELP_TEXT)


HELP_TEXT = (
    "Commands:\n"
    "  !np / !playing           — now playing\n"
    "  !request / !play         — queue a track (one-shot)\n"
    "  !skip / !next            — skip current track\n"
    "  !queue                   — show upcoming tracks\n"
    "  !shuffle                  — reshuffle the current playlist\n"
    "  !random                   — switch to full library random mode\n"
    "  !genre <genre>           — play by genre\n"
    "  !similar <artist>        — smart radio similar to artist\n"
    "  !start / !stop           — start or stop playback\n"
    "  !mode                    — show current mode\n"
    "\nPlaylists:\n"
    "  !playlist                — list all playlists\n"
    "  !playlist <name>         — play a playlist (Plex or shared)\n"
    "  !playlists               — detailed list with types\n"
    "\nShared playlists (anyone can edit):\n"
    "  !createplaylist <name>   — create a new shared playlist\n"
    "  !showplaylist <name>     — list tracks in a shared playlist\n"
    "  !addto <name>             — add currently playing track to shared playlist\n"
    "  !addto <name> | <track>  — search and add a track to shared playlist\n"
    "  !removefrom <name> | <N> — remove track N from shared playlist\n"
    "  !deleteplaylist <name>   — delete a shared playlist\n"
    "\nYour personal playlist:\n"
    "  !save                    — save currently playing track to your playlist\n"
    "  !save <track>            — search and save a specific track\n"
    "  !mylist                  — show your playlist\n"
    "  !mylist play             — play your playlist\n"
    "  !mylist clear            — clear your playlist\n"
    "  !mylist remove <N>       — remove track N\n"
    "\n  !help                  — show this message\n"
    + ("  Or just @-mention me to chat!" if ai else "")
)

bot.command_handler = handle_command
bot.ai_handler = handle_ai_message

_welcome_lines = ["Welcome to DrWahl Radio!"]
if settings.stream_url:
    _welcome_lines.append(f"Listen in: {settings.stream_url}")
_welcome_lines.append("\n" + HELP_TEXT)
bot.welcome_message = "\n".join(_welcome_lines)


async def _backfill_now_playing() -> None:
    """Populate now_playing from Liquidsoap telnet if track_changed hasn't fired yet.

    Liquidsoap fires on_metadata almost immediately on startup, often before
    radio-service is ready to accept the POST. This recovers from that race.
    """
    global now_playing, now_playing_thumb, current_track, current_filename
    if now_playing.title:
        return
    try:
        meta = await liquidsoap.get_on_air_metadata()
        title = meta.get("title", "")
        artist = meta.get("artist", "")
        if not title:
            return
        started_at = float(meta.get("on_air_timestamp", 0) or 0) or time.time()
        filename = meta.get("filename", "")
        now_playing.title = title
        now_playing.artist = artist
        now_playing.album = meta.get("album", "")
        now_playing.started_at = started_at
        current_filename = filename
        current_track = {
            "title": title, "artist": artist, "album": meta.get("album", ""),
            "path": filename, "thumb": "", "key": "", "duration": 0,
        }
        # Enrich with Plex data (thumb + duration)
        candidates = plex.search_tracks(title, limit=10)
        if candidates:
            def norm(s): return s.lower().replace(" ", "").replace("-", "")
            an = norm(artist)
            match = next(
                (t for t in candidates if norm(t["artist"]) in an or an in norm(t["artist"])),
                candidates[0],
            )
            current_track = match
            if match.get("thumb"):
                now_playing_thumb = match["thumb"]
                now_playing.has_album_art = True
            if match.get("duration"):
                now_playing.duration = match["duration"]
        logger.info("Backfilled now_playing from Liquidsoap: %s — %s", artist, title)
    except Exception:
        logger.warning("Could not backfill now_playing from Liquidsoap on startup")


async def _on_bot_ready() -> None:
    saved_mode = load_mode()
    try:
        with open(PLAYLIST_FILE) as f:
            queued = [ln.strip() for ln in f if ln.strip()]
    except FileNotFoundError:
        queued = []

    logger.info("_on_bot_ready: saved_mode=%r, queue_len=%d, first=%r",
                saved_mode, len(queued), queued[0] if queued else None)
    if saved_mode and queued:
        set_mode(saved_mode)
        resume_msg = f"Resuming: {saved_mode} ({len(queued)} tracks in queue)"
    else:
        tracks = plex.get_all_tracks()
        if tracks:
            random.shuffle(tracks)
            write_playlist(tracks)
        set_mode("random")
        resume_msg = "Playing: random shuffle ({} tracks)".format(len(tracks) if tracks else 0)

    await _backfill_now_playing()
    listen_line = f"\nListen in: {settings.stream_url}" if settings.stream_url else ""
    ai_line = "\nAI DJ is online — @-mention me to chat." if ai else ""
    await bot.send_message(f"Radio bot online!{listen_line}\n{resume_msg}{ai_line}\n\n{HELP_TEXT}")


bot.on_ready = _on_bot_ready


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _secret
    _secret = auth.load_or_create_secret(settings.session_secret)
    restore_queue_position()
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
    allow_methods=["GET", "POST", "DELETE"],
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
    global now_playing, now_playing_thumb, current_track, current_filename

    logger.info("track_changed: title=%r artist=%r filename=%r", title, artist, filename)
    if title == now_playing.title and artist == now_playing.artist:
        logger.info("track_changed: deduped (already playing)")
        return {"ok": True}

    now_playing = NowPlaying(
        title=title, artist=artist, album=album, mode=current_mode,
        started_at=time.time(),
    )
    now_playing_thumb = ""
    current_track = {
        "title": title, "artist": artist, "album": album,
        "path": filename, "thumb": "", "key": "", "duration": 0,
    }
    current_filename = filename
    try:
        with open(LAST_PLAYED_FILE, "w") as f:
            f.write(filename)
    except Exception:
        logger.warning("Could not write last_played file")

    try:
        candidates = plex.search_tracks(title, limit=10)
        if candidates:
            def norm(s): return s.lower().replace(" ", "").replace("-", "")
            artist_norm = norm(artist)
            match = next(
                (t for t in candidates if norm(t["artist"])
                 in artist_norm or artist_norm in norm(t["artist"])),
                candidates[0],
            )
            current_track = match
            if match.get("thumb"):
                now_playing_thumb = match["thumb"]
                now_playing.has_album_art = True
            if match.get("duration"):
                now_playing.duration = match["duration"]
    except Exception:
        logger.warning("Album art lookup failed for: %s - %s", artist, title)

    try:
        await bot.send_now_playing(title, artist, album)
    except Exception:
        logger.exception("Matrix announcement failed")

    return {"ok": True}


# ── Public API ────────────────────────────────────────────────────────────────

@app.get("/api/search")
async def search_library(q: str = "") -> dict:
    if len(q) < 2:
        return {"tracks": [], "artists": [], "albums": [], "playlists": []}
    tracks = plex.search_tracks(q, limit=10)
    artists_raw = plex.search_artists(q, limit=3)
    artists = [
        {
            "name": a["name"],
            "tracks": plex.search_tracks(a["name"], limit=10, artist_filter=a["name"]),
        }
        for a in artists_raw
    ]
    albums = plex.search_albums(q, limit=3)
    ql = q.lower()
    playlists: list[dict] = [
        {"name": p, "type": "plex"}
        for p in plex.list_playlists() if ql in p.lower()
    ]
    shared = _load_shared_playlists()
    playlists += [
        {"name": k, "type": "shared", "count": len(v.get("tracks", []))}
        for k, v in shared.items() if ql in k.lower()
    ]
    return {"tracks": tracks, "artists": artists, "albums": albums, "playlists": playlists}


@app.get("/api/plex-playlists")
async def get_plex_playlists() -> dict:
    return {"playlists": [{"name": p} for p in plex.list_playlists()]}


@app.get("/api/shared-playlists")
async def get_shared_playlists() -> dict:
    shared = _load_shared_playlists()
    return {
        "playlists": [
            {
                "name": k,
                "created_by": v.get("created_by", ""),
                "tracks": v.get("tracks", []),
                "count": len(v.get("tracks", [])),
            }
            for k, v in shared.items()
        ]
    }


@app.post("/api/shared-playlists")
async def create_shared_playlist(request: Request, name: str = Form(...)) -> dict:
    if not name or " " in name:
        return {"ok": False, "error": "Name cannot be empty or contain spaces"}
    user_id = _session_user(request)
    if not user_id:
        return Response(status_code=401)
    shared = _load_shared_playlists()
    if _find_shared(name, shared):
        return {"ok": False, "error": "Already exists"}
    shared[name] = {"created_by": user_id, "tracks": []}
    _save_shared_playlists(shared)
    return {"ok": True}


@app.post("/api/shared-playlists/{name}/add")
async def add_to_shared_playlist(
    name: str,
    request: Request,
    path: str = Form(...),
    title: str = Form(default=""),
    artist: str = Form(default=""),
    album: str = Form(default=""),
    key: str = Form(default=""),
) -> dict:
    if not _session_user(request):
        return Response(status_code=401)
    shared = _load_shared_playlists()
    match = _find_shared(name, shared)
    if not match:
        return {"ok": False, "error": "Playlist not found"}
    tracks = shared[match].setdefault("tracks", [])
    if any(t["path"] == path for t in tracks):
        return {"ok": True, "already": True, "count": len(tracks)}
    track: dict = {"title": title, "artist": artist, "album": album, "path": path}
    if key:
        track["key"] = key
    tracks.append(track)
    _save_shared_playlists(shared)
    _sync_to_plex(match, tracks)
    return {"ok": True, "count": len(tracks)}


@app.post("/api/shared-playlists/{name}/remove")
async def remove_from_shared_playlist(
    name: str, request: Request, path: str = Form(...)
) -> dict:
    if not _session_user(request):
        return Response(status_code=401)
    shared = _load_shared_playlists()
    match = _find_shared(name, shared)
    if not match:
        return {"ok": False, "error": "Playlist not found"}
    shared[match]["tracks"] = [t for t in shared[match].get("tracks", []) if t["path"] != path]
    _save_shared_playlists(shared)
    _sync_to_plex(match, shared[match]["tracks"])
    return {"ok": True}


@app.delete("/api/shared-playlists/{name}")
async def delete_shared_playlist(name: str, request: Request) -> dict:
    if not _session_user(request):
        return Response(status_code=401)
    shared = _load_shared_playlists()
    match = _find_shared(name, shared)
    if not match:
        return {"ok": False, "error": "Playlist not found"}
    del shared[match]
    _save_shared_playlists(shared)
    _delete_from_plex(match)
    return {"ok": True}


@app.post("/api/queue-track")
async def queue_track(path: str = Form(...)) -> dict:
    ok = await liquidsoap.push_request(path)
    return {"ok": ok}


@app.post("/api/set-mode")
async def set_mode_api(mode_type: str = Form(...), name: str = Form(default="")) -> dict:
    if mode_type == "playlist":
        tracks = plex.get_playlist_tracks(name)
        if not tracks:
            return {"ok": False, "error": "Playlist not found"}
        random.shuffle(tracks)
        write_playlist(tracks)
        set_mode(f"playlist:{name}")
    elif mode_type == "artist":
        tracks = plex.get_tracks_by_artist(name)
        if not tracks:
            return {"ok": False, "error": "No tracks found for artist"}
        random.shuffle(tracks)
        write_playlist(tracks)
        set_mode(f"artist:{name}")
    elif mode_type == "shared":
        shared = _load_shared_playlists()
        match = _find_shared(name, shared)
        if not match:
            return {"ok": False, "error": "Shared playlist not found"}
        tracks = [t["path"] for t in shared[match].get("tracks", [])]
        if not tracks:
            return {"ok": False, "error": "Shared playlist is empty"}
        random.shuffle(tracks)
        write_playlist(tracks)
        set_mode(f"shared:{match}")
    elif mode_type == "random":
        tracks = plex.get_all_tracks()
        random.shuffle(tracks)
        write_playlist(tracks)
        set_mode("random")
    else:
        return {"ok": False, "error": "Unknown mode type"}
    return {"ok": True}


@app.get("/api/my-playlist")
async def get_my_playlist(request: Request) -> dict:
    user_id = _session_user(request)
    if not user_id:
        return Response(status_code=401)
    return {"tracks": _load_user_playlists().get(user_id, [])}


@app.post("/api/my-playlist/add")
async def add_to_my_playlist(
    request: Request,
    path: str = Form(...),
    title: str = Form(default=""),
    artist: str = Form(default=""),
    album: str = Form(default=""),
) -> dict:
    user_id = _session_user(request)
    if not user_id:
        return Response(status_code=401)
    playlists = _load_user_playlists()
    tracks = playlists.setdefault(user_id, [])
    if any(t["path"] == path for t in tracks):
        return {"ok": True, "already": True, "count": len(tracks)}
    tracks.append({"title": title, "artist": artist, "album": album, "path": path})
    _save_user_playlists(playlists)
    return {"ok": True, "count": len(tracks)}


@app.post("/api/my-playlist/remove")
async def remove_from_my_playlist(request: Request, path: str = Form(...)) -> dict:
    user_id = _session_user(request)
    if not user_id:
        return Response(status_code=401)
    playlists = _load_user_playlists()
    if user_id in playlists:
        playlists[user_id] = [t for t in playlists[user_id] if t["path"] != path]
        _save_user_playlists(playlists)
    return {"ok": True}


@app.post("/api/my-playlist/play")
async def play_my_playlist(request: Request) -> dict:
    user_id = _session_user(request)
    if not user_id:
        return Response(status_code=401)
    tracks = _load_user_playlists().get(user_id, [])
    if not tracks:
        return {"ok": False, "error": "Your playlist is empty"}
    paths = [t["path"] for t in tracks]
    random.shuffle(paths)
    write_playlist(paths)
    set_mode(f"user:{user_id}")
    return {"ok": True, "count": len(paths)}


@app.post("/api/my-playlist/clear")
async def clear_my_playlist(request: Request) -> dict:
    user_id = _session_user(request)
    if not user_id:
        return Response(status_code=401)
    playlists = _load_user_playlists()
    playlists[user_id] = []
    _save_user_playlists(playlists)
    return {"ok": True}


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


@app.post("/api/now-playing/save")
async def save_now_playing(request: Request, playlist: str = Form(...)) -> dict:
    """Add the currently playing track to a playlist.

    playlist='mylist' → user's personal playlist
    playlist=<name>   → named shared playlist
    """
    user_id = _session_user(request)
    if not user_id:
        return Response(status_code=401)
    if not current_track.get("path"):
        return {"ok": False, "error": "Nothing playing"}

    track = current_track

    if playlist == "mylist":
        playlists = _load_user_playlists()
        tracks = playlists.setdefault(user_id, [])
        if any(t["path"] == track["path"] for t in tracks):
            return {"ok": True, "already": True}
        tracks.append(track)
        _save_user_playlists(playlists)
        return {"ok": True, "n": len(tracks)}

    shared = _load_shared_playlists()
    match = _find_shared(playlist, shared)
    if not match:
        return {"ok": False, "error": "Playlist not found"}
    tracks = shared[match].setdefault("tracks", [])
    if any(t["path"] == track["path"] for t in tracks):
        return {"ok": True, "already": True}
    tracks.append(track)
    _save_shared_playlists(shared)
    _sync_to_plex(match, tracks)
    return {"ok": True, "n": len(tracks)}
