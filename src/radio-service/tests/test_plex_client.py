from __future__ import annotations

import sys
from unittest.mock import MagicMock

from app.plex_client import PlexClient


def _make_client(roots: list[str]) -> PlexClient:
    """Construct a PlexClient backed by a mock PlexServer with the given roots."""
    mock_section = MagicMock()
    mock_section.locations = roots
    sys.modules["plexapi.server"].PlexServer.return_value.library.section.return_value = mock_section
    return PlexClient("http://plex.test:32400", "token")


# ── to_liquidsoap_path ────────────────────────────────────────────────────────

def test_strips_single_root():
    c = _make_client(["/mnt/nas/music"])
    assert c.to_liquidsoap_path("/mnt/nas/music/Artist/track.flac") == "/music/Artist/track.flac"


def test_strips_root_with_trailing_slash():
    c = _make_client(["/mnt/nas/music/"])
    assert c.to_liquidsoap_path("/mnt/nas/music/Artist/track.flac") == "/music/Artist/track.flac"


def test_passthrough_on_no_root_match():
    c = _make_client(["/mnt/nas/music"])
    assert c.to_liquidsoap_path("/other/path/track.flac") == "/other/path/track.flac"


def test_uses_first_matching_root():
    c = _make_client(["/mnt/nas/music", "/mnt/local/music"])
    assert c.to_liquidsoap_path("/mnt/local/music/track.flac") == "/music/track.flac"


def test_no_double_slash_in_result():
    c = _make_client(["/mnt/music"])
    result = c.to_liquidsoap_path("/mnt/music/track.flac")
    assert "//" not in result
    assert result == "/music/track.flac"


def test_nested_path_preserved():
    c = _make_client(["/mnt/music"])
    assert c.to_liquidsoap_path("/mnt/music/A/B/C/track.flac") == "/music/A/B/C/track.flac"
