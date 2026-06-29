"""Tests for motto_common.config."""

from __future__ import annotations

import os
from unittest.mock import patch


class TestConfigImports:
    """Import/smoke tests for the config module."""

    def test_config_module_is_importable(self) -> None:
        """motto_common.config can be imported."""
        import motto_common.config  # noqa: F401

    def test_config_module_exports_load_config(self) -> None:
        """config module exports load_config."""
        from motto_common.config import load_config

        assert callable(load_config)

    def test_load_config_returns_dict(self) -> None:
        """load_config returns a dict."""
        from motto_common.config import load_config

        config = load_config()
        assert isinstance(config, dict)

    def test_load_config_reads_env_vars(self) -> None:
        """load_config reads environment variables."""
        from motto_common.config import load_config

        with patch.dict(os.environ, {"MOTTO_TEST_VAR": "test-value"}):
            config = load_config(prefix="MOTTO_")
            assert "TEST_VAR" in config
            assert config["TEST_VAR"] == "test-value"

    def test_load_config_default_prefix(self) -> None:
        """load_config uses 'MOTTO_' as default prefix."""
        from motto_common.config import load_config

        with patch.dict(os.environ, {"MOTTO_ENV": "production"}):
            config = load_config()
            assert "ENV" in config
            assert config["ENV"] == "production"

    def test_load_config_filters_non_matching_vars(self) -> None:
        """load_config only returns vars matching the prefix."""
        from motto_common.config import load_config

        with patch.dict(
            os.environ,
            {"MOTTO_KEY": "val1", "OTHER_KEY": "val2", "MOTTO_SECRET": "val3"},
        ):
            config = load_config(prefix="MOTTO_")
            assert "KEY" in config
            assert "SECRET" in config
            assert "OTHER_KEY" not in config

    def test_config_importable_from_package(self) -> None:
        """Config symbols are importable from motto_common."""
        from motto_common import load_config

        assert callable(load_config)
