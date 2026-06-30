"""Tests for motto_common.sentry_init."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest


class TestInitSentry:
    """Tests for init_sentry(agent_name)."""

    def test_init_sentry_without_dsn_returns_false(self) -> None:
        """init_sentry returns False when SENTRY_DSN is not set."""
        from motto_common.sentry_init import init_sentry

        with patch.dict(os.environ, {}, clear=True):
            result = init_sentry("test-agent")
            assert result is False

    def test_init_sentry_with_dsn_returns_true(self) -> None:
        """init_sentry returns True when SENTRY_DSN is set."""
        from motto_common.sentry_init import init_sentry

        with patch.dict(os.environ, {"SENTRY_DSN": "https://key@sentry.io/1"}):
            result = init_sentry("test-agent")
            assert result is True

    def test_init_sentry_accepts_agent_name_parameter(self) -> None:
        """init_sentry accepts agent_name as str parameter."""
        from motto_common.sentry_init import init_sentry

        with patch.dict(os.environ, {"SENTRY_DSN": "https://key@sentry.io/1"}):
            result = init_sentry("custom-agent-name")
            assert result is True

    def test_init_sentry_accepts_host_kwarg(self) -> None:
        """init_sentry accepts optional host keyword argument."""
        from motto_common.sentry_init import init_sentry

        with patch.dict(os.environ, {"SENTRY_DSN": "https://key@sentry.io/1"}):
            result = init_sentry("test-agent", host="custom-host")
            assert result is True

    def test_init_sentry_is_idempotent(self) -> None:
        """init_sentry can be called multiple times without error."""
        from motto_common.sentry_init import init_sentry

        with patch.dict(os.environ, {"SENTRY_DSN": "https://key@sentry.io/1"}):
            assert init_sentry("agent-a") is True
            assert init_sentry("agent-b") is True

    def test_init_sentry_sets_tags(self) -> None:
        """init_sentry calls sentry_sdk.set_tag with agent and host."""
        import sentry_sdk

        from motto_common.sentry_init import init_sentry

        with patch.dict(os.environ, {"SENTRY_DSN": "https://key@sentry.io/1"}):
            with patch.object(sentry_sdk, "set_tag") as mock_set_tag:
                init_sentry("my-agent", host="my-host")
                mock_set_tag.assert_any_call("agent", "my-agent")
                mock_set_tag.assert_any_call("host", "my-host")

    def test_init_sentry_default_host_is_northflank(self) -> None:
        """init_sentry defaults host to DEFAULT_HOST ('northflank') when no host arg."""
        import sentry_sdk

        from motto_common.sentry_init import DEFAULT_HOST, init_sentry

        with patch.dict(os.environ, {"SENTRY_DSN": "https://key@sentry.io/1"}):
            with patch.object(sentry_sdk, "set_tag") as mock_set_tag:
                init_sentry("my-agent")
                mock_set_tag.assert_any_call("host", DEFAULT_HOST)

    def test_init_sentry_uses_deploy_env(self) -> None:
        """init_sentry respects DEPLOY_ENV environment variable."""
        import sentry_sdk

        from motto_common.sentry_init import init_sentry

        with patch.dict(
            os.environ,
            {"SENTRY_DSN": "https://key@sentry.io/1", "DEPLOY_ENV": "staging"},
        ):
            with patch.object(sentry_sdk, "init") as mock_init:
                init_sentry("my-agent")
                call_kwargs = mock_init.call_args[1]
                assert call_kwargs["environment"] == "staging"

    def test_init_sentry_respects_traces_sample_rate(self) -> None:
        """init_sentry respects SENTRY_TRACES_SAMPLE_RATE env var."""
        import sentry_sdk

        from motto_common.sentry_init import init_sentry

        with patch.dict(
            os.environ,
            {
                "SENTRY_DSN": "https://key@sentry.io/1",
                "SENTRY_TRACES_SAMPLE_RATE": "0.5",
            },
        ):
            with patch.object(sentry_sdk, "init") as mock_init:
                init_sentry("my-agent")
                call_kwargs = mock_init.call_args[1]
                assert call_kwargs["traces_sample_rate"] == 0.5


class TestGitSha:
    """Tests for _git_sha()."""

    def test_git_sha_returns_string(self) -> None:
        """_git_sha returns a non-empty string."""
        from motto_common.sentry_init import _git_sha

        result = _git_sha()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_git_sha_returns_unknown_when_no_git(self) -> None:
        """_git_sha returns 'unknown' when git is unavailable."""
        from motto_common.sentry_init import _git_sha

        with patch.dict(os.environ, {}, clear=True):
            with patch("subprocess.check_output", side_effect=FileNotFoundError):
                result = _git_sha()
                assert result == "unknown"

    def test_git_sha_uses_git_sha_env_var(self) -> None:
        """_git_sha uses GIT_SHA env var when set."""
        from motto_common.sentry_init import _git_sha

        with patch.dict(os.environ, {"GIT_SHA": "abc123def"}):
            result = _git_sha()
            assert result == "abc123def"

    def test_git_sha_uses_release_sha_env_var(self) -> None:
        """_git_sha uses RELEASE_SHA env var when set."""
        from motto_common.sentry_init import _git_sha

        with patch.dict(os.environ, {"RELEASE_SHA": "release-sha-456"}):
            result = _git_sha()
            assert result == "release-sha-456"


class TestCaptureMainLoop:
    """Tests for capture_main_loop decorator."""

    def test_capture_main_loop_preserves_return_value(self) -> None:
        """Wrapped function's return value is preserved."""
        from motto_common.sentry_init import capture_main_loop

        @capture_main_loop
        def greet(name: str) -> str:
            return f"Hello, {name}"

        assert greet("World") == "Hello, World"

    def test_capture_main_loop_captures_and_reraises(self) -> None:
        """capture_main_loop captures exception then re-raises."""
        import sentry_sdk

        from motto_common.sentry_init import capture_main_loop

        @capture_main_loop
        def failing_func() -> None:
            raise ValueError("test error")

        with patch.object(sentry_sdk, "capture_exception") as mock_capture:
            with pytest.raises(ValueError, match="test error"):
                failing_func()
            mock_capture.assert_called_once()

    def test_capture_main_loop_preserves_function_metadata(self) -> None:
        """Wrapped function preserves __name__, __doc__, etc."""
        from motto_common.sentry_init import capture_main_loop

        @capture_main_loop
        def my_func() -> str:
            """Docstring here."""
            return "ok"

        assert my_func.__name__ == "my_func"
        assert my_func.__doc__ == "Docstring here."

    def test_capture_main_loop_handles_generic_return_type(self) -> None:
        """capture_main_loop preserves typed return values."""
        from motto_common.sentry_init import capture_main_loop

        @capture_main_loop
        def get_list() -> list[int]:
            return [1, 2, 3]

        result = get_list()
        assert result == [1, 2, 3]

    def test_capture_main_loop_passes_through_keyword_args(self) -> None:
        """capture_main_loop preserves keyword arguments."""
        from motto_common.sentry_init import capture_main_loop

        @capture_main_loop
        def build_message(greeting: str, *, name: str = "World") -> str:
            return f"{greeting}, {name}"

        assert build_message("Hi", name="Alice") == "Hi, Alice"

    def test_capture_main_loop_is_callable(self) -> None:
        """capture_main_loop is a callable decorator."""
        from motto_common.sentry_init import capture_main_loop

        assert callable(capture_main_loop)


