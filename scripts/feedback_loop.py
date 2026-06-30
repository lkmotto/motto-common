#!/usr/bin/env python3
"""Feedback loop automation: Sentry frequency + coverage gap → GitHub issues.

Queries Sentry for the most frequent unresolved errors, cross-references
them with the current test-coverage report (pytest-cov), and automatically
files GitHub issues labeled ``sentry-feedback`` and ``improvement`` for
high-priority error patterns that lack adequate test coverage.

Each filed issue includes:
- Sentry frequency data (event count, affected users, first/last seen)
- Coverage gap analysis (which files/modules have missing lines near the
  error's source)
- Concrete improvement suggestions (add test, increase coverage, add guard)

Deduplication: queries existing open issues with the ``sentry-feedback``
label and skips error types already filed.  A local state file
(``scripts/.feedback_state.json``) provides additional idempotency.

Environment:
    SENTRY_AUTH_TOKEN   – Sentry API auth token (``event:read`` scope)
    SENTRY_ORG          – Sentry organisation slug (e.g. ``lkmotto``)
    SENTRY_PROJECT      – Sentry project slug (e.g. ``motto-common``)
    GITHUB_TOKEN        – GitHub PAT with ``issues:write`` on the target repo
    GITHUB_REPOSITORY   – GitHub owner/repo slug (set automatically by CI)
    FEEDBACK_MIN_EVENTS  – Min event count before filing (default ``5``)
    FEEDBACK_MIN_USERS   – Min affected users before filing (default ``1``)
    FEEDBACK_LOOKBACK_HOURS – Sentry stats period in hours (default ``168``)
    FEEDBACK_COVERAGE_GAP_THRESHOLD – Coverage pct below which a gap is
                                      reported (default ``80``)

Usage (local):
    SENTRY_AUTH_TOKEN=<token> SENTRY_ORG=lkmotto \\
        SENTRY_PROJECT=motto-common GITHUB_TOKEN=<gh_token> \\
        GITHUB_REPOSITORY=lkmotto/motto-common \\
        python scripts/feedback_loop.py

Usage (CI):
    The scheduled workflow sets all required env vars from repository
    secrets automatically.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
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
STATE_FILE = Path(__file__).resolve().parent / ".feedback_state.json"

FEEDBACK_MIN_EVENTS = int(os.getenv("FEEDBACK_MIN_EVENTS", "5"))
FEEDBACK_MIN_USERS = int(os.getenv("FEEDBACK_MIN_USERS", "1"))
FEEDBACK_LOOKBACK_HOURS = int(os.getenv("FEEDBACK_LOOKBACK_HOURS", "168"))
FEEDBACK_COVERAGE_GAP_THRESHOLD = int(os.getenv("FEEDBACK_COVERAGE_GAP_THRESHOLD", "80"))

FEEDBACK_LABEL = "sentry-feedback"
IMPROVEMENT_LABEL = "improvement"


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


def _github_request(method: str, path: str, data: dict[str, object] | None = None) -> typing.Any:
    """Send an authenticated request to the GitHub API."""
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        _fatal("GITHUB_TOKEN environment variable is required.")

    url = f"https://api.github.com{path}"
    body = json.dumps(data).encode("utf-8") if data else None

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method=method,
    )

    try:
        with urllib.request.urlopen(req) as resp:
            return typing.cast(typing.Any, json.loads(resp.read().decode("utf-8")))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        _fatal(f"GitHub HTTP {exc.code} on {method} {path}: {error_body}")


# ---------------------------------------------------------------------------
# State file (idempotency)
# ---------------------------------------------------------------------------


def _load_state() -> dict[str, object]:
    """Return tracked feedback issue IDs."""
    if not STATE_FILE.is_file():
        return {"filed_issues": {}, "last_run": None}
    try:
        with open(STATE_FILE) as fh:
            return typing.cast(dict[str, object], json.load(fh))
    except (json.JSONDecodeError, OSError):
        return {"filed_issues": {}, "last_run": None}


def _save_state(filed_issues: dict[str, str]) -> None:
    """Persist filed issue mappings (sentry_issue_id → github_issue_url)."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as fh:
        json.dump(
            {
                "filed_issues": filed_issues,
                "last_run": _dt.datetime.now(tz=_dt.UTC).isoformat(),
            },
            fh,
            indent=2,
        )


