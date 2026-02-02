#!/usr/bin/env python3
"""
Test orchestrator for Claude Code API Server integration tests.

Discovers and executes test modules against a running Docker container,
aggregates results into a consolidated JSON report.

Usage:
    python tests/run_tests.py [OPTIONS]

Options:
    --base-url URL        Server base URL (default: http://127.0.0.1:8000)
    --admin-key KEY       Admin API key (or set TEST_ADMIN_API_KEY env var)
    --anthropic-key KEY   Anthropic API key (or set ANTHROPIC_API_KEY env var)
    --report-dir DIR      Report output directory (default: tests/reports/)
    --skip-ai             Skip the AI integration test
    --module NAME         Run only a specific test module (e.g., test_01_health)
    --verbose             Pass -v to pytest
"""

import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import httpx

# Add project root to path so helpers can be imported
PROJECT_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent / "tests"

# Cleanup prefixes for global safety-net cleanup
CLEANUP_PREFIXES = {
    "clients": "test-client-",
    "agents": "test-agent-",
    "skills": "test-skill-",
    "mcp_servers": "test-mcp-",
    "security_profiles": "test-profile-",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Integration test orchestrator")
    parser.add_argument("--base-url", default=os.environ.get("TEST_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--admin-key", default=os.environ.get("TEST_ADMIN_API_KEY", ""))
    parser.add_argument("--anthropic-key", default=os.environ.get("ANTHROPIC_API_KEY", ""))
    parser.add_argument("--report-dir", default=str(Path(__file__).resolve().parent / "reports"))
    parser.add_argument("--skip-ai", action="store_true")
    parser.add_argument("--module", default=None)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def health_check(base_url: str) -> bool:
    try:
        resp = httpx.get(f"{base_url}/v1/health", timeout=10)
        return resp.status_code == 200
    except Exception:
        return False


def discover_modules(skip_ai: bool, only_module: str | None) -> list[str]:
    """Discover test modules sorted by filename."""
    pattern = str(TESTS_DIR / "test_*.py")
    modules = sorted(glob.glob(pattern))

    if skip_ai:
        modules = [m for m in modules if "test_99" not in m and "test_98" not in m]

    if only_module:
        modules = [m for m in modules if only_module in Path(m).stem]

    return modules


def run_module(module_path: str, env: dict, verbose: bool) -> dict:
    """Run a single test module via pytest subprocess."""
    module_name = Path(module_path).stem
    tmp_report = tempfile.NamedTemporaryFile(suffix=".json", delete=False, prefix=f"{module_name}_")
    tmp_report.close()

    cmd = [
        sys.executable, "-m", "pytest",
        module_path,
        "--json-report",
        f"--json-report-file={tmp_report.name}",
        "--json-report-indent=2",
        "--tb=short",
        "-rs",
        "-v" if verbose else "-q",
    ]

    result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)

    return {
        "name": module_name,
        "report_path": tmp_report.name,
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def global_cleanup(base_url: str, admin_key: str) -> None:
    """Delete any leftover test resources (safety net)."""
    headers = {"Authorization": f"Bearer {admin_key}"}
    client = httpx.Client(base_url=base_url, timeout=15)

    # Clean clients
    try:
        resp = client.get("/v1/admin/clients", headers=headers)
        if resp.status_code == 200:
            for c in resp.json():
                if c["client_id"].startswith(CLEANUP_PREFIXES["clients"]):
                    client.delete(f"/v1/admin/clients/{c['client_id']}", headers=headers)
    except Exception:
        pass

    # Clean agents
    try:
        resp = client.get("/v1/admin/agents", headers=headers)
        if resp.status_code == 200:
            for a in resp.json():
                if a["name"].startswith(CLEANUP_PREFIXES["agents"]):
                    client.delete(f"/v1/admin/agents/{a['name']}", headers=headers)
    except Exception:
        pass

    # Clean skills
    try:
        resp = client.get("/v1/admin/skills", headers=headers)
        if resp.status_code == 200:
            for s in resp.json():
                if s["name"].startswith(CLEANUP_PREFIXES["skills"]):
                    client.delete(f"/v1/admin/skills/{s['name']}", headers=headers)
    except Exception:
        pass

    # Clean MCP servers
    try:
        resp = client.get("/v1/admin/mcp", headers=headers)
        if resp.status_code == 200:
            for s in resp.json():
                if s["name"].startswith(CLEANUP_PREFIXES["mcp_servers"]):
                    client.delete(
                        f"/v1/admin/mcp/{s['name']}",
                        headers=headers,
                        params={"keep_package": "true"},
                    )
    except Exception:
        pass

    # Clean security profiles
    try:
        resp = client.get("/v1/admin/security-profiles", headers=headers)
        if resp.status_code == 200:
            for p in resp.json():
                if p["name"].startswith(CLEANUP_PREFIXES["security_profiles"]):
                    client.delete(f"/v1/admin/security-profiles/{p['name']}", headers=headers)
    except Exception:
        pass

    client.close()


def aggregate_results(module_results: list[dict], base_url: str, started_at: datetime) -> dict:
    """Aggregate per-module results into consolidated report."""
    # Import here to avoid path issues
    sys.path.insert(0, str(TESTS_DIR))
    from helpers.report import aggregate_reports
    return aggregate_reports(module_results, base_url, started_at)


def print_summary(report: dict) -> None:
    """Print human-readable summary to stdout."""
    s = report["summary"]
    print(f"\n{'=' * 60}")
    print(f"Integration Test Results")
    print(f"{'=' * 60}")
    print(f"Total: {s['total']}  Passed: {s['passed']}  Failed: {s['failed']}  Errors: {s['errors']}  Skipped: {s['skipped']}")
    print()

    for mod in report["modules"]:
        if mod["status"] == "passed":
            status_icon = "PASS"
        elif mod["status"] == "skipped":
            status_icon = "SKIP"
        else:
            status_icon = "FAIL"
        print(f"  [{status_icon}] {mod['name']}: {mod['tests_passed']}/{mod['tests_total']} passed ({mod['duration_seconds']}s)")

        if mod.get("skip_reason"):
            reason = str(mod["skip_reason"])
            # Extract clean message from pytest's tuple representation
            prefix = "Skipped: "
            if prefix in reason:
                reason = reason[reason.index(prefix) + len(prefix):].rstrip("\")'")
            print(f"         Reason: {reason}")
        if mod.get("error"):
            print(f"         Error: {mod['error']}")

        for ft in mod.get("failed_tests", []):
            print(f"         FAILED: {ft['name']}")
            if ft.get("assertion"):
                # Print first 2 lines of assertion
                lines = str(ft["assertion"]).strip().split("\n")[:2]
                for line in lines:
                    print(f"                 {line}")

    print(f"{'=' * 60}")
    all_passed = s["failed"] == 0 and s["errors"] == 0 and s["skipped"] == 0
    if all_passed:
        verdict = "ALL PASSED"
    elif s["skipped"] > 0 and s["failed"] == 0 and s["errors"] == 0:
        verdict = f"INCOMPLETE — {s['skipped']} test(s) skipped"
    else:
        verdict = "FAILURES DETECTED"
    print(f"Result: {verdict}")
    print()


def main():
    args = parse_args()

    # Validate prerequisites
    if not args.admin_key:
        print("ERROR: Admin API key is required. Set TEST_ADMIN_API_KEY or use --admin-key.")
        sys.exit(2)

    if not args.anthropic_key and not args.skip_ai:
        print("ERROR: Anthropic API key is required for AI integration tests.")
        print("  Set ANTHROPIC_API_KEY, use --anthropic-key, or pass --skip-ai to skip.")
        sys.exit(2)

    # Health check
    print(f"Checking server at {args.base_url}...")
    if not health_check(args.base_url):
        print(f"ERROR: Server unreachable at {args.base_url}")
        sys.exit(2)
    print("Server is healthy.")

    # Gate check: admin API must be functional before running any tests
    print("Verifying admin API (GET /v1/admin/clients)...", end=" ", flush=True)
    try:
        gate_resp = httpx.get(
            f"{args.base_url}/v1/admin/clients",
            headers={"Authorization": f"Bearer {args.admin_key}"},
            timeout=10,
        )
        if gate_resp.status_code != 200:
            print(f"FAILED (HTTP {gate_resp.status_code})")
            print()
            print("  This test suite requires access to the admin API.")
            print("  A basic request to GET /v1/admin/clients failed with the")
            print(f"  provided API key (HTTP {gate_resp.status_code}).")
            print("  All subsequent tests depend on admin access and cannot proceed.")
            print()
            print("  Please verify that:")
            print("    - The admin API key (--admin-key / TEST_ADMIN_API_KEY) is correct")
            print("    - The admin client has the 'admin' role")
            print("    - The server is fully initialized")
            sys.exit(2)
    except Exception as e:
        print(f"FAILED ({e})")
        print()
        print("  This test suite requires access to the admin API.")
        print("  A basic request to GET /v1/admin/clients could not be completed:")
        print(f"    {e}")
        print("  All subsequent tests depend on admin access and cannot proceed.")
        sys.exit(2)
    print("OK.")

    # Discover modules
    modules = discover_modules(args.skip_ai, args.module)
    if not modules:
        print("No test modules found.")
        sys.exit(2)

    print(f"Found {len(modules)} test module(s):")
    for m in modules:
        print(f"  - {Path(m).stem}")

    # Build environment for subprocesses
    env = {
        **os.environ,
        "TEST_BASE_URL": args.base_url,
        "TEST_ADMIN_API_KEY": args.admin_key,
        "ANTHROPIC_API_KEY": args.anthropic_key or "",
        "PYTHONPATH": f"{PROJECT_ROOT}{os.pathsep}{TESTS_DIR}",
    }

    started_at = datetime.now(UTC)
    module_results = []

    # Execute each module
    for module_path in modules:
        module_name = Path(module_path).stem
        print(f"\nRunning {module_name}...", end=" ", flush=True)

        try:
            result = run_module(module_path, env, args.verbose)
            module_results.append(result)

            if result["exit_code"] == 0:
                # Check if all tests were skipped
                if "skipped" in result["stdout"] and "passed" not in result["stdout"]:
                    print("SKIPPED")
                else:
                    print("PASSED")
            elif result["exit_code"] == 5:
                print("NO TESTS COLLECTED")
            else:
                print(f"FAILED (exit code {result['exit_code']})")
            # Print raw output only in verbose mode
            if args.verbose and result["exit_code"] != 0:
                if result["stderr"]:
                    print(result["stderr"][-1000:])
                if result["stdout"]:
                    print(result["stdout"][-1000:])

        except subprocess.TimeoutExpired:
            print("TIMEOUT")
            module_results.append({
                "name": module_name,
                "report_path": None,
                "exit_code": -1,
            })
        except Exception as e:
            print(f"ERROR: {e}")
            module_results.append({
                "name": module_name,
                "report_path": None,
                "exit_code": -1,
            })

    # Global cleanup
    print("\nRunning global cleanup...", end=" ", flush=True)
    global_cleanup(args.base_url, args.admin_key)
    print("done.")

    # Aggregate report
    report = aggregate_results(module_results, args.base_url, started_at)

    # Write report
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    report_path = report_dir / f"report_{timestamp}.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nReport written to: {report_path}")

    # Print summary
    print_summary(report)

    # Cleanup temp files
    for r in module_results:
        rp = r.get("report_path")
        if rp and Path(rp).exists():
            Path(rp).unlink()

    # Exit code: 0 = all passed, 1 = failures/errors, 2 = skipped tests
    s = report["summary"]
    if s["failed"] > 0 or s["errors"] > 0:
        sys.exit(1)
    elif s["skipped"] > 0:
        sys.exit(2)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
