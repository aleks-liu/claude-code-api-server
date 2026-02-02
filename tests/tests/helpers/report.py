"""Report aggregation logic for consolidating per-module pytest-json-report outputs."""

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path


def aggregate_reports(
    module_reports: list[dict],
    base_url: str,
    started_at: datetime,
) -> dict:
    """
    Merge per-module pytest-json-report outputs into a consolidated report.

    Args:
        module_reports: List of dicts with keys: name, report_path, exit_code
        base_url: Server base URL used for tests
        started_at: When the test run started

    Returns:
        Consolidated report dict.
    """
    completed_at = datetime.now(UTC)

    total = 0
    passed = 0
    failed = 0
    errors = 0
    skipped = 0
    modules = []

    for mod in module_reports:
        mod_name = mod["name"]
        report_path = mod.get("report_path")
        exit_code = mod.get("exit_code", -1)
        stderr = mod.get("stderr", "")

        if report_path is None or not Path(report_path).exists():
            modules.append({
                "name": mod_name,
                "status": "error",
                "duration_seconds": 0,
                "tests_total": 0,
                "tests_passed": 0,
                "tests_failed": 0,
                "failed_tests": [],
                "error": f"Module crashed with exit code {exit_code}",
            })
            errors += 1
            continue

        try:
            with open(report_path) as f:
                content = f.read().strip()
                if not content:
                    raise ValueError("Empty report file")
                report = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            modules.append({
                "name": mod_name,
                "status": "error",
                "duration_seconds": 0,
                "tests_total": 0,
                "tests_passed": 0,
                "tests_failed": 0,
                "failed_tests": [],
                "error": f"Module failed to produce a valid report (exit code {exit_code})",
            })
            errors += 1
            continue

        summary = report.get("summary", {})
        mod_total = summary.get("total", 0)
        mod_passed = summary.get("passed", 0)
        mod_failed = summary.get("failed", 0)
        mod_errors = summary.get("error", 0)
        mod_skipped = summary.get("skipped", 0)
        duration = report.get("duration", 0)

        # Detect module-level skips: pytest.skip(allow_module_level=True)
        # produces a collector-level skip that doesn't appear in the normal
        # test summary. We detect it by checking:
        # 1. The collectors section in pytest-json-report
        # 2. The stdout from pytest (e.g. "1 skipped")
        module_level_skip = False
        skip_reason = ""
        stdout = mod.get("stdout", "")
        if mod_total == 0:
            # Check collectors section
            for collector in report.get("collectors", []):
                longrepr = collector.get("longrepr", "")
                if longrepr and ("Skipped" in longrepr or "skipped" in longrepr):
                    module_level_skip = True
                    skip_reason = longrepr
                    break
                for result_entry in collector.get("result", []):
                    longrepr = result_entry.get("longrepr", "")
                    if longrepr and ("Skipped" in longrepr or "skipped" in longrepr):
                        module_level_skip = True
                        skip_reason = longrepr
                        break
                if module_level_skip:
                    break
            # Fallback: check pytest stdout for "N skipped"
            if not module_level_skip and "skipped" in stdout:
                module_level_skip = True
                # Try to extract reason from SKIPPED line in stdout
                for line in stdout.splitlines():
                    if "SKIPPED" in line and ":" in line:
                        skip_reason = line.split(":", 1)[-1].strip()
                        break

        total += mod_total
        passed += mod_passed
        failed += mod_failed
        errors += mod_errors
        skipped += mod_skipped

        failed_tests = []
        for test in report.get("tests", []):
            if test.get("outcome") != "failed":
                continue

            ft = {
                "name": test.get("nodeid", "").split("::")[-1],
                "duration_seconds": round(test.get("call", {}).get("duration", 0), 3),
                "assertion": test.get("call", {}).get("longrepr", ""),
            }

            # Extract request/response from user_properties
            for prop_name, prop_val in test.get("user_properties", []):
                if prop_name == "last_api_exchange" and isinstance(prop_val, dict):
                    ft["request"] = prop_val.get("request")
                    ft["response"] = prop_val.get("response")

            failed_tests.append(ft)

        if mod_failed > 0 or mod_errors > 0:
            mod_status = "failed"
        elif module_level_skip:
            mod_status = "skipped"
            skipped += 1
        elif mod_total == 0 and exit_code != 0:
            mod_status = "error"
            errors += 1
        elif mod_total > 0 and mod_passed == 0 and mod_skipped == mod_total:
            mod_status = "skipped"
        else:
            mod_status = "passed"

        mod_entry = {
            "name": mod_name,
            "status": mod_status,
            "duration_seconds": round(duration, 1),
            "tests_total": mod_total,
            "tests_passed": mod_passed,
            "tests_failed": mod_failed + mod_errors,
            "failed_tests": failed_tests,
        }
        if mod_status == "skipped" and skip_reason:
            mod_entry["skip_reason"] = skip_reason
        if mod_status == "error" and mod_total == 0:
            mod_entry["error"] = f"No tests collected (exit code {exit_code})"
            if stderr:
                mod_entry["error_detail"] = stderr[-500:].strip()
        modules.append(mod_entry)

    return {
        "run_id": str(uuid.uuid4()),
        "started_at": started_at.isoformat() + "Z",
        "completed_at": completed_at.isoformat() + "Z",
        "base_url": base_url,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "skipped": skipped,
        },
        "modules": modules,
    }
