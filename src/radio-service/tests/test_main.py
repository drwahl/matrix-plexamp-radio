from __future__ import annotations

import pytest

from app.main import _parse_request_query, _path_to_label, _find_shared


# ── _parse_request_query ──────────────────────────────────────────────────────

@pytest.mark.parametrize("query,expected", [
    ("Artist - Title",              ("Artist", "Title")),
    ("  Artist  -  Title  ",        ("Artist", "Title")),
    ("Title by Artist",             ("Artist", "Title")),
    ("Just a title",                ("",       "Just a title")),
    ("Multi Word - Multi Word",     ("Multi Word", "Multi Word")),
    ("Song by The Band",            ("The Band", "Song")),
    # "by" at the start of the query should NOT trigger the split
    ("by the sea",                  ("",       "by the sea")),
])
def test_parse_request_query(query, expected):
    assert _parse_request_query(query) == expected


# ── _path_to_label ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path,expected", [
    ("/music/Artist/Album/Artist - Title.flac", "Artist — Title"),
    ("/music/Artist/Album/Title.mp3",           "Title"),
    ("/music/Artist/Album/No Extension",        "No Extension"),
    ("plain.mp3",                               "plain"),
])
def test_path_to_label(path, expected):
    assert _path_to_label(path) == expected


# ── _find_shared ──────────────────────────────────────────────────────────────

def test_find_shared_exact():
    assert _find_shared("Foo", {"Foo": {}}) == "Foo"


def test_find_shared_case_insensitive():
    d = {"Foo": {}, "Bar": {}}
    assert _find_shared("foo", d) == "Foo"
    assert _find_shared("FOO", d) == "Foo"
    assert _find_shared("BAR", d) == "Bar"


def test_find_shared_missing():
    assert _find_shared("missing", {"Foo": {}}) is None


def test_find_shared_empty_dict():
    assert _find_shared("foo", {}) is None
