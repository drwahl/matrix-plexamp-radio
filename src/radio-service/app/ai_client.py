from __future__ import annotations

import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

import litellm

logger = logging.getLogger(__name__)

# Suppress litellm's verbose startup noise
litellm.suppress_debug_info = True

ToolHandler = Callable[[str, dict], Awaitable[str]]

SYSTEM_PROMPT = """\
You are DJ Wahl, the AI host of DrWahl's personal internet radio station. \
You're a music enthusiast — knowledgeable, opinionated, and a little irreverent. \
You can talk about artists, albums, genres, and music history, or just vibe.

You have tools to control the radio. Use them naturally when someone asks: \
"play some jazz" → genre_radio, "queue some Radiohead" → request_track, \
"skip this" → skip_track, "what's playing?" → you already know from context.

IMPORTANT — tool distinction:
- request_track queues ONE track to play next, then the station automatically \
returns to whatever background mode was already running. It does NOT change the \
background mode. Use it when someone wants a specific song without disrupting \
the current playlist or mode.
- play_playlist, similar_artist_radio, genre_radio, and random_shuffle REPLACE \
the current background mode entirely. Only use these when someone explicitly \
wants to change what the station is playing long-term.
- Never call a mode-changing tool at the same time as request_track. Pick one.

Keep responses short — a sentence or two. This is a chat room, not a podcast. \
When you change something on the radio, confirm it briefly and conversationally.\
"""

# OpenAI-format tool definitions (litellm normalises these for all providers)
RADIO_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "request_track",
            "description": "Search for a track and queue it to play next as a one-shot request. The station automatically returns to the current background mode after the track finishes. Does NOT change the background playlist or mode.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Track title, artist name, or 'Artist - Title' to disambiguate. Use artist name alone to pick a random track by that artist."}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skip_track",
            "description": "Skip the current track.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "play_playlist",
            "description": "Switch to a named Plex playlist.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Playlist name"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "similar_artist_radio",
            "description": "Build a queue of tracks similar to a given artist (requires Last.fm).",
            "parameters": {
                "type": "object",
                "properties": {"artist": {"type": "string"}},
                "required": ["artist"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "genre_radio",
            "description": "Play music from a specific genre.",
            "parameters": {
                "type": "object",
                "properties": {"genre": {"type": "string"}},
                "required": ["genre"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "random_shuffle",
            "description": "Shuffle and play the full music library.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_playback",
            "description": "Stop the station. Clears the playlist and silences the stream.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_playback",
            "description": "Start the station with a random shuffle of the full library.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_playlists",
            "description": "Get the list of available Plex playlists.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


class AIClient:
    def __init__(self, model: str, api_key: str = "", base_url: str = "") -> None:
        self._model = model
        self._extra: dict[str, Any] = {}
        if api_key:
            self._extra["api_key"] = api_key
        if base_url:
            self._extra["api_base"] = base_url
        # Shared room history — bounded to last 20 turns (10 exchanges)
        self._history: deque[dict[str, Any]] = deque(maxlen=20)

    async def chat(
        self,
        user_message: str,
        now_playing_context: str,
        tool_handler: ToolHandler,
    ) -> str:
        system = SYSTEM_PROMPT
        if now_playing_context:
            system += f"\n\nCurrently on air: {now_playing_context}"

        self._history.append({"role": "user", "content": user_message})
        messages = [{"role": "system", "content": system}] + list(self._history)

        try:
            response = await litellm.acompletion(
                model=self._model,
                messages=messages,
                tools=RADIO_TOOLS,
                max_tokens=512,
                timeout=30,
                num_retries=0,
                **self._extra,
            )

            # Agentic tool-use loop
            while response.choices[0].finish_reason == "tool_calls":
                assistant_msg = response.choices[0].message
                messages.append(assistant_msg)

                tool_results: list[dict[str, Any]] = []
                for tc in assistant_msg.tool_calls or []:
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        args = {}
                    logger.info("AI invoking %s(%s)", tc.function.name, args)
                    result = await tool_handler(tc.function.name, args)
                    tool_results.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result,
                        }
                    )

                messages.extend(tool_results)
                response = await litellm.acompletion(
                    model=self._model,
                    messages=messages,
                    tools=RADIO_TOOLS,
                    max_tokens=512,
                    timeout=30,
                    num_retries=0,
                    **self._extra,
                )

            text = response.choices[0].message.content or ""
            self._history.append({"role": "assistant", "content": text})
            return text

        except Exception:
            logger.exception("AI chat failed (model=%s)", self._model)
            return ""