# ---------------------------------------------------------------------------
# Existing GitHub issue lookup
# ---------------------------------------------------------------------------


def _list_existing_feedback_titles() -> set[str]:
    """Return the set of titles (lowercased) for open issues with feedback labels."""
    repo = os.getenv("GITHUB_REPOSITORY", "")
    if not repo:
        _fatal("GITHUB_REPOSITORY environment variable is required.")

    titles: set[str] = set()
    page = 1
    while True:
        issues = _github_request(
            "GET",
            f"/repos/{repo}/issues?labels={FEEDBACK_LABEL}&state=open&per_page=100&page={page}",
        )
        if not isinstance(issues, list) or not issues:
            break
        for issue in issues:
            title = issue.get("title", "")
            if isinstance(title, str):
                titles.add(title.lower())
        page += 1
    return titles


# ---------------------------------------------------------------------------
# Sentry data fetch
# ---------------------------------------------------------------------------


def _fetch_sentry_issues(org: str, project: str) -> list[dict[str, typing.Any]]:
    """Fetch unresolved Sentry issues sorted by frequency."""
    all_issues: list[dict[str, typing.Any]] = []
    stats_period = f"{FEEDBACK_LOOKBACK_HOURS}h"

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


def _fetch_latest_event(issue_id: str) -> dict[str, typing.Any] | None:
    """Fetch the latest event for a Sentry issue."""
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


def _extract_source_module(event: dict[str, typing.Any]) -> str:
    """Extract the most likely source module from a Sentry event."""
    entries: list[dict[str, typing.Any]] = event.get("entries", [])
    for entry in entries:
        if entry.get("type") != "exception":
            continue
        data: dict[str, typing.Any] = entry.get("data", {})
        values: list[dict[str, typing.Any]] = data.get("values", [])
        for value in values:
            stacktrace = value.get("stacktrace")
            if stacktrace:
                frames: list[dict[str, typing.Any]] = stacktrace.get("frames", [])
                if frames:
                    # Find the first frame from the project's own code
                    for frame in frames:
                        module = str(frame.get("module", ""))
                        if module.startswith("motto_common."):
                            return module
                    # Fallback to first frame
                    return str(frames[0].get("module", "unknown"))
            module = str(value.get("module", ""))
            if module:
                return module
    return "unknown"


# ---------------------------------------------------------------------------
# Coverage analysis
# ---------------------------------------------------------------------------


def _run_coverage_analysis() -> dict[str, typing.Any]:
    """Run pytest --cov and return coverage metrics.

    Returns a dict with:
    - ``overall_pct``: float (overall coverage percentage)
    - ``file_coverage``: dict[str, float] (per-file coverage percentages)
    - ``missing_by_file``: dict[str, list[int]] (missing line numbers)
    - ``below_threshold``: list[str] (files below threshold)
    """
    result: dict[str, typing.Any] = {
        "overall_pct": 0.0,
        "file_coverage": {},
        "missing_by_file": {},
        "below_threshold": [],
    }

    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--cov=src/motto_common",
                "--cov-report=json",
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

    coverage_json = REPO_ROOT / "coverage.json"
    if not coverage_json.is_file():
        return result

    try:
        cov_data = json.loads(coverage_json.read_text(encoding="utf-8"))
        totals = cov_data.get("totals", {})
        result["overall_pct"] = totals.get("percent_covered", 0.0)

        files_data: dict[str, typing.Any] = cov_data.get("files", {})
        for filepath, file_info in files_data.items():
            summary = file_info.get("summary", {})
            pct = summary.get("percent_covered", 0.0)
            result["file_coverage"][filepath] = pct

            missing = file_info.get("missing_lines", [])
            if missing:
                result["missing_by_file"][filepath] = missing

            if pct < FEEDBACK_COVERAGE_GAP_THRESHOLD:
                result["below_threshold"].append(filepath)
    except (json.JSONDecodeError, KeyError, TypeError):
        pass

    return result


# ---------------------------------------------------------------------------
# Gap analysis
# ---------------------------------------------------------------------------


