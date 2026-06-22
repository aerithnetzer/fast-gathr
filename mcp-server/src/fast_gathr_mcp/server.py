"""HTTP+SSE entry point for the fast-gathr MCP server.

Wraps the MCP SSE transport in a Starlette app so we can:

* Add a ``GET /health`` route for the ALB target group health check.
* Extract the bearer token from the inbound ``Authorization`` header
  (set by Claude based on the user's connector configuration) and stash
  it on a context variable that tool implementations read.
"""

from __future__ import annotations

import logging
import os

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from .tools import current_bearer_token, register_all_tools


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("fast_gathr_mcp")


# ── MCP server with all tools registered ────────────────────────────────────

mcp = FastMCP("fast-gathr")
register_all_tools(mcp)


# ── Bearer-token middleware ─────────────────────────────────────────────────

class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Extract the inbound ``Authorization: Bearer …`` header and bind it
    to a context variable for the duration of the request."""

    async def dispatch(self, request: Request, call_next):
        token: str | None = None
        auth = request.headers.get("authorization") or request.headers.get(
            "Authorization"
        )
        if auth and auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()

        token_reset = current_bearer_token.set(token)
        try:
            return await call_next(request)
        finally:
            current_bearer_token.reset(token_reset)


# ── Starlette app: /health + the MCP SSE transport ──────────────────────────

async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def build_app() -> Starlette:
    sse_app = mcp.sse_app()
    return Starlette(
        debug=False,
        routes=[
            Route("/health", endpoint=health, methods=["GET"]),
            Mount("/", app=sse_app),
        ],
        middleware=[Middleware(BearerTokenMiddleware)],
    )


app = build_app()


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    log.info("starting fast-gathr MCP server on port %d", port)
    uvicorn.run(
        "fast_gathr_mcp.server:app",
        host="0.0.0.0",
        port=port,
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
