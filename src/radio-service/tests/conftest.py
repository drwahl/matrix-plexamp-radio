from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

# ── Environment ───────────────────────────────────────────────────────────────
# Satisfy pydantic-settings required fields before any app module is imported.
os.environ.setdefault("PLEX_URL", "http://plex.test:32400")
os.environ.setdefault("PLEX_TOKEN", "test-token")
os.environ.setdefault("MATRIX_HOMESERVER", "http://matrix.test")
os.environ.setdefault("MATRIX_TOKEN", "mat-token")
os.environ.setdefault("MATRIX_USER_ID", "@bot:test")
os.environ.setdefault("MATRIX_ROOM_ID", "!room:test")

# ── Stub external libraries ───────────────────────────────────────────────────
# These modules make network connections in their constructors or at import
# time.  Stub them out before any app module is collected so that the import
# tests work without a real Plex server, Matrix homeserver, or LLM backend.
for _mod in ("plexapi", "plexapi.server", "plexapi.audio", "nio", "litellm", "pylast"):
    sys.modules.setdefault(_mod, MagicMock())

# Make PlexClient.__init__ succeed: _music.locations must be a real list.
_plex_instance = sys.modules["plexapi.server"].PlexServer.return_value
_plex_instance.library.section.return_value.locations = ["/mnt/music"]
_plex_instance.playlists.return_value = []
