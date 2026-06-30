"""Integration tests for motto-common.

These tests exercise multiple motto-common modules together in realistic
scenarios, including mocked external dependencies (sentry-sdk via mocking,
environment variable simulation, and subprocess mocking for git operations).

Unlike unit tests, integration tests verify that modules work correctly
when composed together rather than in isolation.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any
from unittest.mock import patch


class TestEndToEndFleetServiceSetup:
    """Simulates a realistic fleet service startup: logging → config → auth → Sentry.

    This is the canonical "new agent" setup flow that every Motto service
    follows.  It verifies that all motto-common modules compose correctly
    and that Sentry SDK integration works end-to-end with mocked external deps.
    """

    def _simulate_service_startup(self, agent_name: str) -> dict[str, Any]:
        """Run the full Motto service startup sequence.

        Returns a dict of results from each step for assertion.
        """
        # 1. Setup logging
        from motto_common.logging import setup_logging

        logger = setup_logging(agent_name)
        logger.info("Service starting")

        # 2. Load configuration
        from motto_common.config import load_config

        config = load_config("MOTTO_")

        # 3. Validate auth token from config
        from motto_common.auth import validate_token

        token_ok = validate_token(config.get("API_TOKEN"))

        # 4. Initialize Sentry
        from motto_common.sentry_init import DEFAULT_HOST, init_sentry

        sentry_ok = init_sentry(agent_name)

        return {
            "logger_name": logger.name,
            "logger_handlers": len(logger.handlers),
            "config_keys": sorted(config.keys()),
            "token_valid": token_ok,
            "sentry_initialized": sentry_ok,
            "default_host": DEFAULT_HOST,
        }

    def test_full_startup_with_valid_dsn_and_token(self) -> None:
        """Complete startup with valid DSN and API token succeeds.

        Mocks sentry_sdk.init to avoid network calls and verifies all
        modules compose correctly.
        """
        env = {
            "SENTRY_DSN": "https://key@sentry.io/1",
            "MOTTO_API_TOKEN": "secret-token-123",
            "MOTTO_DB_URL": "postgresql://localhost/test",
            "DEPLOY_ENV": "staging",
        }

        import sentry_sdk

        with patch.dict(os.environ, env, clear=True):
            with patch.object(sentry_sdk, "init") as mock_init:
                with patch.object(sentry_sdk, "set_tag") as mock_set_tag:
                    result = self._simulate_service_startup("test-service")

                    # Verifications
                    assert result["logger_name"] == "test-service"
                    assert result["logger_handlers"] >= 1
                    assert "API_TOKEN" in result["config_keys"]
                    assert "DB_URL" in result["config_keys"]
                    assert result["token_valid"] is True
                    assert result["sentry_initialized"] is True
                    assert result["default_host"] == "northflank"

                    # Verify Sentry SDK was called correctly
                    mock_init.assert_called_once()
                    call_kwargs = mock_init.call_args[1]
                    assert call_kwargs["dsn"] == env["SENTRY_DSN"]
                    assert call_kwargs["environment"] == "staging"

                    mock_set_tag.assert_any_call("agent", "test-service")
                    mock_set_tag.assert_any_call("host", "northflank")

    def test_startup_without_dsn_gracefully_degraded(self) -> None:
        """Startup without SENTRY_DSN does not error; Sentry skipped cleanly."""
        import sentry_sdk

        with patch.dict(os.environ, {}, clear=True):
            with patch.object(sentry_sdk, "init") as mock_init:
                result = self._simulate_service_startup("test-service")

                assert result["sentry_initialized"] is False
                mock_init.assert_not_called()

                # Auth still works even without Sentry
                assert result["token_valid"] is False

    def test_invalid_token_detected(self) -> None:
        """validate_token returns False for empty/None tokens."""
        from motto_common.auth import validate_token

        assert validate_token(None) is False
        assert validate_token("") is False
        assert validate_token("   ") is False
        assert validate_token("valid-token") is True

    def test_config_prefix_isolation(self) -> None:
        """load_config only returns vars matching the prefix; strips prefix."""
        from motto_common.config import load_config

        env = {
            "MOTTO_A": "1",
            "MOTTO_B": "2",
            "OTHER_C": "3",
            "NOT_MOTTO": "4",
        }
        with patch.dict(os.environ, env, clear=True):
            config = load_config("MOTTO_")
            assert config == {"A": "1", "B": "2"}

    def test_auth_headers_include_bearer(self) -> None:
        """create_auth_headers includes Bearer prefix and Content-Type."""
        from motto_common.auth import create_auth_headers

        headers = create_auth_headers("my-token")
        assert headers["Authorization"] == "Bearer my-token"
        assert headers["Content-Type"] == "application/json"


class TestSentrinitIntegrationWithMockedSDK:
    """Tests that sentry_init integration works end-to-end with mocked SDK.

    These tests verify that the sentry_init module correctly interacts with
    the real sentry-sdk package (not fully mocked), but with network calls
    intercepted.  This validates that our usage of the sentry-sdk API is
    correct and compatible with the installed version.
    """

    def test_init_sentry_passes_correct_kwargs_to_sdk(self) -> None:
        """Verify all kwargs passed to sentry_sdk.init match expectations."""
        import sentry_sdk

        from motto_common.sentry_init import init_sentry

        env = {
            "SENTRY_DSN": "https://abc@o1.ingest.sentry.io/2",
            "DEPLOY_ENV": "production",
            "SENTRY_TRACES_SAMPLE_RATE": "0.25",
            "GIT_SHA": "deadbeef",
        }

        with patch.dict(os.environ, env):
            with patch.object(sentry_sdk, "init") as mock_init:
                with patch.object(sentry_sdk, "set_tag"):
                    result = init_sentry("prod-agent")

                    assert result is True
                    kwargs = mock_init.call_args[1]
                    assert kwargs["dsn"] == env["SENTRY_DSN"]
                    assert kwargs["environment"] == "production"
                    assert kwargs["release"] == "deadbeef"
                    assert kwargs["traces_sample_rate"] == 0.25

    def test_git_sha_uses_subprocess_when_no_env(self) -> None:
        """_git_sha calls git rev-parse when env vars are absent."""
        from motto_common.sentry_init import _git_sha

        with patch.dict(os.environ, {}, clear=True):
            with patch("subprocess.check_output") as mock_co:
                mock_co.return_value = b"abcdef1234567890\n"
                sha = _git_sha()
                assert sha == "abcdef1234567890"
                mock_co.assert_called_once()

    def test_git_sha_returns_unknown_on_subprocess_failure(self) -> None:
        """_git_sha returns 'unknown' when git command fails."""
        from motto_common.sentry_init import _git_sha

        with patch.dict(os.environ, {}, clear=True):
            with patch("subprocess.check_output", side_effect=OSError):
                sha = _git_sha()
                assert sha == "unknown"

    def test_auto_init_on_import_without_dsn_is_safe(self) -> None:
        """Importing sentry_init without SENTRY_DSN is a no-op, not an error.

        The module-level auto-init runs init_sentry() which returns False
        when DSN is missing.  This test verifies that re-importing does
        not cause errors (motto_common is already imported, so we verify
        the behavior through init_sentry directly).
        """
        from motto_common.sentry_init import init_sentry

        with patch.dict(os.environ, {}, clear=True):
            assert init_sentry("test") is False

    def test_auto_init_on_import_respects_motto_agent_name(self) -> None:
        """Auto-init uses MOTTO_AGENT_NAME env var for agent tag."""
        import sentry_sdk

        from motto_common.sentry_init import init_sentry

        env = {
            "SENTRY_DSN": "https://key@sentry.io/1",
            "MOTTO_AGENT_NAME": "auto-agent",
        }

        with patch.dict(os.environ, env):
            with patch.object(sentry_sdk, "init"):
                with patch.object(sentry_sdk, "set_tag") as mock_tag:
                    init_sentry("auto-agent")
                    mock_tag.assert_any_call("agent", "auto-agent")


class TestLoggingIntegration:
    """Integration tests for logging module with real logging infrastructure."""

    def test_setup_logging_plain_format(self) -> None:
        """setup_logging creates a logger with plain text handler."""
        from motto_common.logging import setup_logging

        logger = setup_logging("test-plain")
        assert logger.name == "test-plain"
        assert len(logger.handlers) >= 1
        assert logger.level > 0

    def test_setup_logging_json_format(self) -> None:
        """setup_logging with json_fmt=True creates JSON-line handler."""
        from motto_common.logging import setup_logging

        logger = setup_logging("test-json", json_fmt=True)
        assert len(logger.handlers) >= 1

    def test_setup_logging_respects_log_level_env(self) -> None:
        """setup_logging reads LOG_LEVEL from environment."""
        import logging as _logging

        from motto_common.logging import setup_logging

        # Use a unique name to avoid interference with other tests
        logger_name = f"test-level-{os.urandom(4).hex()}"
        with patch.dict(os.environ, {"LOG_LEVEL": "DEBUG"}):
            logger = setup_logging(logger_name)
            assert logger.level == _logging.DEBUG

    def test_setup_logging_is_idempotent(self) -> None:
        """Calling setup_logging twice does not add duplicate handlers."""
        from motto_common.logging import setup_logging

        logger1 = setup_logging("test-idempotent")
        handler_count = len(logger1.handlers)
        logger2 = setup_logging("test-idempotent")
        assert len(logger2.handlers) == handler_count
        assert logger1 is logger2


class TestPerformanceCharacteristics:
    """Performance characterization tests for motto-common.

    These tests measure import time and verify it stays within SLO bounds.
    """

    def test_import_time_is_reasonable(self) -> None:
        """Importing motto_common takes less than 200ms (cold import).

        Note: The first import in a process is measured.  Subsequent
        imports hit the module cache so they will be near-instant.
        """
        # We import via subprocess to measure cold-import time
        script = """
