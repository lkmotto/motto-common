#!/usr/bin/env python3
"""Configure Sentry alert rules for motto-common.

Creates metric alert rules on the Sentry project for:
- Error rate threshold (high error count in a short window)
- Issue frequency spike (sudden increase in new issues)

Requires:
    SENTRY_AUTH_TOKEN  - Sentry API auth token with ``alerts:write`` scope
    SENTRY_ORG         - Sentry organisation slug (e.g. ``lkmotto``)
    SENTRY_PROJECT     - Sentry project slug (e.g. ``motto-common``)

Usage:
    SENTRY_AUTH_TOKEN=<token> SENTRY_ORG=lkmotto \
        SENTRY_PROJECT=motto-common python scripts/configure_sentry_alerts.py

The script is idempotent: it retrieves existing rules first and only creates
new ones when a rule with the same label does not already exist.
"""

from __future__ import annotations

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

# Alert rule definitions.  Each entry maps to a POST to
# /projects/{org}/{project}/rules/ with the given payload.
ALERT_RULES = [
    {
        "label": "motto-common: High Error Rate (5+ events in 5 min)",
        "description": (
            "Alert when motto-common generates 5 or more error events "
            "within a 5-minute rolling window.  Covers both handled "
            "exceptions captured via ``capture_main_loop`` and unhandled "
            "crashes."
        ),
        "actionMatch": "all",
        "actions": [
            {
                "id": "sentry.mail.actions.EmailNotifyEmailAction",
                "targetType": "team",
                "targetIdentifier": "",  # Replace with team ID or email
            }
        ],
        "conditions": [
            {
                "id": "sentry.rules.conditions.event_frequency.EventFrequencyCondition",
                "interval": "5m",
                "value": 5,
                "comparisonType": "count",
            }
        ],
        "filterMatch": "all",
        "filters": [
            {
                "id": "sentry.rules.filters.level.LevelFilter",
                "level": "40",  # error and above
                "match": "gte",
            }
        ],
        "frequency": 30,  # evaluate every 30 min
    },
    {
        "label": "motto-common: Issue Frequency Spike (3+ new issues in 15 min)",
        "description": (
            "Alert when the number of *new* distinct issues created in "
            "a 15-minute window exceeds the baseline.  This catches "
            "new error types that start appearing at high frequency "
            "after a deploy or config change."
        ),
        "actionMatch": "all",
        "actions": [
            {
                "id": "sentry.mail.actions.EmailNotifyEmailAction",
                "targetType": "team",
                "targetIdentifier": "",  # Replace with team ID or email
            }
        ],
        "conditions": [
            {
                "id": "sentry.rules.conditions.event_frequency.EventFrequencyCondition",
                "interval": "15m",
                "value": 3,
                "comparisonType": "count",
            }
        ],
        "filterMatch": "all",
        "filters": [
            {
                "id": "sentry.rules.filters.age_comparison.AgeComparisonFilter",
                "comparison_type": "older",
                "time": "15",
                "value": "new",
            }
        ],
        "frequency": 15,
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _request(
    method: str, path: str, data: dict[str, typing.Any] | None = None
) -> typing.Any:
    """Send an authenticated request to the Sentry API."""
    token = os.getenv("SENTRY_AUTH_TOKEN")
    if not token:
        print("ERROR: SENTRY_AUTH_TOKEN environment variable is required.")
        sys.exit(1)

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
            result: typing.Any = json.loads(resp.read().decode("utf-8"))
            return result
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP {exc.code} on {method} {path}: {error_body}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    org = os.getenv("SENTRY_ORG")
    project = os.getenv("SENTRY_PROJECT")

    if not org or not project:
        print(
            "ERROR: SENTRY_ORG and SENTRY_PROJECT environment variables "
            "are required."
        )
        sys.exit(1)

    # Fetch existing rules so we can be idempotent
    print(f"Fetching existing alert rules for {org}/{project} ...")
    existing: list[dict[str, typing.Any]] = _request(
        "GET", f"/projects/{org}/{project}/rules/"
    )
    existing_labels = {r.get("label", "") for r in existing}

    created = 0
    skipped = 0
    for rule in ALERT_RULES:
        label = rule["label"]
        if label in existing_labels:
            print(f"  SKIP (already exists): {label}")
            skipped += 1
            continue

        print(f"  CREATE: {label}")
        _request("POST", f"/projects/{org}/{project}/rules/", rule)
        created += 1

    print(f"\nDone: {created} alert rules created, {skipped} already existed.")


if __name__ == "__main__":
    main()
