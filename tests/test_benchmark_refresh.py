import subprocess
from unittest.mock import Mock

import pytest

from scripts import benchmark_refresh
from scripts.benchmark_refresh import (
    ensure_app_closed,
    safe_failure_detail,
    safe_mail_metadata_profiles,
)


@pytest.mark.parametrize("returncode", [0, 2])
def test_benchmark_refuses_running_app_or_failed_process_check_without_private_output(monkeypatch, returncode):
    monkeypatch.setattr(benchmark_refresh.sys, "platform", "darwin")
    run = Mock(return_value=subprocess.CompletedProcess([], returncode, b"private details", b"secret"))
    monkeypatch.setattr(benchmark_refresh.subprocess, "run", run)
    with pytest.raises(SystemExit) as error:
        ensure_app_closed()
    assert "private details" not in str(error.value) and "secret" not in str(error.value)
    run.assert_called_once_with(["/usr/bin/pgrep", "-x", "Email Reader"], capture_output=True)


def test_benchmark_accepts_verified_absent_app(monkeypatch):
    monkeypatch.setattr(benchmark_refresh.sys, "platform", "darwin")
    monkeypatch.setattr(benchmark_refresh.subprocess, "run",
                        Mock(return_value=subprocess.CompletedProcess([], 1, b"", b"")))
    ensure_app_closed()


def test_non_macos_benchmark_does_not_assume_native_process_tools(monkeypatch):
    monkeypatch.setattr(benchmark_refresh.sys, "platform", "linux")
    monkeypatch.setattr(benchmark_refresh.subprocess, "run", Mock(side_effect=AssertionError("no native tools")))
    ensure_app_closed()


def test_benchmark_failure_detail_excludes_provider_text():
    details = safe_failure_detail(
        b'Startup: 1.0s\nRefresh components: {"discovery": 1.2, "metadata": 30.0, "private": "secret"}\n'
        b'Apple Mail did not respond before the operation timed out for private@example.com\n',
        b"",
    )
    assert details == {
        "reason": "apple_mail_timeout",
        "completed_stage_seconds": {"discovery": 1.2, "metadata": 30.0},
    }
    assert "private@example.com" not in str(details)
    assert "secret" not in str(details)


def test_benchmark_failure_detail_handles_bad_timing_json_without_raw_output():
    details = safe_failure_detail(
        b'Refresh components: not-json\nSensitive mailbox content\n', b""
    )
    assert details == {"reason": "other_failure", "completed_stage_seconds": {}}


def test_benchmark_failure_detail_retains_only_known_mail_operation():
    details = safe_failure_detail(
        b"Apple Mail timed out during incremental_metadata for private@example.com\n",
        b"",
    )
    assert details == {
        "reason": "apple_mail_timeout",
        "completed_stage_seconds": {},
        "operation": "incremental_metadata",
    }
    assert "private@example.com" not in str(details)


def test_mail_profile_retains_only_numeric_timings_and_known_roles():
    profile = safe_mail_metadata_profiles(
        b'Mail metadata profile: [{"role":"inbox","seconds":1.25,"resolve":0.1,'
        b'"first_date":true,"count":"private@example.com","dates":-1,'
        b'"fast_path":true,"body":"secret","token":"secret"},'
        b'{"role":"private@example.com","seconds":2},'
        b'{"role":"drafts","seconds":NaN,"resolve":Infinity,"fast_path":"secret"}]\n'
        b'Sensitive message content outside profile\n'
    )
    assert profile == [
        {"mailbox_ordinal": 1, "role": "inbox", "seconds": 1.25, "resolve": 0.1, "fast_path": True},
        {"mailbox_ordinal": 3, "role": "drafts"},
    ]
    assert "secret" not in str(profile) and "private@example.com" not in str(profile)


def test_mail_profile_rejects_malformed_nonlist_and_nonrow_data():
    assert safe_mail_metadata_profiles(
        b'Mail metadata profile: not-json\nMail metadata profile: {"role":"inbox"}\n'
        b'Mail metadata profile: [null, "secret", {"role":["inbox"]}]\n'
    ) == []
