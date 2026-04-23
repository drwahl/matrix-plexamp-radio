import asyncio
import logging

logger = logging.getLogger(__name__)


class LiquidsoapClient:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port

    async def _command(self, cmd: str) -> str:
        try:
            reader, writer = await asyncio.open_connection(self.host, self.port)
            writer.write(f"{cmd}\n".encode())
            await writer.drain()
            lines: list[str] = []
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                text = line.decode().strip()
                if text == "END":
                    break
                if text:
                    lines.append(text)
            writer.close()
            await writer.wait_closed()
            return "\n".join(lines)
        except asyncio.TimeoutError:
            logger.warning("Liquidsoap telnet timeout for: %s", cmd)
            return ""
        except Exception:
            logger.exception("Liquidsoap command failed: %s", cmd)
            return ""

    async def skip(self) -> None:
        await self._command("out.skip")

    async def push_request(self, filepath: str) -> bool:
        result = await self._command(f"requests.push {filepath}")
        return result.strip().isdigit()

    async def now_on_air(self) -> str:
        return await self._command("request.on_air")
