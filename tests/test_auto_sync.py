from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import email_reader.controller as controller_module
from email_reader.contracts import SyncStepResult
from email_reader.controller import (
    AppController,
    apple_mail_draft_refresh_due,
    automatic_sync_retry_delay,
)
from email_reader.providers.base import ProviderOperationError
from email_reader.scheduling import RefreshRound


def test_drafts_are_due_after_thirty_minutes_or_invalid_clock():
    now = datetime(2026, 9, 27, 20, 0, tzinfo=UTC)
    assert apple_mail_draft_refresh_due("", now)
    assert apple_mail_draft_refresh_due("malformed", now)
    assert not apple_mail_draft_refresh_due((now - timedelta(minutes=29)).isoformat(), now)
    assert apple_mail_draft_refresh_due((now - timedelta(minutes=30)).isoformat(), now)
    assert apple_mail_draft_refresh_due((now + timedelta(minutes=1)).isoformat(), now)


def test_failed_automatic_sync_uses_bounded_backoff():
    assert automatic_sync_retry_delay(300, 1) == 600
    assert automatic_sync_retry_delay(300, 2) == 1200
    assert automatic_sync_retry_delay(300, 4) == 3600
    assert automatic_sync_retry_delay(300, 20) == 3600


def test_background_refresh_continues_during_chat_and_editor_but_honors_backoff(monkeypatch):
    started, delays = [], []
    fake = SimpleNamespace(
        _busy=True, syncBusy=False,
        _editor_draft={},
        _assistant_visible=False,
        _next_automatic_sync_at=0.0,
        context=SimpleNamespace(sync=SimpleNamespace(paused=False)),
        timer=SimpleNamespace(start=delays.append),
        _sync_all=lambda **kwargs: started.append(kwargs),
    )
    monkeypatch.setattr("email_reader.controller.time.monotonic", lambda: 100.0)
    fake._assistant_visible = True
    AppController._automatic_sync(fake)
    fake._assistant_visible = False
    fake._editor_draft = {"body": "unsaved"}
    AppController._automatic_sync(fake)
    fake._editor_draft = {}
    assert len(started) == 2
    started.clear()
    fake._next_automatic_sync_at = 101.0
    AppController._automatic_sync(fake)
    assert started == []
    fake._next_automatic_sync_at = 99.0
    AppController._automatic_sync(fake)
    assert started == [{"force_original_retries": False, "automatic": True}]
    assert delays == [1000]
    fake.context.sync.paused = True
    AppController._automatic_sync(fake)
    assert len(started) == 1


def test_failed_sync_sets_backoff_without_changing_manual_refresh(monkeypatch):
    messages = []
    fake = SimpleNamespace(
        _worker_jobs={"job": {"label": "Syncing all accounts…"}},
        _sync_failures=0,
        _next_automatic_sync_at=0.0,
        context=SimpleNamespace(
            settings=SimpleNamespace(sync_interval_seconds=300),
            database=SimpleNamespace(execute=lambda query, values: messages.append(("cache", values))),
        ),
        _set_busy=lambda value: messages.append(("busy", value)),
        _set_status=lambda value: messages.append(("status", value)),
        _set_activity=lambda **value: messages.append(("activity", value)),
        reload=lambda: messages.append(("reload", True)),
    )
    monkeypatch.setattr("email_reader.controller.time.monotonic", lambda: 100.0)
    AppController._worker_failed(fake, "job", "Apple Mail timed out")
    assert fake._sync_failures == 1
    assert fake._next_automatic_sync_at == 700.0
    assert ("cache", ("apple_mail_discovery_cache_v1",)) in messages
    assert ("busy", False) in messages
    assert ("status", "Apple Mail timed out") in messages
    assert ("reload", True) in messages


