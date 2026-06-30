#!/usr/bin/env python3
"""Generate pytest tests from Sentry error patterns.

Fetches error events from Sentry for a given project, extracts exception
type, message, and stack trace information, and generates targeted pytest
test files in ``tests/generated/`` that reproduce or characterise each
unique error pattern.

Deduplication: before generating a test, the script scans existing test
files (both under ``tests/`` and ``tests/generated/``) for references to
the exception type.  A local state file (``scripts/.gen_tests_state.json``)
tracks which Sentry event IDs have already been processed so the same
event is never re-generated.

Environment:
    SENTRY_AUTH_TOKEN  – Sentry API auth token (``event:read`` scope)
    SENTRY_ORG         – Sentry organisation slug (e.g. ``lkmotto``)
    SENTRY_PROJECT     – Sentry project slug (e.g. ``motto-common``)
    GEN_TESTS_MIN_COUNT – Minimum event count for an error pattern
                           before generating a test (default ``2``).
    GEN_TESTS_LOOKBACK_HOURS – Sentry stats period in hours (default ``168``,
                               i.e. one week).

Usage (local):
    SENTRY_AUTH_TOKEN=<token> SENTRY_ORG=lkmotto \\
        SENTRY_PROJECT=motto-common python scripts/generate_tests_from_sentry.py

Usage (CI):
    The scheduled workflow sets all required env vars from repository
    secrets automatically.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess  # nosec B404
import sys
import typing
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SENTRY_BASE = "https://sentry.io/api/0"

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATED_DIR = REPO_ROOT / "tests" / "generated"
STATE_FILE = Path(__file__).resolve().parent / ".gen_tests_state.json"

GEN_TESTS_MIN_COUNT = int(os.getenv("GEN_TESTS_MIN_COUNT", "2"))
GEN_TESTS_LOOKBACK_HOURS = int(os.getenv("GEN_TESTS_LOOKBACK_HOURS", "168"))

# Regex patterns for scanning existing tests for already-covered exceptions
_RE_EXCEPTION_REF = re.compile(
    r"(?:pytest\.raises|with\s+raises)\s*\(\s*(\w+(?:\.\w+)*)",
)
_RE_CLASS_DEF = re.compile(r"class\s+(\w+Error\w*|\w+Exception\w*)")

# ---------------------------------------------------------------------------
# Helpers — HTTP
# ---------------------------------------------------------------------------


def _sentry_request(method: str, path: str, data: dict[str, object] | None = None) -> typing.Any:
    """Send an authenticated request to the Sentry API."""
    token = os.getenv("SENTRY_AUTH_TOKEN")
    if not token:
        _fatal("SENTRY_AUTH_TOKEN environment variable is required.")

    url = f"{SENTRY_BASE}{path}"
    body = json.dumps(data).encode("utf-8") if data else None

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method=method,
    )

    try:
        with urllib.request.urlopen(req) as resp:
            return typing.cast(typing.Any, json.loads(resp.read().decode("utf-8")))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        _fatal(f"HTTP {exc.code} on {method} {path}: {error_body}")


# ---------------------------------------------------------------------------
# State file (idempotency)
# ---------------------------------------------------------------------------


def _load_state() -> dict[str, object]:
    """Return tracked event IDs and generated test mappings."""
    if not STATE_FILE.is_file():
        return {"processed_event_ids": [], "generated_tests": {}}
    try:
        with open(STATE_FILE) as fh:
            return typing.cast(dict[str, object], json.load(fh))
    except (json.JSONDecodeError, OSError):
        return {"processed_event_ids": [], "generated_tests": {}}


def _save_state(processed_ids: list[str], generated: dict[str, str]) -> None:
    """Persist processed event IDs and generated test mappings."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as fh:
        json.dump(
            {
                "processed_event_ids": processed_ids,
                "generated_tests": generated,
                "updated": _dt.datetime.now(tz=_dt.UTC).isoformat(),
            },
            fh,
            indent=2,
        )


# ---------------------------------------------------------------------------
# Existing test coverage scanning
# ---------------------------------------------------------------------------


def _scan_existing_tests() -> set[str]:
    """Scan ``tests/`` for exception types already covered by tests.

    Looks for ``pytest.raises(ExceptionType)``, ``with raises(ExceptionType)``,
    and custom exception class definitions to build a set of already-covered
    exception names.
    """
    covered: set[str] = set()
    tests_dir = REPO_ROOT / "tests"
    if not tests_dir.is_dir():
        return covered

    for py_file in tests_dir.rglob("test_*.py"):
        try:
            content = py_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        # Find pytest.raises(ExceptionType)
        for match in _RE_EXCEPTION_REF.finditer(content):
            exc_name = match.group(1).split(".")[-1]
            covered.add(exc_name)

        # Find custom exception class definitions
        for match in _RE_CLASS_DEF.finditer(content):
            covered.add(match.group(1))

    return covered


