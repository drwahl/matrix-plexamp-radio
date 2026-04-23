from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from nio import AsyncClient, MatrixRoom, RoomMemberEvent, RoomMessageText

logger = logging.getLogger(__name__)

CommandHandler = Callable[[str, str, str], Awaitable[None]]
ReadyHandler = Callable[[], Awaitable[None]]
AIHandler = Callable[[str, str], Awaitable[None]]


class MatrixBot:
    def __init__(
        self,
        homeserver: str,
        token: str,
        user_id: str,
        room_id: str,
        allowed_users: list[str],
    ) -> None:
        self.client = AsyncClient(homeserver, user_id)
        self.client.access_token = token
        self.room_id = room_id
        self.allowed_users: set[str] = set(allowed_users)
        self.command_handler: CommandHandler | None = None
        self.ai_handler: AIHandler | None = None
        self.on_ready: ReadyHandler | None = None
        self.welcome_message: str = ""

    async def send_message(self, body: str) -> None:
        await self.client.room_send(
            self.room_id,
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": body},
        )

    async def send_now_playing(self, title: str, artist: str, album: str) -> None:
        lines = []
        if artist:
            lines.append(f"Artist: {artist}")
        if title:
            lines.append(f"Track: {title}")
        if album:
            lines.append(f"Album: {album}")
        await self.send_message("\n".join(lines))

    async def run(self) -> None:
        logger.info("Matrix bot syncing...")
        # Initial sync first — advances the next_batch token past historical messages
        # so sync_forever only delivers events that arrive after bot startup.
        await self.client.sync(timeout=5000, full_state=True)
        if self.room_id in self.client.invited_rooms:
            logger.info("Accepting room invite for %s", self.room_id)
            await self.client.join(self.room_id)
        # Register callback only after the initial sync to avoid replaying old commands.
        self.client.add_event_callback(self._on_message, RoomMessageText)
        self.client.add_event_callback(self._on_member_event, RoomMemberEvent)
        if self.on_ready:
            await self.on_ready()
        await self.client.sync_forever(timeout=5000)

    async def _on_message(self, room: MatrixRoom, event: RoomMessageText) -> None:
        if room.room_id != self.room_id:
            return
        if event.sender == self.client.user_id:
            return
        if event.sender not in self.allowed_users:
            return
        body = event.body.strip()
        if not body:
            return

        await self.client.room_read_markers(self.room_id, event.event_id, event.event_id)

        if body.startswith("!") and self.command_handler:
            parts = body.split(None, 1)
            cmd = parts[0].lower()
            args = parts[1].strip() if len(parts) > 1 else ""
            await self.command_handler(event.sender, cmd, args)
        elif self.ai_handler and self._mentions_bot(body):
            # Run off the sync loop so a slow/hanging AI call never blocks commands
            asyncio.create_task(self._run_ai(event.sender, body))

    async def _run_ai(self, sender: str, body: str) -> None:
        try:
            await self.ai_handler(sender, body)
        except Exception:
            logger.exception("AI handler raised an unhandled exception")

    async def _on_member_event(self, room: MatrixRoom, event: RoomMemberEvent) -> None:
        if room.room_id != self.room_id:
            return
        if event.membership != "join" or event.prev_membership == "join":
            return
        if event.sender == self.client.user_id:
            return
        if self.welcome_message:
            await self._send_dm(event.sender, self.welcome_message)

    async def _send_dm(self, user_id: str, message: str) -> None:
        resp = await self.client.room_create(is_direct=True, invite=[user_id])
        if hasattr(resp, "room_id"):
            await self.client.room_send(
                resp.room_id,
                message_type="m.room.message",
                content={"msgtype": "m.text", "body": message},
            )
        else:
            logger.warning("Failed to create DM room for %s: %s", user_id, resp)

    def _mentions_bot(self, body: str) -> bool:
        body_lower = body.lower()
        # Match full user ID (@bot.drwahl.radio:drwahl.me) or just the localpart
        localpart = self.client.user_id.split(":")[0].lstrip("@").lower()
        return self.client.user_id.lower() in body_lower or localpart in body_lower
