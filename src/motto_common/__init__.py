"""motto-common — shared Python utilities for the Motto fleet.

Provides:
- sentry_init: parameterized Sentry initialisation (init_sentry, capture_main_loop, _git_sha)
- auth: shared authentication patterns
- config: shared configuration loading (Doppler, env vars)
- logging: shared logging setup
"""

from motto_common.auth import create_auth_headers, validate_token
from motto_common.config import load_config
from motto_common.logging import setup_logging
from motto_common.sentry_init import DEFAULT_HOST, _git_sha, capture_main_loop, init_sentry

__all__ = [
    "init_sentry",
    "capture_main_loop",
    "_git_sha",
    "DEFAULT_HOST",
    "create_auth_headers",
    "validate_token",
    "load_config",
    "setup_logging",
]
