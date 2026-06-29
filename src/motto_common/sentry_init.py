"""Sentry error tracking initialisation for the Motto fleet.

Wires ``sentry-sdk`` to the ``SENTRY_DSN`` env var, tags every event with
``agent`` and ``host`` so we can slice fleet errors by repo and deployment
target, and exposes a small ``capture_main_loop`` decorator that captures any
exception escaping the main loop before re-raising.

Auto-initialises on import when ``SENTRY_DSN`` is set, so a single
``import motto_common.sentry_init`` at the top of an entrypoint module is
enough — no need to call ``init_sentry`` yourself unless you want to pass
a specific agent name.

Environment:
    SENTRY_DSN                 - DSN; when unset, init is a no-op.
    MOTTO_AGENT_NAME           - default agent name for auto-init on import.
    DEPLOY_ENV                 - environment name, defaults to ``prd``.
    DEPLOY_HOST                - overrides the default host tag.
    SENTRY_TRACES_SAMPLE_RATE  - traces sample rate, defaults to ``0.1``.
    GIT_SHA / RELEASE_SHA      - explicit release SHA; otherwise read from git.
"""

from __future__ import annotations

import functools
import os
import subprocess
from collections.abc import Callable
from typing import ParamSpec

import sentry_sdk

DEFAULT_HOST = "northflank"

P = ParamSpec("P")
R_co = object  # TypeVar-like placeholder — replaced by actual R in capture_main_loop


def _git_sha() -> str:
    """Return the current git commit SHA, falling back to ``"unknown"``.

    Checks ``GIT_SHA`` and ``RELEASE_SHA`` environment variables first, then
    shells out to ``git rev-parse HEAD``.  If all sources fail the function
    returns ``"unknown"`` rather than raising.
    """
    sha = os.getenv("GIT_SHA") or os.getenv("RELEASE_SHA")
    if sha:
        return sha
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL)
            .decode()
            .strip()
        )
    except Exception:  # noqa: BLE001
        return "unknown"


def init_sentry(agent_name: str, host: str | None = None) -> bool:
    """Initialise Sentry with environment, release and context tags.

    Args:
        agent_name: Human-readable agent / repo name tagged on every event.
        host: Deployment host tag (defaults to ``DEPLOY_HOST`` env var or
            ``DEFAULT_HOST``).

    Returns:
        ``True`` when Sentry was successfully initialised, ``False`` when
        ``SENTRY_DSN`` is missing (a no-op).
    """
    dsn = os.getenv("SENTRY_DSN")
    if not dsn:
        return False

    sentry_sdk.init(
        dsn=dsn,
        environment=os.getenv("DEPLOY_ENV", "prd"),
        release=_git_sha(),
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
    )
    sentry_sdk.set_tag("agent", agent_name)
    sentry_sdk.set_tag("host", host or os.getenv("DEPLOY_HOST", DEFAULT_HOST))
    return True


def capture_main_loop[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Decorator: capture any exception escaping the main loop, then re-raise.

    Uses Python 3.12+ `[**P, R]` unified generics syntax so the wrapped
    function's parameter and return types are preserved exactly.
    """

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            sentry_sdk.capture_exception(e)
            raise

    return wrapper


# Auto-init on import so a bare ``import motto_common.sentry_init`` is
# sufficient to wire up Sentry.  The agent name is drawn from the
# ``MOTTO_AGENT_NAME`` environment variable — set it per-repo in your
# Northflank config, Dockerfile, or Doppler secrets.
_auto_agent = os.getenv("MOTTO_AGENT_NAME", "unknown")
init_sentry(_auto_agent)
