#!/usr/bin/env python3
"""Automated Root Cause Analysis: Sentry-to-GitHub issue auto-filing.

Fetches new Sentry error issues (grouped by error type) and automatically
files a GitHub issue for each *new* error type not yet tracked, including
rich metadata (title, error count, first/last seen, event sample, stack
trace excerpt).

After filing, the script records the Sentry issue ID in a local state file
(``scripts/.rca_state.json``) so that it never re-files the same error type
twice.  The state file is committed back to the repo for durability across
CI runs, but the workflow also queries existing GitHub issues whose title
matches the Sentry issue title as a safety net when the state file is
missing.

Environment:
    SENTRY_AUTH_TOKEN  – Sentry API auth token (``event:read`` scope)
    SENTRY_ORG         – Sentry organisation slug (e.g. ``lkmotto``)
    SENTRY_PROJECT     – Sentry project slug (e.g. ``motto-common``)
    GITHUB_TOKEN       – GitHub PAT with ``issues:write`` on the target repo
    GITHUB_REPOSITORY  – GitHub owner/repo slug (set automatically by CI)
    RCA_ISSUE_LABEL    – Label(s) applied to auto-filed issues
                          (default ``sentry-rca``)
    RCA_MIN_EVENTS     – Minimum event count before filing (default ``3``)
    RCA_LOOKBACK_HOURS – How many hours of Sentry data to examine
                          (default ``24``)

Usage (local):
    SENTRY_AUTH_TOKEN=<token> SENTRY_ORG=lkmotto \\
        SENTRY_PROJECT=motto-common GITHUB_TOKEN=<gh_token> \\
        GITHUB_REPOSITORY=lkmotto/motto-common python scripts/auto_rca.py

Usage (CI, ``.github/workflows/rca.yml``):
    The scheduled workflow sets all required env vars from repository
    secrets automatically.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import typing
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SENTRY_BASE = "https://sentry.io/api/0"

STATE_FILE = os.path.join(os.path.dirname(__file__), ".rca_state.json")

RCA_ISSUE_LABEL = os.getenv("RCA_ISSUE_LABEL", "sentry-rca")
RCA_MIN_EVENTS = int(os.getenv("RCA_MIN_EVENTS", "3"))
RCA_LOOKBACK_HOURS = int(os.getenv("RCA_LOOKBACK_HOURS", "24"))


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


def _load_state() -> set[str]:
    """Return the set of Sentry issue IDs already tracked."""
    if not os.path.isfile(STATE_FILE):
        return set()
    try:
        with open(STATE_FILE) as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return set()
    return set(data.get("tracked_issues", []))


def _save_state(tracked_issues: set[str]) -> None:
    """Persist the set of tracked Sentry issue IDs to the state file."""
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as fh:
        now_iso = _dt.datetime.now(tz=_dt.UTC).isoformat()
        json.dump(
            {"tracked_issues": sorted(tracked_issues), "updated": now_iso}, fh, indent=2
        )


def _list_existing_github_issue_titles() -> set[str]:
    """Return the set of titles (lowercased) for open issues with the RCA label."""
    repo = os.getenv("GITHUB_REPOSITORY", "")
    if not repo:
        _fatal("GITHUB_REPOSITORY environment variable is required.")

    titles: set[str] = set()
    page = 1
    while True:
        issues = _github_request(
            "GET",
            f"/repos/{repo}/issues"
            f"?labels={RCA_ISSUE_LABEL}&state=open&per_page=100&page={page}",
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
    """Fetch unresolved Sentry issues with sufficient event count.

    Returns the list of issue dicts from the Sentry Issues API, paginated.
    """
    all_issues: list[dict[str, typing.Any]] = []
    cursor: str | None = None
    stats_period = f"{RCA_LOOKBACK_HOURS}h"

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

        # Sentry returns a JSON array for paginated results
        issues: list[dict[str, typing.Any]] = (
            result if isinstance(result, list) else result.get("issues", result)
        )
        if not isinstance(issues, list) or not issues:
            break

        all_issues.extend(issues)

        # Check for a Link header via a second request with cursor-based pagination.
        # The Sentry API also supports cursor in the response body.
        # Fallback: if we got fewer than 100 results, we've reached the end.
        if len(issues) < 100:
            break
        cursor = issues[-1].get("id", None)

    return all_issues


# ---------------------------------------------------------------------------
# GitHub issue creation
# ---------------------------------------------------------------------------


def _build_issue_body(issue: dict[str, typing.Any], org: str, project: str) -> str:
    """Build a markdown body for the GitHub issue from a Sentry issue dict."""
    count: str = issue.get("count", "?")
    user_count: int = issue.get("userCount", 0)
    first_seen: str = issue.get("firstSeen", "unknown")
    last_seen: str = issue.get("lastSeen", "unknown")
    culprit: str = issue.get("culprit", "unknown")
    level: str = issue.get("level", "unknown")
    issue_id: str = issue.get("id", "")
    short_id: str = issue.get("shortId", issue_id[:12] if issue_id else "?")

    # Try to extract a sample stack trace from the latest event
    stack_trace = _extract_stack_trace(issue, org, project)

    sentry_url = f"https://sentry.io/organizations/{org}/issues/{issue_id}/"

    # Truncate very long culprit paths
    if len(culprit) > 140:
        culprit = "..." + culprit[-137:]

    lines: list[str] = [
        "> Auto-filed by `scripts/auto_rca.py` — automated root cause analysis.",
        "",
        "## Sentry Issue Details",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| **Sentry ID** | `{short_id}` |",
        f"| **Level** | `{level}` |",
        f"| **Event count** | {count} |",
        f"| **Affected users** | {user_count} |",
        f"| **First seen** | {first_seen} |",
        f"| **Last seen** | {last_seen} |",
        f"| **Culprit** | `{culprit}` |",
        f"| **Sentry link** | [{short_id}]({sentry_url}) |",
        "",
    ]

    if stack_trace:
        lines.extend(
            [
                "## Sample Stack Trace",
                "",
                "```",
                stack_trace.strip(),
                "```",
                "",
            ]
        )

    lines.extend(
        [
            "## Metadata",
            "",
            f"- **Source**: Sentry project `{org}/{project}`",
            f"- **Filed at**: {_dt.datetime.now(tz=_dt.UTC).isoformat()}",
            f"- **Minimum event threshold**: {RCA_MIN_EVENTS}",
            f"- **Lookback window**: {RCA_LOOKBACK_HOURS}h",
        ]
    )

    return "\n".join(lines)


def _extract_stack_trace(
    issue: dict[str, typing.Any], org: str, project: str
) -> str:
    """Try to pull a stack trace excerpt from the issue's latest event."""
    issue_id = issue.get("id", "")
    if not issue_id:
        return ""

    try:
        events = _sentry_request(
            "GET",
            f"/issues/{issue_id}/events/?limit=1",
        )
        if not isinstance(events, list) or not events:
            return ""
        event = events[0]
    except Exception:  # noqa: BLE001  — best-effort, never block filing
        return ""

    # Walk known locations for stack trace content
    entries: list[dict[str, typing.Any]] = event.get("entries", [])
    for entry in entries:
        if entry.get("type") != "exception":
            continue
        data: dict[str, typing.Any] = entry.get("data", {})
        values: list[dict[str, typing.Any]] = data.get("values", [])
        for value in values:
            stacktrace: dict[str, typing.Any] | None = value.get("stacktrace")
            if not stacktrace:
                continue
            frames: list[dict[str, typing.Any]] = stacktrace.get("frames", [])
            if not frames:
                continue
            # Build a compact stack excerpt (last 10 frames)
            lines: list[str] = []
            for frame in frames[-10:]:
                func = frame.get("function", "?")
                filename = frame.get("filename", "?")
                lineno = frame.get("lineNo", "?")
                lines.append(f"  {filename}:{lineno} in {func}")
            return "\n".join(lines)

    return ""


