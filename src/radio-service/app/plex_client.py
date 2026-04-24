from __future__ import annotations

import logging
import random
from typing import Optional
from plexapi.server import PlexServer
from plexapi.audio import Track

logger = logging.getLogger(__name__)

MATRIX_PREFIX = "matrix_"


class PlexClient:
    def __init__(self, url: str, token: str) -> None:
        self.server = PlexServer(url, token)
        self._music = self.server.library.section("Music")
        self._plex_roots = [loc.rstrip("/") for loc in self._music.locations]
        logger.info("Plex music library roots: %s", self._plex_roots)

    def to_liquidsoap_path(self, plex_path: str) -> str:
        for root in self._plex_roots:
            if plex_path.startswith(root):
                rel = plex_path[len(root):].lstrip("/")
                return f"/music/{rel}"
        logger.warning("Path %r doesn't start with any known Plex root %s",
                       plex_path, self._plex_roots)
        return plex_path

    def _track_to_path(self, track: Track) -> str:
        return self.to_liquidsoap_path(track.media[0].parts[0].file)

    def _track_to_dict(self, t: Track) -> dict:
        return {
            "title": t.title,
            "artist": t.grandparentTitle,
            "album": t.parentTitle,
            "path": self._track_to_path(t),
            "thumb": t.parentThumb or t.thumb or "",
            "key": t.key,
            "duration": int((t.duration or 0) / 1000),  # ms → seconds
        }

    def search_tracks(self, query: str, limit: int = 10, artist_filter: str = "") -> list[dict]:
        fetch = limit if not artist_filter else limit * 4
        results = self._music.search(query, libtype="track", maxresults=fetch)
        tracks = [self._track_to_dict(t) for t in results]
        if artist_filter:
            def norm(s): return s.lower().replace(" ", "")
            af = norm(artist_filter)
            tracks = [t for t in tracks if af in norm(t["artist"]) or norm(t["artist"]) in af]
        return tracks[:limit]

    def get_random_track_by_artist(self, artist_name: str) -> Optional[dict]:
        results = self._music.search(artist_name, libtype="artist", maxresults=5)
        if not results:
            return None

        def norm(s): return s.lower().strip()
        an = norm(artist_name)
        artist = next((a for a in results if norm(a.title) == an), results[0])
        tracks = artist.tracks()
        if not tracks:
            return None
        return self._track_to_dict(random.choice(tracks))

    def search_artists(self, query: str, limit: int = 5) -> list[dict]:
        results = self._music.search(query, libtype="artist", maxresults=limit)
        return [{"name": a.title} for a in results]

    def search_albums(self, query: str, limit: int = 3) -> list[dict]:
        results = self._music.search(query, libtype="album", maxresults=limit)
        albums = []
        for album in results:
            try:
                tracks = [self._track_to_dict(t) for t in album.tracks()]
            except Exception:
                tracks = []
            albums.append({
                "title": album.title,
                "artist": album.parentTitle,
                "tracks": tracks,
            })
        return albums

    def get_tracks_by_artist(self, artist_name: str) -> list[str]:
        try:
            results = self._music.search(artist_name, libtype="track")
            return [self._track_to_path(t) for t in results]
        except Exception:
            logger.exception("Plex artist search failed for: %s", artist_name)
            return []

    def get_tracks_by_genre(self, genre: str) -> list[str]:
        try:
            results = self._music.search(libtype="track", filters={"genre": genre})
            return [self._track_to_path(t) for t in results]
        except Exception:
            logger.exception("Plex genre search failed for: %s", genre)
            return []

    def get_playlist_tracks(self, playlist_name: str) -> list[str]:
        for pl in self.server.playlists():
            if pl.title.lower() == playlist_name.lower() and pl.playlistType == "audio":
                return [self._track_to_path(t) for t in pl.items()]
        return []

    def list_playlists(self) -> list[str]:
        """Return native Plex playlist names, excluding bot-managed matrix_ ones."""
        return [
            pl.title for pl in self.server.playlists()
            if pl.playlistType == "audio" and not pl.title.startswith(MATRIX_PREFIX)
        ]

    def get_all_tracks(self) -> list[str]:
        return [self._track_to_path(t) for t in self._music.all(libtype="track")]

    def get_thumbnail_url(self, thumb_path: str) -> str:
        return f"{self.server._baseurl}{thumb_path}?X-Plex-Token={self.server._token}"

    # ── Shared-playlist Plex sync ─────────────────────────────────────────────

    def sync_shared_playlist_to_plex(self, name: str, tracks: list[dict]) -> bool:
        """Upsert matrix_<name> in Plex to mirror the shared playlist.

        Full delete-and-recreate so Plex is always consistent with local state.
        Returns True on success (or no-op), False if write permission is missing.
        """
        full_name = MATRIX_PREFIX + name

        # Delete existing copy if present
        try:
            self.server.playlist(full_name).delete()
        except Exception:
            pass  # Didn't exist yet — that's fine

        items = []
        for t in tracks:
            key = t.get("key")
            if not key:
                continue
            try:
                items.append(self.server.fetchItem(key))
            except Exception:
                logger.debug("Could not fetch Plex item %r for playlist sync", key)

        if not items:
            # Nothing to create; Plex can't store empty playlists
            return True

        try:
            self.server.createPlaylist(full_name, items=items)
            logger.info("Synced shared playlist %r to Plex (%d tracks)", name, len(items))
            return True
        except Exception:
            logger.warning(
                "Failed to create Plex playlist %r — token may lack write permission", full_name
            )
            return False

    def delete_plex_playlist(self, name: str) -> bool:
        """Delete matrix_<name> from Plex. Returns True if deleted or already absent."""
        full_name = MATRIX_PREFIX + name
        try:
            self.server.playlist(full_name).delete()
            logger.info("Deleted Plex playlist %r", full_name)
            return True
        except Exception:
            logger.debug("Plex playlist %r not found or delete failed (ignored)", full_name)
            return False
