"""fast-gathr MCP server.

Exposes the fast-gathr API as a remote MCP server speaking HTTP+SSE.
Claude (or any MCP-aware client) connects to ``mcp.gathrlab.org``,
authenticates with a fast-gathr API token, and gets a uniform CRUD tool
surface across every entity table.

The server is deliberately a thin proxy — it never touches the database.
Every tool call forwards the inbound bearer token to the FastAPI service
on ``$FAST_GATHR_API_BASE``.
"""