def _file_github_issue(title: str, body: str) -> dict[str, typing.Any]:
    """Create a labelled GitHub issue and return the response."""
    repo = os.getenv("GITHUB_REPOSITORY", "")
    if not repo:
        _fatal("GITHUB_REPOSITORY is required.")

    payload: dict[str, object] = {
        "title": title,
        "body": body,
        "labels": [RCA_ISSUE_LABEL],
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

    print(f"Fetching unresolved Sentry issues for {org}/{project} ...")
    sentry_issues = _fetch_sentry_issues(org, project)
    print(f"  Found {len(sentry_issues)} unresolved issues.")

    tracked = _load_state()
    existing_titles = _list_existing_github_issue_titles()

    filed = 0
    skipped_below_threshold = 0
    skipped_already_tracked = 0
    skipped_existing_issue = 0

    for issue in sentry_issues:
        issue_id: str = issue.get("id", "")
        count_str: str = issue.get("count", "0")
        try:
            count = int(count_str)
        except (ValueError, TypeError):
            count = 0

        if count < RCA_MIN_EVENTS:
            skipped_below_threshold += 1
            continue

        if issue_id in tracked:
            skipped_already_tracked += 1
            continue

        title = f"[sentry-rca] {issue.get('title', 'Unknown error')}"
        if title.lower() in existing_titles:
            skipped_existing_issue += 1
            # Add to tracked set even though we didn't file it (it's on GitHub already)
            tracked.add(issue_id)
            continue

        body = _build_issue_body(issue, org, project)
        print(f"  Filing GitHub issue: {title}")

        try:
            _file_github_issue(title, body)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"  WARNING: Failed to file issue for {title}: {exc}")
            continue

        tracked.add(issue_id)
        filed += 1

    _save_state(tracked)

    print(
        f"\nDone: {filed} issues filed, "
        f"{skipped_below_threshold} below threshold, "
        f"{skipped_already_tracked} already in state, "
        f"{skipped_existing_issue} already on GitHub."
    )


if __name__ == "__main__":
    main()
