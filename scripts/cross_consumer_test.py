#!/usr/bin/env python3
"""Cross-consumer regression testing for motto-common.

Clones or updates a set of consumer repositories, installs the current
checkout of motto-common into each one, runs their test suites, and
aggregates the results into a pass/fail report.

Usage:
    uv run python scripts/cross_consumer_test.py [--repo-dir PATH] [--verbose]

Environment:
    CONSUMER_REPOS     - comma-separated list of consumer GitHub repos
                         (defaults to the built-in list below).
    GITHUB_TOKEN       - optional token for cloning private repos.
    MOTTO_COMMON_PATH  - path to motto-common checkout (defaults to repo root).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess  # nosec B404
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Default consumer repositories (GitHub org/repo format).
# ---------------------------------------------------------------------------
DEFAULT_CONSUMER_REPOS: list[str] = [
    "lkmotto/motto-director",
    "lkmotto/motto-appraisal-pipeline",
    "lkmotto/motto-sdr-agent",
]

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class RepoResult:
    """Test result for a single consumer repo."""

    repo: str
    success: bool
    exit_code: int
    output: str = ""
    error: str = ""


@dataclass
class AggregateReport:
    """Aggregated test results across all consumer repos."""

    results: list[RepoResult] = field(default_factory=list)
    total: int = 0
    passed: int = 0
    failed: int = 0

    def summary(self) -> str:
        lines = [
            "=" * 72,
            "Cross-Consumer Regression Test Report",
            "=" * 72,
            f"Total repos tested: {self.total}",
            f"Passed: {self.passed}",
            f"Failed: {self.failed}",
            "",
        ]
        for r in self.results:
            status = "PASS" if r.success else "FAIL"
            lines.append(f"  [{status}] {r.repo} (exit={r.exit_code})")
            if not r.success and r.error:
                lines.append(f"         Error: {r.error.strip()}")
        lines.append("")
        lines.append("Overall: PASS" if self.failed == 0 else "Overall: FAIL")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 600) -> tuple[int, str, str]:
    """Run a command and return (exit_code, stdout, stderr)."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,  # nosec B603
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {timeout}s: {' '.join(cmd)}"
    except FileNotFoundError as e:
        return -1, "", str(e)


def _clone_or_pull(repo: str, dest: Path, token: str | None = None) -> Path:
    """Clone a repo if it does not exist; otherwise fetch and reset to origin/main."""
    repo_dir = dest / repo.split("/")[-1]

    clone_url = f"https://github.com/{repo}.git"
    if token:
        clone_url = f"https://{token}@github.com/{repo}.git"

    if repo_dir.exists():
        print(f"  [pull] Updating {repo} ...")
        _run(["git", "fetch", "origin"], cwd=repo_dir)
        # Try main, then master
        for branch in ("main", "master"):
            rc, _, _ = _run(["git", "checkout", branch], cwd=repo_dir)
            if rc == 0:
                _run(["git", "reset", "--hard", f"origin/{branch}"], cwd=repo_dir)
                break
    else:
        print(f"  [clone] Cloning {repo} ...")
        _run(["git", "clone", clone_url, str(repo_dir)], cwd=dest)

    return repo_dir


def _install_motto_common(repo_dir: Path, motto_common_path: Path) -> None:
    """Install the local motto-common checkout into the consumer repo."""

    # Determine how to install based on whether the repo uses uv or pip.
    pyproject = repo_dir / "pyproject.toml"
    reqs_txt = repo_dir / "requirements.txt"

    if pyproject.exists():
        # Try uv first, fall back to pip
        if shutil.which("uv"):
            _run(
                ["uv", "add", str(motto_common_path)],
                cwd=repo_dir,
            )
        else:
            _run(
                [sys.executable, "-m", "pip", "install", "-e", str(motto_common_path)],
                cwd=repo_dir,
            )
    elif reqs_txt.exists():
        # pip with requirements.txt
        _run(
            [sys.executable, "-m", "pip", "install", "-e", str(motto_common_path)],
            cwd=repo_dir,
        )
        # Also install requirements if present
        _run(
            [sys.executable, "-m", "pip", "install", "-r", str(reqs_txt)],
            cwd=repo_dir,
        )
    else:
        # Bare pip install
        _run(
            [sys.executable, "-m", "pip", "install", "-e", str(motto_common_path)],
            cwd=repo_dir,
        )


