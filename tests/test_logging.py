"""Tests for motto_common.logging."""

from __future__ import annotations


class TestLoggingImports:
    """Import/smoke tests for the logging module."""

    def test_logging_module_is_importable(self) -> None:
        """motto_common.logging can be imported."""
        import motto_common.logging  # noqa: F401

    def test_logging_module_exports_setup_logging(self) -> None:
        """logging module exports setup_logging."""
        from motto_common.logging import setup_logging

        assert callable(setup_logging)

    def test_setup_logging_returns_logger(self) -> None:
        """setup_logging returns a Logger instance."""
        import logging

        from motto_common.logging import setup_logging

        logger = setup_logging("test-logger")
        assert isinstance(logger, logging.Logger)
        assert logger.name == "test-logger"

    def test_setup_logging_sets_level(self) -> None:
        """setup_logging sets the logging level from env or default."""
        from motto_common.logging import setup_logging

        logger = setup_logging("test-level")
        assert logger.level > 0  # Some logging level is set

    def test_setup_logging_adds_handler(self) -> None:
        """setup_logging adds a StreamHandler by default."""
        import logging

        from motto_common.logging import setup_logging

        logger = setup_logging("test-handler")
        assert len(logger.handlers) >= 1
        assert any(isinstance(h, logging.Handler) for h in logger.handlers)

    def test_setup_logging_json_format(self) -> None:
        """setup_logging with json_fmt=True produces structured output handler."""

        from motto_common.logging import setup_logging

        logger = setup_logging("json-logger", json_fmt=True)
        assert len(logger.handlers) >= 1

    def test_logging_importable_from_package(self) -> None:
        """Logging symbols are importable from motto_common."""
        from motto_common import setup_logging

        assert callable(setup_logging)
