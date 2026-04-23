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

    # Internal — not serialized to API responses
    _thumb_path: str = ""