# ---------------------------------------------------------------------------
# Sentry data fetch
# ---------------------------------------------------------------------------


def _fetch_sentry_issues(org: str, project: str) -> list[dict[str, typing.Any]]:
    """Fetch unresolved Sentry issues with error type metadata."""
    all_issues: list[dict[str, typing.Any]] = []
    stats_period = f"{GEN_TESTS_LOOKBACK_HOURS}h"

    cursor: str | None = None
    while True:
        params: dict[str, str] = {
            "query": "is:unresolved",
            "statsPeriod": stats_period,
            "sort": "freq",
            "limit": "100",
        }
        if cursor:
            params["cursor"] = cursor

        param_str = "&".join(f"{k}={v}" for k, v in params.items())
        result = _sentry_request(
            "GET",
            f"/projects/{org}/{project}/issues/?{param_str}",
        )

        issues: list[dict[str, typing.Any]] = result if isinstance(result, list) else []
        if not issues:
            break

        all_issues.extend(issues)

        if len(issues) < 100:
            break
        cursor = issues[-1].get("id", None)

    return all_issues


def _fetch_latest_event(
    issue_id: str,
) -> dict[str, typing.Any] | None:
    """Fetch the latest event for a Sentry issue to get exception details."""
    try:
        events = _sentry_request(
            "GET",
            f"/issues/{issue_id}/events/?limit=1",
        )
        if isinstance(events, list) and events:
            return typing.cast(dict[str, typing.Any], events[0])
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception:  # noqa: BLE001
        pass
    return None


def _extract_exception_info(
    event: dict[str, typing.Any],
) -> list[dict[str, str]]:
    """Extract exception type, value, and module from a Sentry event.

    Returns a list of exception info dicts (one per chained exception).
    Each dict has keys: ``type``, ``value``, ``module``.
    """
    exceptions: list[dict[str, str]] = []
    entries: list[dict[str, typing.Any]] = event.get("entries", [])
    for entry in entries:
        if entry.get("type") != "exception":
            continue
        data: dict[str, typing.Any] = entry.get("data", {})
        values: list[dict[str, typing.Any]] = data.get("values", [])
        for value in values:
            exc_type = value.get("type", "Exception")
            exc_value = value.get("value", "")
            module = value.get("module", "")

            # Try to get module from top frame if not explicit
            if not module:
                stacktrace = value.get("stacktrace")
                if stacktrace:
                    frames: list[dict[str, typing.Any]] = stacktrace.get("frames", [])
                    if frames:
                        module = frames[0].get("module", "")

            exceptions.append(
                {
                    "type": exc_type,
                    "value": exc_value[:200],  # truncate for test names
                    "module": module,
                }
            )
    return exceptions


# ---------------------------------------------------------------------------
# Test file generation
# ---------------------------------------------------------------------------


def _sanitise_test_name(name: str) -> str:
    """Sanitise a string for use as a test function name."""
    # Keep only alphanumeric, underscores, and hyphens
    safe = re.sub(r"[^\w\-]", "_", name)
    # Collapse multiple underscores
    safe = re.sub(r"_+", "_", safe)
    # Strip leading/trailing underscores
    safe = safe.strip("_")
    if not safe:
        safe = "unknown_error"
    # Ensure it starts with a letter
    if safe[0].isdigit():
        safe = "err_" + safe
    return safe.lower()


