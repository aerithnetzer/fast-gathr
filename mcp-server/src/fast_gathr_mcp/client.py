"""HTTP client for the fast-gathr API.

Forwards an inbound bearer token from the MCP request to every outbound
request. Translates HTTP errors into ``ApiError`` so tools can surface
useful messages (e.g. 409-on-create instructs Claude to retry with a new
id).
"""

from __future__ import annotations

import os
from typing import Any

import httpx


DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class ApiError(Exception):
    """Raised when the fast-gathr API returns a non-2xx response.

    The MCP server translates this into a tool error string for the
    caller (Claude) to read.
    """

    def __init__(self, status: int, message: str, body: Any = None) -> None:
        super().__init__(f"[{status}] {message}")
        self.status = status
        self.message = message
        self.body = body


def _api_base() -> str:
    base = os.environ.get("FAST_GATHR_API_BASE")
    if not base:
        raise RuntimeError(
            "FAST_GATHR_API_BASE is not set. The MCP server must know "
            "where the fast-gathr API lives."
        )
    return base.rstrip("/")


def _format_detail(payload: Any) -> str:
    """Pull a human-readable detail string out of a FastAPI error body."""
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str):
            return detail
        if detail is not None:
            return str(detail)
    return str(payload)


class GathrClient:
    """Thin authenticated wrapper around the fast-gathr API.

    A new client is constructed per MCP request so the bearer token is
    request-scoped and never leaks across users.
    """

    def __init__(self, bearer_token: str) -> None:
        if not bearer_token:
            raise ApiError(401, "No API token provided")
        self._headers = {
            "Authorization": f"Bearer {bearer_token}",
            "Accept": "application/json",
        }
        self._base = _api_base()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
    ) -> Any:
        url = f"{self._base}{path}"
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            try:
                response = await client.request(
                    method,
                    url,
                    headers=self._headers,
                    params=params,
                    json=json,
                )
            except httpx.HTTPError as exc:
                raise ApiError(
                    0, f"network error contacting fast-gathr API: {exc}"
                ) from exc

        if 200 <= response.status_code < 300:
            if response.status_code == 204 or not response.content:
                return None
            return response.json()

        # Surface useful errors to Claude.
        try:
            body = response.json()
        except ValueError:
            body = response.text

        message = _format_detail(body)
        if response.status_code == 401:
            message = (
                "fast-gathr API token is invalid or has been revoked. "
                "Ask the user to mint a new token."
            )
        elif response.status_code == 403:
            message = "Permission denied. " + message
        elif response.status_code == 404:
            message = "Not found. " + message
        elif response.status_code == 409:
            # Surface a hint so Claude knows to retry with a fresh id.
            message = (
                f"Conflict: {message}. Generate a new id (different from "
                "the one just used) and retry."
            )
        raise ApiError(response.status_code, message, body)

    # ── Convenience verbs ────────────────────────────────────────────────

    async def list(
        self,
        prefix: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return await self._request(
            "GET",
            f"/{prefix}/",
            params={"limit": limit, "offset": offset},
        ) or []

    async def get(self, prefix: str, item_id: str | int) -> dict[str, Any]:
        return await self._request("GET", f"/{prefix}/{item_id}")

    async def create(self, prefix: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", f"/{prefix}/", json=body)

    async def delete(self, prefix: str, item_id: str | int) -> None:
        await self._request("DELETE", f"/{prefix}/{item_id}")

    async def whoami(self) -> dict[str, Any]:
        return await self._request("GET", "/users/me")

    async def health(self) -> dict[str, Any]:
        # /health is unauthenticated, but we still go through the same
        # client so connectivity is exercised end-to-end.
        return await self._request("GET", "/health")
