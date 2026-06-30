"""Benchmark tests for motto_common.sentry_init performance-critical paths.

Uses pytest-benchmark to measure and track the cost of sentry_init
function calls, git SHA resolution, and the capture_main_loop decorator.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Generator
from typing import Any
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _clear_sentry_env() -> Generator[None, None, None]:
    """Ensure SENTRY_DSN is not set from the real environment."""
    with patch.dict(os.environ, {}, clear=True):
        yield


def test_benchmark_init_sentry_without_dsn(benchmark: Any) -> None:
    """Benchmark: init_sentry returning False when SENTRY_DSN is not set."""
    from motto_common.sentry_init import init_sentry

    with patch.dict(os.environ, {}, clear=True):
        result = benchmark(init_sentry, "bench-agent")
        assert result is False


def test_benchmark_init_sentry_with_dsn(benchmark: Any) -> None:
    """Benchmark: init_sentry with a valid DSN (including Sentry SDK init overhead)."""
    from motto_common.sentry_init import init_sentry

    with patch.dict(os.environ, {"SENTRY_DSN": "https://key@sentry.io/1"}):
        # Benchmark the full init path
        benchmark(init_sentry, "bench-agent")


def test_benchmark_git_sha(benchmark: Any) -> None:
    """Benchmark: _git_sha() resolution (should be fast, no git call in CI)."""
    from motto_common.sentry_init import _git_sha

    result = benchmark(_git_sha)
    assert isinstance(result, str)
    assert len(result) > 0


def test_benchmark_git_sha_from_env(benchmark: Any) -> None:
    """Benchmark: _git_sha() when GIT_SHA is already set in environment."""
    from motto_common.sentry_init import _git_sha

    with patch.dict(os.environ, {"GIT_SHA": "abc123def456"}):
        result = benchmark(_git_sha)
        assert result == "abc123def456"


def test_benchmark_capture_main_loop_success(benchmark: Any) -> None:
    """Benchmark: capture_main_loop decorator on a successful function call."""
    from motto_common.sentry_init import capture_main_loop

    @capture_main_loop
    def fast_func(x: int) -> int:
        return x * 2

    result = benchmark(fast_func, 42)
    assert result == 84


def test_benchmark_capture_main_loop_exception(benchmark: Any) -> None:
    """Benchmark: capture_main_loop decorator when the wrapped function raises."""
    import sentry_sdk

    from motto_common.sentry_init import capture_main_loop

    @capture_main_loop
    def failing_func() -> None:
        raise ValueError("benchmark error")

    def _run_and_catch() -> None:
        with patch.object(sentry_sdk, "capture_exception"):
            try:
                failing_func()
            except ValueError:
                pass

    benchmark(_run_and_catch)


def test_benchmark_import_overhead(benchmark: Any) -> None:
    """Benchmark: cost of importing the sentry_init module."""

    def _import() -> None:
        import motto_common.sentry_init  # noqa: F811

        importlib.reload(motto_common.sentry_init)

    benchmark(_import)
