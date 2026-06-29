"""Shared configuration loading for the Motto fleet.

Reads environment variables (optionally filtered by a prefix) and returns
them as a plain dict.  In production the env vars are typically injected by
Doppler or Northflank secret groups, making this module the single entry
point for config retrieval across the fleet.
"""

from __future__ import annotations

import os


def load_config(prefix: str = "MOTTO_") -> dict[str, str]:
    """Return a dict of environment variables whose names start with *prefix*.

    The prefix is stripped from the key in the returned dict.  For example,
    with ``prefix="MOTTO_"`` the variable ``MOTTO_ENV=prd`` becomes
    ``{"ENV": "prd"}``.

    Args:
        prefix: The prefix to filter and strip.  Defaults to ``"MOTTO_"``.

    Returns:
        A dict mapping stripped variable names to their string values.
    """
    result: dict[str, str] = {}
    for key, value in os.environ.items():
        if key.startswith(prefix) and len(key) > len(prefix):
            stripped = key[len(prefix) :]
            result[stripped] = value
    return result
