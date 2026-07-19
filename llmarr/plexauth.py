"""Browser-based Plex sign-in via the plex.tv PIN flow.

Instead of pasting a token, the user links LLMarr to their Plex account: LLMarr
requests a short PIN, the user enters it at https://plex.tv/link (signing in if
needed), and LLMarr polls plex.tv until the PIN is claimed, yielding an account
auth token that works against the local server. The device identity
(``X-Plex-Client-Identifier``) is persisted so the same LLMarr registration is
reused across logins.
"""

from __future__ import annotations

import uuid

import httpx

_PLEX_TV = "https://plex.tv/api/v2"
LINK_URL = "https://plex.tv/link"


def new_client_id() -> str:
    return str(uuid.uuid4())


def _headers(client_id: str, product: str) -> dict:
    return {
        "Accept": "application/json",
        "X-Plex-Product": product,
        "X-Plex-Client-Identifier": client_id,
    }


async def request_pin(client_id: str, product: str = "LLMarr") -> dict:
    """Create a PIN. Returns its id and the short code the user types at
    https://plex.tv/link."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(f"{_PLEX_TV}/pins", headers=_headers(client_id, product))
        resp.raise_for_status()
        data = resp.json()
    return {"id": data["id"], "code": data["code"]}


async def poll_token(pin_id: str | int, client_id: str, product: str = "LLMarr") -> str | None:
    """Return the auth token once the PIN is claimed, else ``None``."""
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{_PLEX_TV}/pins/{pin_id}", headers=_headers(client_id, product))
        resp.raise_for_status()
        return resp.json().get("authToken")