def _analyse_coverage_gap(
    source_module: str,
    coverage_data: dict[str, typing.Any],
) -> dict[str, typing.Any]:
    """Determine the coverage gap for a given source module.

    Returns a dict with ``has_gap`` (bool), ``file_pct`` (float),
    ``missing_lines`` (list[int]), and ``files_below_threshold`` (list[str]).
    """
    gap: dict[str, typing.Any] = {
        "has_gap": False,
        "file_pct": 100.0,
        "missing_lines": [],
        "files_below_threshold": [],
    }

    file_coverage: dict[str, float] = coverage_data.get("file_coverage", {})
    missing_by_file: dict[str, list[int]] = coverage_data.get("missing_by_file", {})

    # Convert module path to file path
    module_path = source_module.replace(".", "/") + ".py"

    # Find matching coverage file
    matched_file = None
    for cov_file in file_coverage:
        if cov_file.endswith(module_path) or module_path in cov_file:
            matched_file = cov_file
            break

    if matched_file:
        gap["file_pct"] = file_coverage.get(matched_file, 100.0)
        gap["missing_lines"] = missing_by_file.get(matched_file, [])
        if gap["file_pct"] < FEEDBACK_COVERAGE_GAP_THRESHOLD:
            gap["has_gap"] = True

    # Also check global below-threshold files
    below: list[str] = coverage_data.get("below_threshold", [])
    gap["files_below_threshold"] = below

    # If overall coverage is below threshold, flag as gap
    overall_pct: float = coverage_data.get("overall_pct", 0.0)
    if overall_pct < FEEDBACK_COVERAGE_GAP_THRESHOLD:
        gap["has_gap"] = True

    return gap


# ---------------------------------------------------------------------------
# Issue body builder
# ---------------------------------------------------------------------------


