import logging
from plexapi.server import PlexServer
from plexapi.audio import Track

logger = logging.getLogger(__name__)


class PlexClient:
    def __init__(self, url: str, token: str) -> None:
        self.server = PlexServer(url, token)
        self._music = self.server.library.section("Music")
        # Plex reports the library roots it sees on its own disk.
        # Strip whichever prefix matches to get a relative path, then
        # prepend the container mount point where the same files live.
        self._plex_roots = [loc.rstrip("/") for loc in self._music.locations]
        logger.info("Plex music library roots: %s", self._plex_roots)

    def to_liquidsoap_path(self, plex_path: str) -> str:
        """Translate a Plex server file path to the /music container mount path."""
        for root in self._plex_roots:
            if plex_path.startswith(root):
                rel = plex_path[len(root):].lstrip("/")
                return f"/music/{rel}"
        logger.warning("Path %r doesn't start with any known Plex root %s", plex_path, self._plex_roots)
        return plex_path

    def _track_to_path(self, track: Track) -> str:
        return self.to_liquidsoap_path(track.media[0].parts[0].file)

    def search_tracks(self, query: str, limit: int = 10) -> list[dict]:
        results = self._music.search(query, libtype="track", maxresults=limit)
        return [
            {
                "title": t.title,
                "artist": t.grandparentTitle,
                "album": t.parentTitle,
                "path": self._track_to_path(t),
                # parentThumb is the album art; track.thumb is usually empty
                "thumb": t.parentThumb or t.thumb or "",
            }
            for t in results
        ]

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
        return [pl.title for pl in self.server.playlists() if pl.playlistType == "audio"]

    def get_all_tracks(self) -> list[str]:
        return [self._track_to_path(t) for t in self._music.all(libtype="track")]

    def get_thumbnail_url(self, thumb_path: str) -> str:
        return f"{self.server._baseurl}{thumb_path}?X-Plex-Token={self.server._token}"
