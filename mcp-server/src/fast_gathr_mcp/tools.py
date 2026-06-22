"""MCP tool registry for fast-gathr.

Each entity table from the API gets four tools:
``list_<entity>``, ``get_<entity>``, ``create_<entity>``, and
``delete_<entity>``. Plus two utility tools (``health_check`` and
``whoami``).

Every tool requires an inbound bearer token (the user's fast-gathr API
token); the MCP transport extracts it from the request and stores it in
a context variable that the tool implementations read.
"""

from __future__ import annotations

import contextvars
from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import ApiError, GathrClient


# ── Per-request bearer token plumbing ──────────────────────────────────────

# The MCP transport sets this for the duration of a request so tool
# implementations can construct a properly-authenticated client without
# having to thread the token through every call.
current_bearer_token: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_bearer_token", default=None
)


def _client() -> GathrClient:
    token = current_bearer_token.get()
    if not token:
        raise ApiError(
            401,
            "No fast-gathr API token in this MCP request. Configure the "
            "Authorization header in your Claude connector settings.",
        )
    return GathrClient(token)


# ── Entity definitions ──────────────────────────────────────────────────────

# (tool_singular, tool_plural, api_prefix, description)
ENTITIES: list[tuple[str, str, str, str]] = [
    ("vocabulary_entry", "vocabulary_entries", "vocabulary",
     "Master vocabulary list entries (headwords, triggers, classifications)."),
    ("mention", "mentions", "mentions",
     "Mentions linking a clause to a master vocabulary item."),
    ("location", "locations", "locations",
     "Geographic locations (places, with optional 'located in' parent)."),
    ("person", "persons", "persons",
     "Master person records (name, alias, social identity, residence)."),
    ("person_occurrence", "person_occurrences", "persons-occurrences",
     "Per-clause occurrence of a person, with role-specific attributes."),
    ("vessel", "vessels", "vessels",
     "Master vessel records (ship name, home port, master, owner)."),
    ("vessel_occurrence", "vessel_occurrences", "vessels-occurrences",
     "Per-clause occurrence of a vessel."),
    ("social_identity", "social_identities", "social-identities",
     "Master social-identity records (e.g. 'merchant', 'mariner')."),
    ("social_identity_occurrence", "social_identity_occurrences",
     "social-identities-occurrences",
     "Per-clause occurrence of a social identity, including secondary "
     "identity, sex, and related-to references."),
    ("relationship", "relationships", "relationships",
     "Master relationship vocabulary."),
    ("relationship_occurrence", "relationship_occurrences",
     "relationships-occurrences",
     "Per-clause relationship between two entities (subject, object, "
     "polymorphic types)."),
    ("event_occurrence", "event_occurrences", "events-occurrences",
     "Per-clause event occurrence: actor, counterparty, object, means, "
     "place(s), and modality."),
    ("attribute_occurrence", "attribute_occurrences",
     "attributes-occurrences",
     "Per-clause attribute applied to an object (polymorphic object_type)."),
    ("quantified_statement_occurrence", "quantified_statement_occurrences",
     "quantified-statements-occurrences",
     "Per-clause quantified statement: quantity, unit, per-quantity, etc."),
    ("clause", "clauses", "clauses",
     "Document clauses — the unit of analysis everything else attaches to."),
    ("document", "documents", "documents",
     "Document metadata: archival reference, parties, dates, court terms."),
    ("summary", "summaries", "summaries",
     "Free-form per-document summary text. Auto-incrementing integer id."),
    ("keyword", "keywords", "keywords",
     "Master keyword list with optional definition + vector embedding."),
    ("keyword_occurrence", "keyword_occurrences", "keywords-occurrences",
     "Per-document keyword occurrence."),
    ("chat_record", "chat_records", "chat-records",
     "Per-document chat transcript text. Auto-incrementing integer id."),
]


# ── Tool registration ───────────────────────────────────────────────────────

def _register_entity_tools(mcp: FastMCP, singular: str, plural: str,
                           prefix: str, description: str) -> None:
    """Register list / get / create / delete tools for one entity table."""

    list_name = f"list_{plural}"
    get_name = f"get_{singular}"
    create_name = f"create_{singular}"
    delete_name = f"delete_{singular}"

    list_doc = (
        f"List {plural}.\n\n{description}\n\n"
        f"Args:\n"
        f"  limit: max rows to return (1–200, default 50).\n"
        f"  offset: row offset for pagination (default 0).\n\n"
        f"Returns a list of objects."
    )
    get_doc = (
        f"Fetch a single {singular} by id.\n\n{description}\n\n"
        f"Returns the row or raises an error if not found."
    )
    create_doc = (
        f"Create a new {singular}.\n\n{description}\n\n"
        f"The ``body`` must be a JSON object matching the {singular} "
        f"schema. The caller (you) generates the ``id`` field. If the "
        f"server returns a 409 conflict the chosen id already exists — "
        f"generate a new id and retry."
    )
    delete_doc = (
        f"Delete a {singular} by id. Returns 404 if not found, 409 if "
        f"the row is still referenced by another table."
    )

    @mcp.tool(name=list_name, description=list_doc)
    async def _list(limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        try:
            return await _client().list(prefix, limit=limit, offset=offset)
        except ApiError as exc:
            raise RuntimeError(exc.message) from exc

    @mcp.tool(name=get_name, description=get_doc)
    async def _get(item_id: str) -> dict[str, Any]:
        try:
            return await _client().get(prefix, item_id)
        except ApiError as exc:
            raise RuntimeError(exc.message) from exc

    @mcp.tool(name=create_name, description=create_doc)
    async def _create(body: dict[str, Any]) -> dict[str, Any]:
        try:
            return await _client().create(prefix, body)
        except ApiError as exc:
            raise RuntimeError(exc.message) from exc

    @mcp.tool(name=delete_name, description=delete_doc)
    async def _delete(item_id: str) -> dict[str, str]:
        try:
            await _client().delete(prefix, item_id)
            return {"status": "deleted", "id": str(item_id)}
        except ApiError as exc:
            raise RuntimeError(exc.message) from exc


def register_all_tools(mcp: FastMCP) -> None:
    """Wire every entity's CRUD tools and the two utility tools."""

    @mcp.tool(
        name="health_check",
        description=(
            "Check that the fast-gathr API is reachable. Does not "
            "require authentication. Returns ``{'status': 'ok'}`` on "
            "success."
        ),
    )
    async def _health() -> dict[str, Any]:
        # Construct the client without a token — /health is public.
        from .client import GathrClient

        try:
            return await GathrClient(
                current_bearer_token.get() or "unauthenticated"
            ).health()
        except ApiError as exc:
            raise RuntimeError(exc.message) from exc

    @mcp.tool(
        name="whoami",
        description=(
            "Return the authenticated user's identity. Useful for "
            "verifying that the configured API token is valid."
        ),
    )
    async def _whoami() -> dict[str, Any]:
        try:
            return await _client().whoami()
        except ApiError as exc:
            raise RuntimeError(exc.message) from exc

    for singular, plural, prefix, description in ENTITIES:
        _register_entity_tools(mcp, singular, plural, prefix, description)
