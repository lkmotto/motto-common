# AGENTS.md for motto-common

## Overview
Shared Python utilities for the Motto fleet: Sentry initialisation, authentication, configuration, and logging. This is a foundational package used by all other Motto agents.

## Development

### Setup
```bash
uv sync
```

### Test
```bash
uv run pytest
```

### Lint
```bash
uv run ruff check .
```

### Type Check
```bash
uv run mypy .
```

## Deployment
Published as a Python package for consumption by other Motto fleet repositories. Install with `uv add motto-common` or `pip install motto-common`.
