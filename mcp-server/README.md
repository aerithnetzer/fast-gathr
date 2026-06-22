# fast-gathr MCP server

Hosted MCP (Model Context Protocol) server that exposes the fast-gathr
API as Claude tools.

## Architecture

```
Claude / claude.ai  --HTTPS+SSE-->  mcp.gathrlab.org  --HTTPS-->  api.gathrlab.org  -->  RDS Postgres
                    bearer token forwarded throughout
```

The server is a thin proxy: it never touches the database. Every tool
call forwards the inbound ``Authorization: Bearer …`` header to the
FastAPI service. The FastAPI ``ApiToken`` table is the source of truth
for who can do what.

## Onboarding a user

The user (e.g. the professor) does **not** need to install anything.

1. Mint an API token for them via the API: ``POST /tokens`` as admin,
   name it descriptively (``"claude-professor"``).
2. Send them two strings: the URL ``https://mcp.gathrlab.org`` and the
   ``fgk_…`` token.
3. They open Claude → Settings → Connectors / Custom MCP → paste both.

Revoke at any time via ``DELETE /tokens/{id}``.

## Local development

```bash
export FAST_GATHR_API_BASE=http://localhost:8000
uv run --directory mcp-server python -m fast_gathr_mcp
```

The server listens on ``$PORT`` (default 8000) and exposes:

* ``GET /health`` — ALB health check, no auth.
* ``GET /sse`` and ``POST /messages`` — the MCP SSE transport.
