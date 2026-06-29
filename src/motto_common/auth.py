"""Shared authentication utilities for the Motto fleet.

Provides lightweight helpers for creating auth headers and validating
bearer tokens — used across all Motto services that call secured APIs.
"""

from __future__ import annotations


def create_auth_headers(token: str, *, content_type: str = "application/json") -> dict[str, str]:
    """Return a dict of HTTP headers with a Bearer token and Content-Type.

    Args:
        token: The bearer token string.
        content_type: Value for the ``Content-Type`` header.

    Returns:
        A dict suitable for passing to ``requests`` / ``httpx`` / ``aiohttp``.
    """
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": content_type,
    }


def validate_token(token: str | None) -> bool:
    """Return ``True`` if *token* is a non-empty string.

    This is a basic presence check.  Real services should layer on top of
    this (e.g. JWT decoding, expiry checks, or Doppler ``/verify`` calls).

    Args:
        token: The token string (or ``None``).

    Returns:
        ``True`` when the token looks usable.
    """
    if token is None:
        return False
    return len(token.strip()) > 0