def test_one_mail_timeout_does_not_prevent_other_account_checks(monkeypatch):
    monkeypatch.setattr("email_reader.scheduling.time.monotonic", lambda: 100.0)
    accounts = [{"id": f"apple-mail:{index}", "email": f"person{index}@example.com",
                 "sync_cursor": datetime.now(UTC).isoformat(), "sync_page_token": None,
                 "sync_from": None} for index in range(3)]
    checked = []
    def sync(account_id, *args, **kwargs):
        checked.append(account_id)
        if account_id == accounts[0]["id"]:
            raise ProviderOperationError("Apple Mail timed out during incremental_metadata", kind="timeout")
        return SyncStepResult(processed=2, complete=True)
    schedule = {}
    refresh = RefreshRound(SimpleNamespace(sync=SimpleNamespace(sync_account_step=sync,
        retry_pending_originals=lambda *args, **kwargs: 0, paused=False)),
        lambda account_id: object(), accounts=accounts, schedule=schedule)
    for _ in range(20):
        result = refresh.step(lambda update: None)
        if result.get("done"):
            break
    assert result["done"]
    assert checked[:3] == [account["id"] for account in accounts]
    assert checked.count(accounts[0]["id"]) == 1
    assert result["failed"] == {accounts[0]["id"]: "timeout"}
    assert schedule[accounts[0]["id"]] == {"failures": 1, "due": 400.0, "error": "timeout"}
    assert schedule[accounts[1]["id"]] == {"failures": 0, "due": 400.0, "error": ""}


def test_two_timeout_failures_schedule_recovery_but_auth_errors_do_not(monkeypatch):
    scheduled = []
    monkeypatch.setattr(controller_module, "QTimer", SimpleNamespace(
        singleShot=lambda delay, callback: scheduled.append(callback)
    ))
    fake = SimpleNamespace(
        _worker_jobs={}, _sync_failures=0, _mail_timeout_failures=0,
        context=SimpleNamespace(settings=SimpleNamespace(sync_interval_seconds=300),
                                database=SimpleNamespace(execute=lambda *args: None)),
        _set_busy=lambda *args: None, _set_status=lambda *args: None,
        _set_activity=lambda **kwargs: None, reload=lambda: None,
        recoverAppleMail=lambda: None,
    )
    def fail(message):
        fake._worker_jobs["job"] = {"label": "Syncing all accounts…"}
        AppController._worker_failed(fake, "job", message)
    fail("Apple Mail timed out")
    assert not scheduled
    fail("Apple Mail timed out")
    assert scheduled == [fake.recoverAppleMail]
    scheduled.clear()
    fail("AuthenticationError: Allow Mail access")
    fail("Apple Mail timed out")
    assert not scheduled


@pytest.mark.parametrize("outcome", ["restarted", "approval_required", "drafts_open", "cooldown"])
def test_recovery_resumes_sync_only_after_success(monkeypatch, outcome):
    scheduled, notices, statuses = [], [], []
    monkeypatch.setattr(controller_module, "QTimer", SimpleNamespace(
        singleShot=lambda delay, callback: scheduled.append(callback)
    ))
    monkeypatch.setattr(controller_module, "recover_mail", lambda *args, **kwargs: outcome)
    fake = SimpleNamespace(
        providerBusy=False, _editor_draft={}, _assistant_visible=False, _providers={"old": object()},
        context=SimpleNamespace(database=SimpleNamespace(execute=lambda *args: None)),
        _set_status=statuses.append, _automatic_sync=lambda: None,
        mailRestartApprovalRequired=SimpleNamespace(emit=lambda: notices.append("ask")),
        _run=lambda label, work, done: done(work()),
    )
    AppController._recover_mail(fake, confirmed_force=False)
    assert bool(scheduled) == (outcome == "restarted")
    assert bool(notices) == (outcome == "approval_required")
    assert fake._pending_mail_restart == (outcome == "approval_required")
    if outcome == "restarted":
        assert not fake._providers


def test_recovery_never_interrupts_email_reader_editor():
    statuses = []
    fake = SimpleNamespace(
        providerBusy=False, _editor_draft={"body": "unsaved"}, _assistant_visible=False,
        _set_status=statuses.append,
    )
    AppController._recover_mail(fake, confirmed_force=True)
    assert "postponed" in statuses[0]