def _build_pytest_test(
    exc_type: str,
    exc_value: str,
    module: str,
    issue_title: str,
    event_id: str,
) -> str:
    """Build a pytest test function that documents and reproduces an error.

    The generated test is a *characterisation test*: it documents the
    error pattern observed in Sentry and provides a skeleton that
    developers can fill in with the actual reproduction steps.
    """
    safe_type = _sanitise_test_name(exc_type)
    safe_value = _sanitise_test_name(exc_value)[:50]
    safe_module = _sanitise_test_name(module.replace(".", "_"))[:40]

    test_func_name = f"test_{safe_type}_{safe_value}"[:80]
    if len(test_func_name) < 70:
        if module:
            test_func_name = f"test_{safe_module}_{safe_type}"[:80]

    # Escape the exception value for docstrings
    escaped_value = exc_value.replace('"', '\\"').replace("\n", "\\n")

    lines: list[str] = [
        '"""Auto-generated test from Sentry error pattern.',
        "",
        f"Sentry event:  {event_id}",
        f"Exception:     {exc_type}",
        f"Message:       {escaped_value}",
        f"Module:        {module}",
        f"Issue title:   {issue_title}",
        "",
        "This is a characterisation test generated from a Sentry error pattern.",
        "Replace the placeholder assertions with actual reproduction steps",
        "that exercise the code path causing this error.",
        '"""',
        "",
        "import pytest",
        "",
    ]

    # Add a module-level import if the module is from the motto_common package
    if module.startswith("motto_common."):
        lines.append(f"# Consider importing: {module.rsplit('.', 1)[0]}")
    elif module:
        lines.append(f"# Error originates in module: {module}")

    lines.append("")
    lines.append("")
    lines.append(f"def {test_func_name}() -> None:")
    lines.append(f'    """Reproduce Sentry error: {exc_type}: {escaped_value[:80]}"""')
    lines.append("    # TODO: Add reproduction steps for this error pattern.")
    lines.append("    # The Sentry event data provides clues about the failing code path.")
    lines.append("")
    lines.append(f"    # Observed exception: {exc_type}")
    lines.append(f"    # Observed message:   {escaped_value}")
    if module:
        lines.append(f"    # Source module:      {module}")
    lines.append("")
    lines.append("    # Example pattern:")
    lines.append(f"    # with pytest.raises({exc_type}):")
    lines.append("    #     # call the function that triggers this error")
    lines.append("    #     pass")
    lines.append("")
    lines.append("    # For now, document the error is tracked (characterisation test):")
    lines.append(f"    assert isinstance({exc_type}, type)")
    lines.append(f"    assert issubclass({exc_type}, Exception)")

    return "\n".join(lines) + "\n"


def _generate_test_file(
    test_content: str,
    exc_type: str,
    module: str,
) -> Path:
    """Write a generated test to ``tests/generated/``.

    Returns the path to the created file.
    """
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)

    safe_module = _sanitise_test_name(module.replace(".", "_"))[:40]
    safe_type = _sanitise_test_name(exc_type)
    filename = f"test_generated_{safe_module}_{safe_type}.py"[:100]
    if len(filename) < 90:
        filename = f"test_generated_{safe_type}.py"

    # Ensure the filename is valid
    filename = re.sub(r"[^\w\-\.]", "_", filename)
    if not filename.endswith(".py"):
        filename += ".py"

    filepath = GENERATED_DIR / filename

    header = (
        f"# Auto-generated by scripts/generate_tests_from_sentry.py\n"
        f"# Generated at: {_dt.datetime.now(tz=_dt.UTC).isoformat()}\n"
        f"# Source: Sentry error pattern for {exc_type}\n"
        f"\n"
    )

    filepath.write_text(header + test_content, encoding="utf-8")
    return filepath


# ---------------------------------------------------------------------------
# Coverage gap analysis (used by both scripts)
# ---------------------------------------------------------------------------


def _run_coverage_check() -> dict[str, typing.Any]:
    """Run pytest --cov and parse coverage data.

    Returns a dict with ``pct`` (float), ``missing_lines`` (dict), and
    ``total_statements`` (int).
    """
    result: dict[str, typing.Any] = {
        "pct": 0.0,
        "missing_lines": {},
        "total_statements": 0,
    }

    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--cov=src/motto_common",
                "--cov-report=json",
                "--cov-report=term-missing",
                "-q",
                "--no-header",
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError):
        return result

    # Parse JSON coverage report if it exists
    coverage_json = REPO_ROOT / "coverage.json"
    if coverage_json.is_file():
        try:
            cov_data = json.loads(coverage_json.read_text(encoding="utf-8"))
            totals = cov_data.get("totals", {})
            result["pct"] = totals.get("percent_covered", 0.0)
            result["total_statements"] = totals.get("num_statements", 0)

            # Extract per-file missing lines
            files_data: dict[str, typing.Any] = cov_data.get("files", {})
            for filepath, file_info in files_data.items():
                missing = file_info.get("missing_lines", [])
                if missing:
                    result["missing_lines"][filepath] = missing
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    return result


