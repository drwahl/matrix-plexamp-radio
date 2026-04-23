from __future__ import annotations

import logging
import pylast

logger = logging.getLogger(__name__)


class LastFMClient:
    def __init__(self, api_key: str) -> None:
        self._network = pylast.LastFMNetwork(api_key=api_key)

    def get_similar_artists(self, artist_name: str, limit: int = 15) -> list[str]:
        try:
            artist = self._network.get_artist(artist_name)
            similar = artist.get_similar(limit=limit)
            return [s.item.name for s in similar]
        except Exception:
            logger.exception("Last.fm similar-artists lookup failed for: %s", artist_name)
            return []