def _run_tests(repo_dir: Path) -> tuple[int, str, str]:
    """Run the consumer repo's test suite.

    Tries uv run pytest first, then pip pytest, then bare pytest.
    """
    pyproject = repo_dir / "pyproject.toml"

    if pyproject.exists() and shutil.which("uv"):
        rc, stdout, stderr = _run(["uv", "run", "pytest", "-q"], cwd=repo_dir)
        if rc == 0 or "No module named" not in stderr:
            return rc, stdout, stderr

    # Fall back to direct pytest
    return _run([sys.executable, "-m", "pytest", "-q"], cwd=repo_dir)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Cross-consumer regression testing")
    parser.add_argument(
        "--repo-dir",
        type=Path,
        default=None,
        help="Directory to clone consumer repos into (default: temp dir)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print full test output for each consumer",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON instead of human-readable text",
    )
    args = parser.parse_args()

    # Determine motto-common path (assume script is in scripts/ relative to repo root)
    motto_common_path = Path(os.getenv("MOTTO_COMMON_PATH", ""))
    if not motto_common_path or not motto_common_path.exists():
        script_dir = Path(__file__).resolve().parent
        motto_common_path = script_dir.parent

    if not (motto_common_path / "pyproject.toml").exists():
        print(f"ERROR: motto-common not found at {motto_common_path}", file=sys.stderr)
        return 1

    # Consumer repo list
    consumer_list_env = os.getenv("CONSUMER_REPOS", "")
    if consumer_list_env:
        consumer_repos = [r.strip() for r in consumer_list_env.split(",") if r.strip()]
    else:
        consumer_repos = DEFAULT_CONSUMER_REPOS

    github_token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")

    # Working directory for clones
    work_dir = args.repo_dir or Path(tempfile.mkdtemp(prefix="cross_consumer_test_"))

    print(f"motto-common path: {motto_common_path}")
    print(f"Consumer repos: {consumer_repos}")
    print(f"Working directory: {work_dir}")
    print()

    report = AggregateReport()

    for repo in consumer_repos:
        print(f"--- Testing consumer: {repo} ---")
        result = RepoResult(repo=repo, success=False, exit_code=-1)

        try:
            # Step 1: Clone or update
            repo_dir = _clone_or_pull(repo, work_dir, github_token)

            # Step 2: Install motto-common
            print(f"  [install] Installing motto-common into {repo_dir.name} ...")
            _install_motto_common(repo_dir, motto_common_path)

            # Step 3: Run tests
            print(f"  [test] Running tests for {repo_dir.name} ...")
            exit_code, stdout, stderr = _run_tests(repo_dir)

            result.exit_code = exit_code
            result.output = stdout
            result.error = stderr
            result.success = exit_code == 0

            if args.verbose:
                print(stdout)
                if stderr:
                    print(stderr, file=sys.stderr)

            status = "PASS" if result.success else "FAIL"
            print(f"  [{status}] {repo} (exit={exit_code})")

        except Exception as exc:  # noqa: BLE001
            result.error = str(exc)
            result.success = False
            print(f"  [FAIL] {repo}: {exc}")

        report.results.append(result)

    report.total = len(report.results)
    report.passed = sum(1 for r in report.results if r.success)
    report.failed = sum(1 for r in report.results if not r.success)

    if args.json:
        json_data: dict[str, Any] = {
            "total": report.total,
            "passed": report.passed,
            "failed": report.failed,
            "results": [
                {
                    "repo": r.repo,
                    "success": r.success,
                    "exit_code": r.exit_code,
                    "output": r.output[-500:] if r.output else "",
                    "error": r.error[-500:] if r.error else "",
                }
                for r in report.results
            ],
        }
        print(json.dumps(json_data, indent=2))
    else:
        print()
        print(report.summary())

    # Clean up temp dir if we created one
    if not args.repo_dir and work_dir.exists():
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass

    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
