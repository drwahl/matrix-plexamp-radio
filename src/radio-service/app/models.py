from __future__ import annotations

from pydantic import BaseModel


class TrackInfo(BaseModel):
    title: str = ""
    artist: str = ""
    album: str = ""
    filename: str = ""


class NowPlaying(BaseModel):
    title: str = ""
    artist: str = ""
    album: str = ""
    has_album_art: bool = False
    mode: str = "random"
    started_at: float = 0.0   # unix timestamp when the track started on the server
    duration: int = 0          # track duration in seconds (0 = unknown)

    # Internal — not serialized to API responses
    _thumb_path: str = ""
