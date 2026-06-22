"""Streamable-HTTP entry point for the fast-gathr MCP server.

The MCP transport spec evolved from the legacy ``/sse`` + ``/messages`` split
to a single **Streamable HTTP** endpoint (POST + SSE on the same path). All
modern clients — Claude Desktop, Claude.ai connectors, and opencode — speak
Streamable HTTP. We expose that at ``/mcp`` and keep a separate ``/health``
route for ALB target-group checks.

A Starlette middleware extracts the inbound ``Authorization: Bearer …``
header (set by the client based on its connector configuration) and stashes
the token on a context variable that the tool implementations read.
"""

from __future__ import annotations

import logging
import os

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .tools import current_bearer_token, register_all_tools


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("fast_gathr_mcp")


# ── MCP server with all tools registered ────────────────────────────────────

# Public hostname this server is reached at. Configurable so non-prod
# deployments (e.g. ``localhost``) work without editing this file.
PUBLIC_HOST = os.environ.get("MCP_PUBLIC_HOST", "mcp.gathrlab.org")

# DNS-rebinding protection in the MCP SDK rejects any Host header that
# isn't on this list. Behind an ALB the inbound Host is the public DNS
# name, so add it explicitly. We also keep localhost for in-container
# health checks and local dev.
_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[
        PUBLIC_HOST,
        "localhost",
        "localhost:8000",
        "127.0.0.1",
        "127.0.0.1:8000",
    ],
    allowed_origins=[
        f"https://{PUBLIC_HOST}",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ],
)

mcp = FastMCP("fast-gathr", transport_security=_security)
register_all_tools(mcp)


# ── Bearer-token middleware ─────────────────────────────────────────────────

class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Extract the inbound ``Authorization: Bearer …`` header and bind it to
    a context variable for the duration of the request. Tool implementations
    in :mod:`.tools` read this variable when constructing the API client."""

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


# ── /health endpoint ────────────────────────────────────────────────────────

async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


# ── Build the Starlette app ─────────────────────────────────────────────────

def build_app():
    """Return the Starlette app, with FastMCP's own routes/lifespan plus a
    ``/health`` route and the bearer-token middleware tacked on.

    We use ``streamable_http_app()`` (the modern transport) rather than
    ``sse_app()`` (the legacy one); modern clients speak Streamable HTTP
    on a single endpoint at ``/mcp``.
    """
    app = mcp.streamable_http_app()
    # Insert /health ahead of the existing /mcp route so the route table is
    # ordered the way an ALB / casual curl expects.
    app.router.routes.insert(0, Route("/health", endpoint=health, methods=["GET"]))
    app.add_middleware(BearerTokenMiddleware)
    return app


app = build_app()


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    log.info("starting fast-gathr MCP server on port %d", port)
    uvicorn.run(
        "fast_gathr_mcp.server:app",
        host="0.0.0.0",
        port=port,
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        # The MCP server runs behind an AWS ALB and Cloudflare. Trust
        # X-Forwarded-* headers from any upstream IP so requests don't get
        # rejected with "Invalid Host header".
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
