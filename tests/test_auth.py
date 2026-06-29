"""Tests for motto_common.auth."""

from __future__ import annotations


class TestAuthImports:
    """Import/smoke tests for the auth module."""

    def test_auth_module_is_importable(self) -> None:
        """motto_common.auth can be imported."""
        import motto_common.auth  # noqa: F401

    def test_auth_module_exports_create_auth_headers(self) -> None:
        """auth module exports create_auth_headers."""
        from motto_common.auth import create_auth_headers

        assert callable(create_auth_headers)

    def test_auth_module_exports_validate_token(self) -> None:
        """auth module exports validate_token."""
        from motto_common.auth import validate_token

        assert callable(validate_token)

    def test_create_auth_headers_returns_dict(self) -> None:
        """create_auth_headers returns a dict with Authorization header."""
        from motto_common.auth import create_auth_headers

        headers = create_auth_headers("my-token")
        assert isinstance(headers, dict)
        assert "Authorization" in headers
        assert headers["Authorization"] == "Bearer my-token"

    def test_create_auth_headers_includes_content_type(self) -> None:
        """create_auth_headers includes Content-Type by default."""
        from motto_common.auth import create_auth_headers

        headers = create_auth_headers("token")
        assert headers.get("Content-Type") == "application/json"

    def test_validate_token_rejects_empty(self) -> None:
        """validate_token returns False for empty/None tokens."""
        from motto_common.auth import validate_token

        assert validate_token("") is False
        assert validate_token(None) is False

    def test_validate_token_accepts_non_empty(self) -> None:
        """validate_token returns True for non-empty tokens."""
        from motto_common.auth import validate_token

        assert validate_token("valid-token") is True

    def test_auth_importable_from_package(self) -> None:
        """Auth symbols are importable from motto_common."""
        from motto_common import create_auth_headers, validate_token

        assert callable(create_auth_headers)
        assert callable(validate_token)