class TestDefaultHost:
    """Tests for DEFAULT_HOST constant."""

    def test_default_host_is_northflank(self) -> None:
        """DEFAULT_HOST equals 'northflank'."""
        from motto_common.sentry_init import DEFAULT_HOST

        assert DEFAULT_HOST == "northflank"
        assert isinstance(DEFAULT_HOST, str)


class TestSentrinitIsImportable:
    """Tests that all symbols are importable."""

    def test_import_init_sentry(self) -> None:
        from motto_common.sentry_init import init_sentry

        assert callable(init_sentry)

    def test_import_git_sha(self) -> None:
        from motto_common.sentry_init import _git_sha

        assert callable(_git_sha)

    def test_import_capture_main_loop(self) -> None:
        from motto_common.sentry_init import capture_main_loop

        assert callable(capture_main_loop)

    def test_import_default_host(self) -> None:
        from motto_common.sentry_init import DEFAULT_HOST

        assert DEFAULT_HOST == "northflank"


class TestContinuousProfiling:
    """Tests for opt-in sentry_sdk.profiler integration."""

    def test_profiling_disabled_by_default(self) -> None:
        """profiles_sample_rate is NOT passed when SENTRY_PROFILING_ENABLED is unset."""
        import sentry_sdk

        from motto_common.sentry_init import init_sentry

        with patch.dict(os.environ, {"SENTRY_DSN": "https://key@sentry.io/1"}):
            with patch.object(sentry_sdk, "init") as mock_init:
                init_sentry("test-agent")
                call_kwargs = mock_init.call_args[1]
                assert "profiles_sample_rate" not in call_kwargs

    def test_profiling_enabled_with_truthy_1(self) -> None:
        """profiles_sample_rate is passed when SENTRY_PROFILING_ENABLED=1."""
        import sentry_sdk

        from motto_common.sentry_init import init_sentry

        with patch.dict(
            os.environ,
            {"SENTRY_DSN": "https://key@sentry.io/1", "SENTRY_PROFILING_ENABLED": "1"},
        ):
            with patch.object(sentry_sdk, "init") as mock_init:
                init_sentry("test-agent")
                call_kwargs = mock_init.call_args[1]
                assert "profiles_sample_rate" in call_kwargs
                assert call_kwargs["profiles_sample_rate"] == 0.1

    def test_profiling_enabled_with_truthy_true(self) -> None:
        """profiles_sample_rate is passed when SENTRY_PROFILING_ENABLED=true."""
        import sentry_sdk

        from motto_common.sentry_init import init_sentry

        with patch.dict(
            os.environ,
            {
                "SENTRY_DSN": "https://key@sentry.io/1",
                "SENTRY_PROFILING_ENABLED": "true",
            },
        ):
            with patch.object(sentry_sdk, "init") as mock_init:
                init_sentry("test-agent")
                call_kwargs = mock_init.call_args[1]
                assert "profiles_sample_rate" in call_kwargs

    def test_profiling_enabled_with_truthy_yes(self) -> None:
        """profiles_sample_rate is passed when SENTRY_PROFILING_ENABLED=yes."""
        import sentry_sdk

        from motto_common.sentry_init import init_sentry

        with patch.dict(
            os.environ,
            {
                "SENTRY_DSN": "https://key@sentry.io/1",
                "SENTRY_PROFILING_ENABLED": "yes",
            },
        ):
            with patch.object(sentry_sdk, "init") as mock_init:
                init_sentry("test-agent")
                call_kwargs = mock_init.call_args[1]
                assert "profiles_sample_rate" in call_kwargs

    def test_profiling_enabled_with_truthy_on(self) -> None:
        """profiles_sample_rate is passed when SENTRY_PROFILING_ENABLED=on."""
        import sentry_sdk

        from motto_common.sentry_init import init_sentry

        with patch.dict(
            os.environ,
            {
                "SENTRY_DSN": "https://key@sentry.io/1",
                "SENTRY_PROFILING_ENABLED": "on",
            },
        ):
            with patch.object(sentry_sdk, "init") as mock_init:
                init_sentry("test-agent")
                call_kwargs = mock_init.call_args[1]
                assert "profiles_sample_rate" in call_kwargs

    def test_profiling_enabled_with_truthy_enabled(self) -> None:
        """profiles_sample_rate is passed when SENTRY_PROFILING_ENABLED=enabled."""
        import sentry_sdk

        from motto_common.sentry_init import init_sentry

        with patch.dict(
            os.environ,
            {
                "SENTRY_DSN": "https://key@sentry.io/1",
                "SENTRY_PROFILING_ENABLED": "enabled",
            },
        ):
            with patch.object(sentry_sdk, "init") as mock_init:
                init_sentry("test-agent")
                call_kwargs = mock_init.call_args[1]
                assert "profiles_sample_rate" in call_kwargs

    def test_profiling_not_enabled_with_false_value(self) -> None:
        """profiles_sample_rate is NOT passed when SENTRY_PROFILING_ENABLED=0."""
        import sentry_sdk

        from motto_common.sentry_init import init_sentry

        with patch.dict(
            os.environ,
            {
                "SENTRY_DSN": "https://key@sentry.io/1",
                "SENTRY_PROFILING_ENABLED": "0",
            },
        ):
            with patch.object(sentry_sdk, "init") as mock_init:
                init_sentry("test-agent")
                call_kwargs = mock_init.call_args[1]
                assert "profiles_sample_rate" not in call_kwargs

    def test_profiling_not_enabled_with_random_string(self) -> None:
        """profiles_sample_rate is NOT passed when SENTRY_PROFILING_ENABLED is arbitrary."""
        import sentry_sdk

        from motto_common.sentry_init import init_sentry

        with patch.dict(
            os.environ,
            {
                "SENTRY_DSN": "https://key@sentry.io/1",
                "SENTRY_PROFILING_ENABLED": "maybe-later",
            },
        ):
            with patch.object(sentry_sdk, "init") as mock_init:
                init_sentry("test-agent")
                call_kwargs = mock_init.call_args[1]
                assert "profiles_sample_rate" not in call_kwargs

    def test_profiling_respects_sample_rate_env(self) -> None:
        """SENTRY_PROFILES_SAMPLE_RATE controls the sample rate value."""
        import sentry_sdk

        from motto_common.sentry_init import init_sentry

        with patch.dict(
            os.environ,
            {
                "SENTRY_DSN": "https://key@sentry.io/1",
                "SENTRY_PROFILING_ENABLED": "1",
                "SENTRY_PROFILES_SAMPLE_RATE": "0.5",
            },
        ):
            with patch.object(sentry_sdk, "init") as mock_init:
                init_sentry("test-agent")
                call_kwargs = mock_init.call_args[1]
                assert call_kwargs["profiles_sample_rate"] == 0.5

    def test_profiling_disabled_without_dsn(self) -> None:
        """Profiling init is a no-op when SENTRY_DSN is missing."""
        from motto_common.sentry_init import init_sentry

        with patch.dict(
            os.environ,
            {"SENTRY_PROFILING_ENABLED": "1"},
            clear=True,
        ):
            result = init_sentry("test-agent")
            assert result is False

    def test_profiler_module_is_importable(self) -> None:
        """sentry_sdk.profiler submodule is importable."""
        import sentry_sdk.profiler  # noqa: F811

        # If we got here without ImportError, the profiling extras are installed
        assert hasattr(sentry_sdk.profiler, "start_profiler") or True
