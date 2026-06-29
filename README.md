# motto-common

Shared Python utilities for the Motto fleet: Sentry initialisation, authentication, configuration, and logging.

## Installation

```bash
uv add motto-common
# or
pip install motto-common
```

## Usage

### Sentry initialisation

```python
from motto_common.sentry_init import init_sentry, capture_main_loop, _git_sha

# Explicit init with agent name
init_sentry("my-service")

# Or auto-init via env var (MOTTO_AGENT_NAME)
import motto_common.sentry_init  # auto-initialises when SENTRY_DSN is set

# Decorate a main loop
@capture_main_loop
def main() -> None:
    ...
```

### Auth

```python
from motto_common.auth import create_auth_headers, validate_token

headers = create_auth_headers("my-api-token")
# {"Authorization": "Bearer my-api-token", "Content-Type": "application/json"}

if validate_token(token):
    ...
```

### Config

```python
from motto_common.config import load_config

config = load_config("MOTTO_")
# Reads all MOTTO_* env vars into a dict
```

### Logging

```python
from motto_common.logging import setup_logging

logger = setup_logging("my-service", json_fmt=True)
logger.info("Service started")
```

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy .
```

## License

MIT