def _build_feedback_issue_body(
    issue: dict[str, typing.Any],
    org: str,
    project: str,
    coverage_data: dict[str, typing.Any],
    gap: dict[str, typing.Any],
    source_module: str,
) -> str:
    """Build a comprehensive markdown issue body for a feedback issue."""
    count: str = issue.get("count", "?")
    user_count: int = issue.get("userCount", 0)
    first_seen: str = issue.get("firstSeen", "unknown")
    last_seen: str = issue.get("lastSeen", "unknown")
    culprit: str = issue.get("culprit", "unknown")
    level: str = issue.get("level", "unknown")
    issue_id: str = issue.get("id", "")
    short_id: str = issue.get("shortId", issue_id[:12] if issue_id else "?")
    title: str = issue.get("title", "Unknown error")

    sentry_url = f"https://sentry.io/organizations/{org}/issues/{issue_id}/"

    # Truncate very long fields
    if len(culprit) > 140:
        culprit = "..." + culprit[-137:]

    overall_pct: float = coverage_data.get("overall_pct", 0.0)
    file_pct: float = gap.get("file_pct", 100.0)
    missing_lines: list[int] = gap.get("missing_lines", [])
    below_threshold: list[str] = gap.get("files_below_threshold", [])

    # Build suggestions
    suggestions: list[str] = []
    if gap.get("has_gap"):
        if file_pct < FEEDBACK_COVERAGE_GAP_THRESHOLD:
            suggestions.append(
                f"- **Increase test coverage** for `{source_module}` "
                f"(currently {file_pct:.1f}%, target ≥{FEEDBACK_COVERAGE_GAP_THRESHOLD}%)."
            )
        if missing_lines:
            line_list = ", ".join(str(ln) for ln in missing_lines[:10])
            suggestions.append(f"- **Add tests** covering uncovered lines: {line_list}.")
        if overall_pct < FEEDBACK_COVERAGE_GAP_THRESHOLD:
            suggestions.append(
                f"- **Improve overall coverage** "
                f"(currently {overall_pct:.1f}%, target ≥{FEEDBACK_COVERAGE_GAP_THRESHOLD}%)."
            )
    else:
        suggestions.append(
            "- **Add characterisation test** for this error pattern to prevent silent regressions."
        )

    if count not in ("?", "0"):
        suggestions.append(
            f"- **Add Sentry alert rule** for this error type "
            f"({count} events in the lookback window)."
        )

    suggestions.append(
        "- **Run `scripts/generate_tests_from_sentry.py`** "
        "to auto-generate a test skeleton for this error."
    )

    lines: list[str] = [
        "> Auto-filed by `scripts/feedback_loop.py` — feedback loop automation.",
        "",
        "## Sentry Error Frequency Data",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| **Sentry ID** | `{short_id}` |",
        f"| **Error title** | {title} |",
        f"| **Level** | `{level}` |",
        f"| **Event count** | {count} |",
        f"| **Affected users** | {user_count} |",
        f"| **First seen** | {first_seen} |",
        f"| **Last seen** | {last_seen} |",
        f"| **Culprit** | `{culprit}` |",
        f"| **Source module** | `{source_module}` |",
        f"| **Sentry link** | [{short_id}]({sentry_url}) |",
        "",
        "## Coverage Gap Analysis",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| **Overall coverage** | {overall_pct:.1f}% |",
        f"| **File coverage** (`{source_module}`) | {file_pct:.1f}% |",
        f"| **Coverage threshold** | {FEEDBACK_COVERAGE_GAP_THRESHOLD}% |",
        f"| **Gap detected** | {'Yes' if gap.get('has_gap') else 'No'} |",
    ]

    if missing_lines:
        line_list = ", ".join(str(ln) for ln in missing_lines[:20])
        lines.append(f"| **Missing lines** | {line_list} |")

    lines.append("")

    if below_threshold:
        lines.append("### Files Below Coverage Threshold")
        lines.append("")
        for f in below_threshold[:10]:
            pct_val: float = coverage_data.get("file_coverage", {}).get(f, 0.0)
            lines.append(f"- `{f}` — {pct_val:.1f}%")
        lines.append("")

    lines.append("## Improvement Suggestions")
    lines.append("")
    for suggestion in suggestions:
        lines.append(suggestion)
    lines.append("")

    lines.extend(
        [
            "## Metadata",
            "",
            f"- **Source**: Sentry project `{org}/{project}`",
            f"- **Filed at**: {_dt.datetime.now(tz=_dt.UTC).isoformat()}",
            f"- **Minimum event threshold**: {FEEDBACK_MIN_EVENTS}",
            f"- **Minimum user threshold**: {FEEDBACK_MIN_USERS}",
            f"- **Lookback window**: {FEEDBACK_LOOKBACK_HOURS}h",
            f"- **Coverage gap threshold**: {FEEDBACK_COVERAGE_GAP_THRESHOLD}%",
        ]
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# GitHub issue creation
# ---------------------------------------------------------------------------


def _file_github_issue(title: str, body: str) -> dict[str, typing.Any]:
    """Create a labelled GitHub issue and return the response."""
    repo = os.getenv("GITHUB_REPOSITORY", "")
    if not repo:
        _fatal("GITHUB_REPOSITORY is required.")

    payload: dict[str, object] = {
        "title": title,
        "body": body,
        "labels": [FEEDBACK_LABEL, IMPROVEMENT_LABEL],
    }
    return typing.cast(
        dict[str, typing.Any],
        _github_request("POST", f"/repos/{repo}/issues", payload),
    )


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
        print("No issues found. Nothing to analyse.")
        return

    # 2. Run coverage analysis ------------------------------------------------
    print("Running coverage analysis ...")
    coverage_data = _run_coverage_analysis()
    overall_pct: float = coverage_data.get("overall_pct", 0.0)
    below_threshold: list[str] = coverage_data.get("below_threshold", [])
    print(f"  Overall coverage: {overall_pct:.1f}%")
    print(f"  Files below {FEEDBACK_COVERAGE_GAP_THRESHOLD}% threshold: {len(below_threshold)}")

    # 3. Load state and existing issues --------------------------------------
    state = _load_state()
    filed_map: dict[str, str] = dict(typing.cast(dict[str, str], state.get("filed_issues", {})))
    existing_titles = _list_existing_feedback_titles()

    # 4. Process high-frequency issues ----------------------------------------
    filed_count = 0
    skipped_below_threshold = 0
    skipped_already_filed = 0
    skipped_existing = 0
    skipped_no_gap = 0

    # Sort by frequency (highest first)
    sorted_issues = sorted(
        sentry_issues,
        key=lambda i: int(i.get("count", "0")),
        reverse=True,
    )

    for issue in sorted_issues:
        issue_id: str = issue.get("id", "")
        count_str: str = issue.get("count", "0")
        try:
            count = int(count_str)
        except (ValueError, TypeError):
            count = 0

        user_count = issue.get("userCount", 0)
        if isinstance(user_count, str):
            try:
                user_count = int(user_count)
            except (ValueError, TypeError):
                user_count = 0

        # Skip below thresholds
        if count < FEEDBACK_MIN_EVENTS:
            skipped_below_threshold += 1
            continue

        if user_count < FEEDBACK_MIN_USERS:
            skipped_below_threshold += 1
            continue

        # Skip if already filed
        if issue_id in filed_map:
            skipped_already_filed += 1
            continue

        # Build a title and check against existing GitHub issues
        issue_title = str(issue.get("title", "Unknown error"))
        gh_title = f"[sentry-feedback] {issue_title}"

        if gh_title.lower() in existing_titles:
            skipped_existing += 1
            filed_map[issue_id] = "existing"
            continue

        # Get event data for source module extraction
        event = _fetch_latest_event(issue_id)
        source_module = "unknown"
        if event:
            source_module = _extract_source_module(event)

        # Analyse coverage gap for the source module
        gap = _analyse_coverage_gap(source_module, coverage_data)

        # Only file if there's a coverage gap or low overall coverage
        if not gap.get("has_gap") and overall_pct >= FEEDBACK_COVERAGE_GAP_THRESHOLD:
            skipped_no_gap += 1
            continue

        # Build and file the issue
        body = _build_feedback_issue_body(issue, org, project, coverage_data, gap, source_module)
        print(f"  Filing feedback issue: {gh_title}")

        try:
            gh_response = _file_github_issue(gh_title, body)
            gh_url = str(gh_response.get("html_url", ""))
            filed_map[issue_id] = gh_url
            filed_count += 1
            existing_titles.add(gh_title.lower())
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"  WARNING: Failed to file issue for {gh_title}: {exc}")
            continue

    # 5. File a global coverage gap issue if overall coverage is below threshold
    if overall_pct < FEEDBACK_COVERAGE_GAP_THRESHOLD and below_threshold:
        global_title = (
            f"[sentry-feedback] Overall coverage {overall_pct:.1f}% "
            f"below {FEEDBACK_COVERAGE_GAP_THRESHOLD}% threshold"
        )
        if global_title.lower() not in existing_titles:
            global_body_lines: list[str] = [
                "> Auto-filed by `scripts/feedback_loop.py` — feedback loop automation.",
                "",
                "## Overall Coverage Gap",
                "",
                f"Overall test coverage is **{overall_pct:.1f}%**, "
                f"below the {FEEDBACK_COVERAGE_GAP_THRESHOLD}% threshold.",
                "",
                "### Files Below Threshold",
                "",
            ]
            for f in below_threshold[:20]:
                pct_val: float = coverage_data.get("file_coverage", {}).get(f, 0.0)
                global_body_lines.append(f"- `{f}` — {pct_val:.1f}%")

            global_body_lines.extend(
                [
                    "",
                    "## Improvement Suggestions",
                    "",
                    "- **Add tests** for the files listed above to raise "
                    "overall coverage above the threshold.",
                    "- **Prioritise** files with the highest Sentry error "
                    "frequency and lowest coverage.",
                    "- **Run `scripts/generate_tests_from_sentry.py`** "
                    "to auto-generate test skeletons from Sentry error patterns.",
                    "",
                    "## Metadata",
                    "",
                    f"- **Filed at**: {_dt.datetime.now(tz=_dt.UTC).isoformat()}",
                    f"- **Coverage gap threshold**: {FEEDBACK_COVERAGE_GAP_THRESHOLD}%",
                ]
            )

            print(f"  Filing global coverage gap issue: {global_title}")
            try:
                _file_github_issue(global_title, "\n".join(global_body_lines))
                filed_count += 1
            except SystemExit:
                raise
            except Exception as exc:  # noqa: BLE001
                print(f"  WARNING: Failed to file global coverage issue: {exc}")

    # 6. Save state -----------------------------------------------------------
    _save_state(filed_map)

    print(
        f"\nDone: {filed_count} issues filed, "
        f"{skipped_below_threshold} below thresholds, "
        f"{skipped_already_filed} already in state, "
        f"{skipped_existing} already on GitHub, "
        f"{skipped_no_gap} with sufficient coverage."
    )


if __name__ == "__main__":
    main()
