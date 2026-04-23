"""
Import tests — the simplest possible check that every module loads cleanly
under the container's Python version.  These are the tests that would have
caught the `str | None` annotation error (TypeError at import time on 3.9).
"""
from __future__ import annotations


def test_import_auth():
    import app.auth  # noqa: F401


def test_import_config():
    import app.config  # noqa: F401


def test_import_models():
    import app.models  # noqa: F401


def test_import_liquidsoap_client():
    import app.liquidsoap_client  # noqa: F401


def test_import_matrix_bot():
    import app.matrix_bot  # noqa: F401


def test_import_ai_client():
    import app.ai_client  # noqa: F401


def test_import_plex_client():
    import app.plex_client  # noqa: F401


def test_import_lastfm_client():
    import app.lastfm_client  # noqa: F401


def test_import_main():
    # This is the heaviest import — exercises all module-level service
    # initialisation and is the exact test that catches annotation errors.
    import app.main  # noqa: F401
