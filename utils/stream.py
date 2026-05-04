"""SSE response helpers for GenOS code serving."""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from fastapi.responses import StreamingResponse


async def create_sse_response(
    generator: AsyncIterator[dict],
    heartbeat_interval: int = 10,
) -> StreamingResponse:
    queue: asyncio.Queue[str] = asyncio.Queue()
    sentinel = "__STREAM_DONE__"
    client_disconnected = asyncio.Event()

    async def emit(event: str, data: Any) -> None:
        payload = {"event": event, "data": data}
        await queue.put(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n")

    async def heartbeat() -> None:
        while not client_disconnected.is_set():
            await asyncio.sleep(heartbeat_interval)
            await queue.put(": keep-alive\n\n")

    async def runner() -> None:
        try:
            async for ev in generator:
                await emit(ev.get("event"), ev.get("data"))
        except Exception as exc:
            await emit("error", str(exc))
        finally:
            await queue.put(sentinel)

    async def sse_stream() -> AsyncIterator[str]:
        producer = asyncio.create_task(runner())
        pinger = asyncio.create_task(heartbeat())
        try:
            while True:
                chunk = await queue.get()
                if chunk == sentinel:
                    break
                yield chunk
        finally:
            client_disconnected.set()
            producer.cancel()
            pinger.cancel()

    return StreamingResponse(
        sse_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
