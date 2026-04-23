import asyncio
import logging
from collections.abc import Awaitable, Callable

from nio import AsyncClient, MatrixRoom, RoomMessageText

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

    async def send_message(self, body: str) -> None:
        await self.client.room_send(
            self.room_id,
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": body},
        )

    async def send_now_playing(self, title: str, artist: str, album: str) -> None:
        lines = [f"Now Playing: {artist} — {title}"]
        if album:
            lines.append(f"  {album}")
        await self.send_message("\n".join(lines))

    async def run(self) -> None:
        logger.info("Matrix bot syncing...")
        # Initial sync first — advances the next_batch token past historical messages
        # so sync_forever only delivers events that arrive after bot startup.
        await self.client.sync(timeout=30000, full_state=True)
        if self.room_id in self.client.invited_rooms:
            logger.info("Accepting room invite for %s", self.room_id)
            await self.client.join(self.room_id)
        # Register callback only after the initial sync to avoid replaying old commands.
        self.client.add_event_callback(self._on_message, RoomMessageText)
        if self.on_ready:
            await self.on_ready()
        await self.client.sync_forever(timeout=30000)

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

        if body.startswith("!") and self.command_handler:
            parts = body.split(None, 1)
            cmd = parts[0].lower()
            args = parts[1].strip() if len(parts) > 1 else ""
            await self.command_handler(event.sender, cmd, args)
        elif self.ai_handler and self._mentions_bot(body):
            await self.ai_handler(event.sender, body)

    def _mentions_bot(self, body: str) -> bool:
        body_lower = body.lower()
        # Match full user ID (@bot.drwahl.radio:drwahl.me) or just the localpart
        localpart = self.client.user_id.split(":")[0].lstrip("@").lower()
        return self.client.user_id.lower() in body_lower or localpart in body_lower