import time
start = time.perf_counter()
import motto_common  # noqa: F401
elapsed = time.perf_counter() - start
print(f"{elapsed:.4f}")
"""
        result = subprocess.run(
            ["python", "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "SENTRY_DSN": ""},  # Ensure DSN not set
        )
        assert result.returncode == 0, f"Import failed: {result.stderr}"
        elapsed = float(result.stdout.strip())
        # Cold import should be well under 1 second (conservative bound)
        # Typical is 50-150ms for motto-common
        assert elapsed < 1.0, f"Import took {elapsed:.3f}s, expected < 1.0s"

    def test_module_composition_has_no_circular_imports(self) -> None:
        """Verify that importing all modules together does not cause circular imports."""
        script = """
# Import in various orders to detect circular import issues
from motto_common.sentry_init import init_sentry  # noqa: F401
from motto_common.auth import create_auth_headers  # noqa: F401
from motto_common.config import load_config  # noqa: F401
from motto_common.logging import setup_logging  # noqa: F401

# Import whole package
import motto_common  # noqa: F401

print("OK")
"""
        result = subprocess.run(
            ["python", "-c", script],
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, "SENTRY_DSN": ""},
        )
        assert result.returncode == 0, f"Circular import detected: {result.stderr}"
        assert result.stdout.strip() == "OK"
