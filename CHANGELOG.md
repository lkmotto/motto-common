# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2025-06-29

### Added

- Initial package structure with `src/motto_common/` layout
- `sentry_init` module: `init_sentry(agent_name)`, `_git_sha`, and `capture_main_loop` with Python 3.12+ `[**P, R]` generics
- `auth` module: shared authentication utilities
- `config` module: shared configuration loading (Doppler, env vars)
- `logging` module: shared logging setup
- Pre-commit hooks: ruff, ruff-format, trailing-whitespace, end-of-file-fixer, check-yaml, check-toml, check-json, debug-statements, check-added-large-files, pytest
- Pytest test suite with coverage reporting
- Ruff linting and mypy type checking (strict mode)
- CI workflow: lint, type check, and test on push/PR

[0.1.0]: https://github.com/lkmotto/motto-common/releases/tag/v0.1.0
