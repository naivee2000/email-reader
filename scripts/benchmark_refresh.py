"""Time the installed app's real sync path without saving private diagnostic output.

Run with Email Reader closed. Same executable interface and process-start boundaries
must be used before and after. A warm-up catches new mail before ten steady runs.
"""
import argparse
import hashlib
import json
import math
import os
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


def ensure_app_closed() -> None:
    """Never overlap the diagnostic with a live editor or scheduled app sync."""
    if sys.platform != "darwin":
        return
    result = subprocess.run(["/usr/bin/pgrep", "-x", "Email Reader"], capture_output=True)
    if result.returncode == 0:
        raise SystemExit("Close Email Reader after saving drafts before starting the benchmark")
    if result.returncode != 1:
        raise SystemExit("Could not verify that Email Reader is closed; benchmark not started")


def safe_mail_metadata_profiles(stdout: bytes) -> list[dict[str, object]]:
    """Allowlist roles and finite timing values; never retain provider strings."""
    profiles = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        if not line.startswith("Mail metadata profile: "):
            continue
        try:
            rows = json.loads(line.removeprefix("Mail metadata profile: "))
        except (ValueError, json.JSONDecodeError):
            continue
        if not isinstance(rows, list):
            continue
        for ordinal, row in enumerate(rows[:30], 1):
            if not isinstance(row, dict) or row.get("role") not in ("inbox", "sent", "drafts"):
                continue
            clean = {"mailbox_ordinal": ordinal, "role": row["role"]}
            for key in ("seconds", "resolve", "first_date", "count", "dates"):
                value = row.get(key)
                if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                    clean[key] = round(float(value), 4)
            if isinstance(row.get("fast_path"), bool):
                clean["fast_path"] = row["fast_path"]
            profiles.append(clean)
    return profiles


def safe_failure_detail(stdout: bytes, stderr: bytes) -> dict[str, object]:
    """Keep only known stage timings and error categories, never provider text."""
    output = stdout.decode("utf-8", errors="replace")
    error = stderr.decode("utf-8", errors="replace")
    components: dict[str, float] = {}
    for line in output.splitlines():
        if not line.startswith("Refresh components: "):
            continue
        try:
            parsed = json.loads(line.removeprefix("Refresh components: "))
            components = {
                key: round(float(parsed[key]), 4)
                for key in ("discovery", "metadata", "accounts", "reload")
                if key in parsed and isinstance(parsed[key], (int, float))
            }
        except (TypeError, ValueError, json.JSONDecodeError):
            components = {}
    combined = output + "\n" + error
    operation = next(
        (name for name in (
            "original_retry", "initial_page", "incremental_metadata",
            "batched_metadata", "incremental_source",
        ) if f"Apple Mail timed out during {name}" in combined),
        None,
    )
    if operation or "Apple Mail did not respond before the operation timed out" in combined:
        reason = "apple_mail_timeout"
    elif "Apple Mail returned invalid metadata" in combined:
        reason = "invalid_mail_metadata"
    elif "Email refresh did not finish within 10 minutes" in combined:
        reason = "overall_timeout"
    elif "Email Reader could not unlock its encrypted data" in combined:
        reason = "private_data_locked"
    else:
        reason = "other_failure"
    details = {"reason": reason, "completed_stage_seconds": components}
    if operation:
        details["operation"] = operation
    return details


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--profile-mail", action="store_true", help="Diagnostic-only, record sanitized per-mailbox metadata timings")
    args = parser.parse_args()
    if args.report.exists():
        raise SystemExit("Use a new report path; previous measurements must be preserved")
    executable = args.executable.resolve(strict=True)
    ensure_app_closed()
    report = {
        "method": "one warm-up, then ten real --sync-once process elapsed times; includes startup and encrypted backup",
        "started_at": datetime.now(UTC).isoformat(),
        "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "runs_seconds": [], "exit_codes": [], "stage_seconds": [],
    }
    if args.profile_mail:
        report["diagnostic_mail_profile"] = True
        report["mail_metadata_profiles"] = []
    child_environment = dict(os.environ)
    if args.profile_mail:
        child_environment["EMAIL_READER_PROFILE_MAIL"] = "1"
    args.report.parent.mkdir(parents=True, exist_ok=True)
    for index in range(11):
        ensure_app_closed()
        started = time.perf_counter()
        result = subprocess.run([str(executable), "--sync-once"], capture_output=True,
                                timeout=660, env=child_environment)
        elapsed = time.perf_counter() - started
        # Never emit subprocess output: error payloads may contain provider data.
        if index == 0:
            report["warmup_seconds"] = round(elapsed, 4)
            if args.profile_mail:
                report["warmup_mail_metadata_profile"] = safe_mail_metadata_profiles(result.stdout)
        else:
            report["runs_seconds"].append(round(elapsed, 4))
            report["exit_codes"].append(result.returncode)
            report["stage_seconds"].append(
                safe_failure_detail(result.stdout, result.stderr)["completed_stage_seconds"]
            )
            if args.profile_mail:
                report["mail_metadata_profiles"].append(safe_mail_metadata_profiles(result.stdout))
        report["successful"] = result.returncode == 0
        if result.returncode:
            report["last_failure"] = {
                "run": index,
                **safe_failure_detail(result.stdout, result.stderr),
            }
        if report["runs_seconds"]:
            report["median_seconds"] = statistics.median(report["runs_seconds"])
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        print(f"{'Warm-up' if index == 0 else f'Run {index}/10'}: {elapsed:.2f}s; exit={result.returncode}", flush=True)
        if result.returncode:
            raise SystemExit("Refresh did not succeed; stopped benchmark without logging private output")


if __name__ == "__main__":
    main()