def _map_exceptions_to_modules(
    issues: list[dict[str, typing.Any]], org: str, project: str
) -> dict[str, list[dict[str, typing.Any]]]:
    """Map exception types to the list of issues that produce them.

    Returns a dict keyed by exception type (str), with values being
    lists of issue dicts.
    """
    exc_map: dict[str, list[dict[str, typing.Any]]] = {}

    for issue in issues:
        issue_id = issue.get("id", "")
        if not issue_id:
            continue
        event = _fetch_latest_event(issue_id)
        if event is None:
            continue
        exc_infos = _extract_exception_info(event)
        for exc_info in exc_infos:
            exc_type = exc_info["type"]
            if exc_type not in exc_map:
                exc_map[exc_type] = []
            entry = dict(issue)
            entry["_exc_info"] = exc_info
            exc_map[exc_type].append(entry)

    return exc_map


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _fatal(msg: str) -> typing.NoReturn:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    org = os.getenv("SENTRY_ORG")
    project = os.getenv("SENTRY_PROJECT")

    if not org or not project:
        _fatal("SENTRY_ORG and SENTRY_PROJECT environment variables are required.")

    # 1. Fetch Sentry issues -------------------------------------------------
    print(f"Fetching unresolved Sentry issues for {org}/{project} ...")
    sentry_issues = _fetch_sentry_issues(org, project)
    print(f"  Found {len(sentry_issues)} unresolved issues.")

    if not sentry_issues:
        print("No issues found. Nothing to generate.")
        return

    # 2. Scan existing tests for already-covered exceptions -------------------
    existing_coverage = _scan_existing_tests()
    print(f"  Found {len(existing_coverage)} exception types already in tests.")

    # 3. Load processed state -------------------------------------------------
    state = _load_state()
    processed_ids: set[str] = set(typing.cast(list[str], state.get("processed_event_ids", [])))
    generated_tests: dict[str, str] = dict(
        typing.cast(dict[str, str], state.get("generated_tests", {}))
    )

    # 4. Process each issue ---------------------------------------------------
    generated_count = 0
    skipped_covered = 0
    skipped_processed = 0
    skipped_no_event = 0
    skipped_below_threshold = 0

    for issue in sentry_issues:
        issue_id: str = issue.get("id", "")
        count_str: str = issue.get("count", "0")
        try:
            count = int(count_str)
        except (ValueError, TypeError):
            count = 0

        if count < GEN_TESTS_MIN_COUNT:
            skipped_below_threshold += 1
            continue

        if issue_id in processed_ids:
            skipped_processed += 1
            continue

        event = _fetch_latest_event(issue_id)
        if event is None:
            skipped_no_event += 1
            processed_ids.add(issue_id)
            continue

        exc_infos = _extract_exception_info(event)
        if not exc_infos:
            skipped_no_event += 1
            processed_ids.add(issue_id)
            continue

        issue_title = str(issue.get("title", "Unknown error"))

        for exc_info in exc_infos:
            exc_type = exc_info["type"]

            # Skip if already covered by existing tests
            if exc_type in existing_coverage:
                skipped_covered += 1
                continue

            # Skip built-in exceptions that are always covered implicitly
            if exc_type in {
                "Exception",
                "BaseException",
                "ValueError",
                "TypeError",
                "KeyError",
                "IndexError",
                "AttributeError",
                "RuntimeError",
                "OSError",
                "IOError",
                "ImportError",
                "ModuleNotFoundError",
            }:
                skipped_covered += 1
                continue

            # Generate the test
            module = exc_info.get("module", "")
            test_content = _build_pytest_test(
                exc_type,
                exc_info["value"],
                module,
                issue_title,
                event.get("eventID", issue_id),
            )

            filepath = _generate_test_file(test_content, exc_type, module)
            generated_tests[issue_id] = str(filepath.relative_to(REPO_ROOT))
            existing_coverage.add(exc_type)  # mark as covered for this run
            generated_count += 1

            print(
                f"  Generated test for {exc_type} (from '{issue_title[:60]}...') -> {filepath.name}"
            )
            break  # one test per issue

        processed_ids.add(issue_id)

    # 5. Save state -----------------------------------------------------------
    _save_state(sorted(processed_ids), generated_tests)

    print(
        f"\nDone: {generated_count} tests generated, "
        f"{skipped_covered} already covered, "
        f"{skipped_processed} already processed, "
        f"{skipped_below_threshold} below threshold, "
        f"{skipped_no_event} had no extractable event."
    )

    # Ensure __init__.py exists in generated dir
    init_file = GENERATED_DIR / "__init__.py"
    if not init_file.exists():
        init_file.write_text(
            "# Auto-generated test package for Sentry error patterns.\n",
            encoding="utf-8",
        )
        print("  Created tests/generated/__init__.py")


if __name__ == "__main__":
    main()
