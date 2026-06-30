#!/usr/bin/env python3
"""Auto-rollback on Sentry Release Health failure.

Monitors Sentry Release Health for the most recent release and triggers
a rollback when error-rate or crash-rate spikes exceed configurable
thresholds.

The script queries Sentry's Release Health API for *sessions* data,
computing the crash-free session rate and error rate for the latest
release.  If either metric drops below its threshold, the script signals
a rollback via one of several mechanisms (configurable):

- **GitHub issue** — creates a high-priority issue tagged ``auto-rollback``
  with the release details and health metrics (default, always on).
- **GitHub Actions dispatch** — fires a ``repository_dispatch`` event so
  a deploy/rollback workflow in the consumer repo can react.
- **Slack webhook** — posts a rollback alert (if ``SLACK_WEBHOOK_URL`` is
  set).

Environment:
    SENTRY_AUTH_TOKEN          – Sentry API token (``event:read`` scope)
    SENTRY_ORG                 – Sentry organisation slug
    SENTRY_PROJECT             – Sentry project slug
    GITHUB_TOKEN               – GitHub PAT (``issues:write``,
                                  ``repo`` for dispatch)
    GITHUB_REPOSITORY          – GitHub owner/repo slug (CI default)

    ROLLBACK_CRASH_FREE_THRESHOLD – Minimum crash-free session % (default ``95.0``)
    ROLLBACK_ERROR_RATE_THRESHOLD – Maximum error events/session (default ``0.05``)

    ROLLBACK_DISPATCH_REPO     – Repo to dispatch rollback event to
                                  (optional; defaults to GITHUB_REPOSITORY)
    ROLLBACK_DISPATCH_TOKEN    – PAT for dispatch (optional; falls back to
                                  GITHUB_TOKEN)
    SLACK_WEBHOOK_URL          – Slack incoming webhook for alerts (optional)

Usage (local):
    SENTRY_AUTH_TOKEN=<token> SENTRY_ORG=lkmotto \\
        SENTRY_PROJECT=motto-common GITHUB_TOKEN=<gh_token> \\
        GITHUB_REPOSITORY=lkmotto/motto-common python scripts/auto_rollback.py

Usage (CI, ``.github/workflows/auto-rollback.yml``):
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

# Health thresholds
CRASH_FREE_THRESHOLD = float(os.getenv("ROLLBACK_CRASH_FREE_THRESHOLD", "95.0"))
ERROR_RATE_THRESHOLD = float(os.getenv("ROLLBACK_ERROR_RATE_THRESHOLD", "0.05"))

# The number of minutes of recent session data to examine
HEALTH_LOOKBACK_MINUTES = int(os.getenv("ROLLBACK_LOOKBACK_MINUTES", "60"))


# ---------------------------------------------------------------------------
# Helpers — HTTP
# ---------------------------------------------------------------------------


def _sentry_request(
    method: str, path: str, data: dict[str, object] | None = None
) -> typing.Any:
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


def _github_request(
    method: str, path: str, data: dict[str, object] | None = None,
    token: str | None = None,
) -> typing.Any:
    """Send an authenticated request to the GitHub API."""
    gh_token = token or os.getenv("GITHUB_TOKEN")
    if not gh_token:
        _fatal("GITHUB_TOKEN environment variable is required.")

    url = f"https://api.github.com{path}"
    body = json.dumps(data).encode("utf-8") if data else None

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {gh_token}",
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
# Sentry Release Health
# ---------------------------------------------------------------------------


class ReleaseHealth(typing.NamedTuple):
    """Aggregated health metrics for a single release."""

    release_version: str
    total_sessions: int
    crash_free_sessions: int
    error_count: int
    crash_free_pct: float
    error_rate: float
    health_status: str  # "healthy", "degraded", "unhealthy"


def _fetch_latest_release(org: str, project: str) -> str | None:
    """Return the version string of the most recent Sentry release."""
    releases: list[dict[str, typing.Any]] = _sentry_request(
        "GET",
        f"/organizations/{org}/releases/?project={project}&per_page=1",
    )
    if not isinstance(releases, list) or not releases:
        return None
    return releases[0].get("version")


def _fetch_release_health(
    org: str, project: str, release_version: str
) -> ReleaseHealth:
    """Query Sentry Release Health sessions data for the given release.

    Uses the sessions API to retrieve session counts (healthy, crashed,
    errored) for the last N minutes.  Computes crash-free percentage and
    error rate from the raw data.
    """
    now = _dt.datetime.now(tz=_dt.UTC)
    start = now - _dt.timedelta(minutes=HEALTH_LOOKBACK_MINUTES)
    end = now

    params = {
        "field": ["sum(session)", "crash_rate", "count_unique(user)"],
        "groupBy": ["session.status"],
        "interval": f"{HEALTH_LOOKBACK_MINUTES}m",
        "statsPeriodStart": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "statsPeriodEnd": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "project": [project],
        "query": f"release:{release_version}",
    }

    sessions_result = _sentry_request(
        "GET",
        f"/organizations/{org}/sessions/?" + "&".join(
            f"{k}={v}" for k, v in params.items() if isinstance(v, str)
        ),
    )

    # Also query the issue count for an error-rate estimate
    issues_result = _sentry_request(
        "GET",
        f"/projects/{org}/{project}/issues/?"
        f"query=is:unresolved&statsPeriod={HEALTH_LOOKBACK_MINUTES}m&limit=100",
    )

    return _compute_health(release_version, sessions_result, issues_result)


def _compute_health(
    release_version: str,
    sessions_data: typing.Any,
    issues_data: typing.Any,
) -> ReleaseHealth:
    """Compute aggregate health metrics from raw Sentry API responses."""
    total = 0
    crashed = 0

    # Parse sessions data
    groups: list[dict[str, typing.Any]] = []
    if isinstance(sessions_data, dict):
        groups = sessions_data.get("groups", [])
    elif isinstance(sessions_data, list):
        groups = sessions_data

    for group in groups:
        status = group.get("by", {}).get("session.status", "")
        totals = group.get("totals", {})
        count = totals.get("sum(session)", 0)

        total += count
        if status == "crashed":
            crashed += count

    # Parse issue/error count
    error_count = 0
    if isinstance(issues_data, list):
        error_count = sum(
            int(i.get("count", 0)) for i in issues_data
        )

    # Compute metrics
    crash_free = total - crashed
    crash_free_pct = (crash_free / total * 100) if total > 0 else 100.0
    error_rate = (error_count / total) if total > 0 else 0.0

    # Determine health status
    if crash_free_pct < CRASH_FREE_THRESHOLD or error_rate > ERROR_RATE_THRESHOLD:
        health_status = "unhealthy"
    elif crash_free_pct < (CRASH_FREE_THRESHOLD + 2.0):
        health_status = "degraded"
    else:
        health_status = "healthy"

    return ReleaseHealth(
        release_version=release_version,
        total_sessions=total,
        crash_free_sessions=crash_free,
        error_count=error_count,
        crash_free_pct=round(crash_free_pct, 2),
        error_rate=round(error_rate, 4),
        health_status=health_status,
    )


# ---------------------------------------------------------------------------
# Rollback actions
# ---------------------------------------------------------------------------


def _file_rollback_issue(
    health: ReleaseHealth, org: str, project: str
) -> None:
    """Create a GitHub issue signalling that rollback is needed."""
    repo = os.getenv("GITHUB_REPOSITORY", "")
    if not repo:
        _fatal("GITHUB_REPOSITORY is required for filing issues.")

    title = (
        f"[auto-rollback] Release {health.release_version} is {health.health_status}"
        f" — rollback recommended"
    )

    body_lines: list[str] = [
        "> Auto-filed by `scripts/auto_rollback.py` — "
        "Sentry Release Health monitor detected a spike.",
        "",
        "## Release Health Metrics",
        "",
        "| Metric | Value | Threshold | Status |",
        "|--------|-------|-----------|--------|",
        (
            f"| **Crash-free sessions** | {health.crash_free_pct:.2f}% "
            f"({health.crash_free_sessions}/{health.total_sessions}) "
            f"| ≥ {CRASH_FREE_THRESHOLD:.1f}% "
            f"| {'✅' if health.crash_free_pct >= CRASH_FREE_THRESHOLD else '❌'} |"
        ),
        (
            f"| **Error rate** | {health.error_rate:.4f} "
            f"({health.error_count} errors) "
            f"| ≤ {ERROR_RATE_THRESHOLD:.4f} "
            f"| {'✅' if health.error_rate <= ERROR_RATE_THRESHOLD else '❌'} |"
        ),
        f"| **Total sessions** | {health.total_sessions} | — | — |",
        "",
        "## Release Information",
        "",
        f"- **Release version**: `{health.release_version}`",
        f"- **Sentry project**: `{org}/{project}`",
        f"- **Lookback window**: {HEALTH_LOOKBACK_MINUTES} min",
        f"- **Checked at**: {_dt.datetime.now(tz=_dt.UTC).isoformat()}",
        "",
        "## Recommended Action",
        "",
        "1. Review the release in Sentry for details on the error spike.",
        "2. If confirmed, roll back to the previous stable release.",
        "3. Investigate root cause using the RCA issue linked below (if available).",
    ]

    _github_request(
        "POST",
        f"/repos/{repo}/issues",
        {
            "title": title,
            "body": "\n".join(body_lines),
            "labels": ["auto-rollback", "high-priority"],
        },
    )


def _dispatch_rollback(health: ReleaseHealth) -> bool:
    """Send a repository_dispatch event to trigger a rollback workflow."""
    dispatch_repo = os.getenv("ROLLBACK_DISPATCH_REPO")
    if not dispatch_repo:
        return False

    dispatch_token = os.getenv("ROLLBACK_DISPATCH_TOKEN") or os.getenv("GITHUB_TOKEN")
    if not dispatch_token:
        print("  WARNING: No dispatch token available, skipping dispatch.")
        return False

    client_payload: dict[str, object] = {
        "release_version": health.release_version,
        "crash_free_pct": health.crash_free_pct,
        "error_rate": health.error_rate,
        "total_sessions": health.total_sessions,
        "health_status": health.health_status,
        "checked_at": _dt.datetime.now(tz=_dt.UTC).isoformat(),
    }

    payload: dict[str, object] = {
        "event_type": "auto-rollback",
        "client_payload": client_payload,
    }

    _github_request(
        "POST",
        f"/repos/{dispatch_repo}/dispatches",
        payload,
        token=dispatch_token,
    )

    print(f"  Dispatched rollback event to {dispatch_repo}")
    return True


def _post_slack_alert(health: ReleaseHealth, org: str, project: str) -> bool:
    """Post a rollback alert to Slack if SLACK_WEBHOOK_URL is configured."""
    webhook = os.getenv("SLACK_WEBHOOK_URL")
    if not webhook:
        return False

    color = "danger" if health.health_status == "unhealthy" else "warning"

    message = {
        "attachments": [
            {
                "color": color,
                "title": (
                    f"Auto-Rollback Alert: Release {health.release_version} "
                    f"is {health.health_status.upper()}"
                ),
                "fields": [
                    {
                        "title": "Crash-Free %",
                        "value": f"{health.crash_free_pct:.2f}%",
                        "short": True,
                    },
                    {
                        "title": "Error Rate",
                        "value": f"{health.error_rate:.4f}",
                        "short": True,
                    },
                    {
                        "title": "Total Sessions",
                        "value": str(health.total_sessions),
                        "short": True,
                    },
                    {
                        "title": "Error Count",
                        "value": str(health.error_count),
                        "short": True,
                    },
                    {
                        "title": "Project",
                        "value": f"{org}/{project}",
                        "short": True,
                    },
                    {
                        "title": "Threshold",
                        "value": (
                            f"CF \u2265 {CRASH_FREE_THRESHOLD}%, "
                            f"ER \u2264 {ERROR_RATE_THRESHOLD}"
                        ),
                        "short": True,
                    },
                ],
                "footer": "motto-common auto-rollback monitor",
                "ts": int(_dt.datetime.now(tz=_dt.UTC).timestamp()),
            }
        ]
    }

    req = urllib.request.Request(
        webhook,
        data=json.dumps(message).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            if resp.status == 200:
                print("  Posted Slack alert.")
                return True
            print(f"  Slack returned status {resp.status}")
            return False
    except Exception as exc:  # noqa: BLE001
        print(f"  WARNING: Failed to post Slack alert: {exc}")
        return False


# ---------------------------------------------------------------------------
# State tracking (idempotency)
# ---------------------------------------------------------------------------

STATE_FILE = os.path.join(os.path.dirname(__file__), ".rollback_state.json")


def _rollback_already_signalled(release_version: str, health_status: str) -> bool:
    """Check if a rollback was already signalled for this release+status combo."""
    if not os.path.isfile(STATE_FILE):
        return False
    try:
        with open(STATE_FILE) as fh:
            state = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return False
    last_release: object = state.get("last_signalled_release")
    last_status: object = state.get("last_signalled_status")
    return bool(last_release == release_version and last_status == health_status)


def _record_rollback_signal(release_version: str, health_status: str) -> None:
    """Record that a rollback signal was sent for this release."""
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as fh:
        json.dump(
            {
                "last_signalled_release": release_version,
                "last_signalled_status": health_status,
                "signalled_at": _dt.datetime.now(tz=_dt.UTC).isoformat(),
            },
            fh,
            indent=2,
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

    print(f"Checking Sentry Release Health for {org}/{project} ...")
    print(f"  Crash-free threshold: {CRASH_FREE_THRESHOLD}%")
    print(f"  Error rate threshold: {ERROR_RATE_THRESHOLD}")
    print(f"  Lookback window: {HEALTH_LOOKBACK_MINUTES} min")

    # 1. Find the latest release
    release_version = _fetch_latest_release(org, project)
    if not release_version:
        print("  No releases found — nothing to monitor.")
        return

    print(f"  Latest release: {release_version}")

    # 2. Query release health
    health = _fetch_release_health(org, project, release_version)

    print(
        f"  Health: {health.health_status.upper()} "
        f"(crash-free: {health.crash_free_pct}%, "
        f"error rate: {health.error_rate}, "
        f"sessions: {health.total_sessions})"
    )

    # 3. If healthy, do nothing
    if health.health_status == "healthy":
        print("  Release is healthy — no action needed.")
        return

    # 4. If degraded or unhealthy, check idempotency
    if _rollback_already_signalled(release_version, health.health_status):
        print(
            f"  Rollback already signalled for {release_version} "
            f"({health.health_status}) — skipping."
        )
        return

    print(f"  Release is {health.health_status.upper()} — signalling rollback ...")

    # 5. Signal rollback
    _file_rollback_issue(health, org, project)
    print("  Created rollback GitHub issue.")

    _dispatch_rollback(health)
    _post_slack_alert(health, org, project)

    _record_rollback_signal(release_version, health.health_status)
    print("  Rollback signal recorded.")


if __name__ == "__main__":
    main()
