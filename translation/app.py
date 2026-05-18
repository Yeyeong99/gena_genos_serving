import json
import os
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import service as service_module


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app: FastAPI = FastAPI(lifespan=lifespan, title="GenA Agent (genos)")

# 정적 서빙 (`/preview-files`) 은 제거됐다. 모든 preview/다운로드는 Azure Blob 의
# SAS URL 로만 노출된다 — 로컬·운영 동일.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)


LOG_BODY_LIMIT = int(os.environ.get("LOG_BODY_LIMIT", 4096))


class LoggingMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        req_chunks = bytearray()

        async def recv_wrapper() -> Message:
            msg = await receive()
            if msg["type"] == "http.request":
                remaining = LOG_BODY_LIMIT - len(req_chunks)
                if remaining > 0:
                    req_chunks.extend(msg.get("body", b"")[:remaining])
            return msg

        resp_chunks = bytearray()

        async def send_wrapper(msg: Message) -> None:
            await send(msg)  # 즉시 흘려보냄 — 스트리밍 보존
            if msg["type"] == "http.response.body" and len(resp_chunks) < LOG_BODY_LIMIT:
                remaining = LOG_BODY_LIMIT - len(resp_chunks)
                resp_chunks.extend(msg.get("body", b"")[:remaining])

        path = scope.get("path", "")
        client = scope.get("client") or ("?", 0)
        method = scope.get("method", "?")
        query = scope.get("query_string", b"").decode("latin1")

        try:
            await self.app(scope, recv_wrapper, send_wrapper)
        except Exception:
            req_preview = "[FILE UPLOAD]" if "upload" in path else bytes(req_chunks).decode("utf-8", errors="replace")
            print(
                f"[LoggingMiddleware] unhandled exception: ip={client[0]} method={method} "
                f"path={path} params={query} req_body={req_preview}\n{traceback.format_exc()}"
            )
            raise

        req_preview = "[FILE UPLOAD]" if "upload" in path else bytes(req_chunks).decode("utf-8", errors="replace")
        resp_preview = bytes(resp_chunks).decode("utf-8", errors="replace")
        print(
            f"req: ip={client[0]} method={method} path={path} params={query} req_body={req_preview}"
        )
        print(f"resp: {resp_preview}")


app.add_middleware(LoggingMiddleware)


@app.get("")
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/json")
async def chat_json(request: Request):
    """GenOS code_serving 호환 — body 의 `mode` 로 라우팅."""
    try:
        data = await request.json()
    except json.decoder.JSONDecodeError:
        data = {}
    return await service_module.service(config={}, data=data)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=5899, reload=True)
