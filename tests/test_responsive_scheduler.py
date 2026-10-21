from collections import Counter
from types import SimpleNamespace

import pytest

from email_reader.contracts import SyncStepResult
from email_reader.providers.base import AuthenticationError, ProviderOperationError
from email_reader.scheduling import RefreshRound


def accounts():
    return [{"id": str(i), "email": f"fictional{i}@example.com", "sync_from": "2020-01-01T00:00:00+00:00",
             "sync_cursor": "cursor", "sync_page_token": None} for i in range(3)]


def test_heads_and_account_pages_are_fair_and_repair_preserves_original_boundary():
    calls, counts = [], Counter()
    def step(identifier, provider, **kwargs):
        kind = kwargs["job_kind"]
        calls.append((identifier, kind, kwargs["since"]))
        counts[(identifier, kind)] += 1
        return SyncStepResult(complete=kind != "incremental" or counts[(identifier, kind)] == 2)
    sync = SimpleNamespace(paused=False, sync_account_step=step, retry_pending_originals=lambda *args, **kw: 0)
    round_ = RefreshRound(SimpleNamespace(sync=sync), lambda identifier: object(), accounts=accounts(), schedule={})
    while not round_.step(lambda value: None)["done"]:
        assert len(calls) < 25
    assert [(i, k) for i, k, _ in calls[:3]] == [(str(i), "freshness") for i in range(3)]
    assert [(i, k) for i, k, _ in calls[3:6]] == [(str(i), "incremental") for i in range(3)]
    assert all(since.year == 2020 for _, kind, since in calls if kind == "repair")


def test_disconnected_account_does_not_block_healthy_account_or_retry_immediately():
    calls, schedule = [], {}
    def step(identifier, provider, **kwargs):
        calls.append(identifier)
        if identifier == "0":
            raise ProviderOperationError("Apple Mail timed out during incremental_metadata", kind="timeout")
        return SyncStepResult(complete=True)
    sync = SimpleNamespace(paused=False, sync_account_step=step, retry_pending_originals=lambda *args, **kw: 0)
    round_ = RefreshRound(SimpleNamespace(sync=sync), lambda identifier: object(), accounts=accounts(), schedule=schedule)
    while not round_.step(lambda value: None)["done"]:
        assert len(calls) < 15
    assert calls[:3] == ["0", "1", "2"]
    assert calls.count("0") == 1
    assert schedule["0"]["error"] == "timeout"
    assert schedule["1"]["failures"] == schedule["2"]["failures"] == 0
    assert schedule["0"]["due"] >= round_.started + 300


@pytest.mark.parametrize("error", [
    RuntimeError("The private database is closed"),
    ValueError("Encrypted attachment has an invalid header"),
    RuntimeError("Apple Mail timed out, but this untyped failure is not trusted"),
    OSError("Disk is full"),
])
def test_unknown_local_and_security_errors_stop_instead_of_becoming_connection_failures(error):
    def step(*args, **kwargs):
        raise error
    invalidated = []
    round_ = RefreshRound(SimpleNamespace(sync=SimpleNamespace(paused=False, sync_account_step=step)),
        lambda identifier: object(), accounts=accounts(), schedule={}, invalidate_provider=invalidated.append)
    with pytest.raises(type(error)):
        round_.step(lambda value: None)
    assert not round_.failed and not invalidated


def test_provider_failures_invalidate_only_the_failed_account():
    invalidated = []
    def step(identifier, provider, **kwargs):
        if identifier == "0":
            raise AuthenticationError("Account needs authorization")
        return SyncStepResult(complete=True)
    round_ = RefreshRound(SimpleNamespace(sync=SimpleNamespace(paused=False, sync_account_step=step)),
        lambda identifier: object(), accounts=accounts(), schedule={}, invalidate_provider=invalidated.append)
    result = round_.step(lambda value: None)
    assert result["error"] == "permission" and invalidated == ["0"]
    assert round_.step(lambda value: None)["account"] == "1"


def test_failed_account_retries_when_due_during_another_accounts_long_repair(monkeypatch):
    clock, calls = [1000.0], []
    monkeypatch.setattr("email_reader.scheduling.time.monotonic", lambda: clock[0])
    def step(identifier, provider, **kwargs):
        calls.append((identifier, kwargs["job_kind"]))
        if identifier == "0" and clock[0] < 1300:
            raise ProviderOperationError("Apple Mail timed out", kind="timeout")
        return SyncStepResult(complete=kwargs["job_kind"] != "repair")
    sync = SimpleNamespace(paused=False, sync_account_step=step, retry_pending_originals=lambda *a, **kw: 0)
    round_ = RefreshRound(SimpleNamespace(sync=sync), lambda identifier: object(), accounts=accounts(), schedule={})
    for _ in range(16):
        round_.step(lambda value: None)
    assert calls.count(("0", "freshness")) == 1
    assert "0" in round_.failed
    clock[0] = 1301
    for _ in range(4):
        round_.step(lambda value: None)
    assert calls.count(("0", "freshness")) == 2
    assert "0" not in round_.failed
    assert any(identifier == "0" and kind == "incremental" for identifier, kind in calls)


def test_account_excluded_from_automatic_start_can_join_when_its_backoff_expires(monkeypatch):
    clock, calls = [1000.0], []
    monkeypatch.setattr("email_reader.scheduling.time.monotonic", lambda: clock[0])
    def step(identifier, provider, **kwargs):
        calls.append((identifier, kwargs["job_kind"]))
        return SyncStepResult(complete=kwargs["job_kind"] != "repair")
    sync = SimpleNamespace(paused=False, sync_account_step=step, retry_pending_originals=lambda *a, **kw: 0)
    round_ = RefreshRound(SimpleNamespace(sync=sync), lambda identifier: object(), accounts=accounts(),
        schedule={"0": {"due": 1300, "failures": 1, "error": "timeout"}}, automatic=True)
    for _ in range(10):
        round_.step(lambda value: None)
    assert all(identifier != "0" for identifier, _ in calls)
    clock[0] = 1301
    round_.step(lambda value: None)
    assert calls[-1] == ("0", "freshness")


def test_long_repair_checks_fresh_heads_without_replacing_repair_progress(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("email_reader.scheduling.time.monotonic", lambda: clock[0])
    calls = []
    def step(identifier, provider, **kwargs):
        calls.append((identifier, kwargs["job_kind"]))
        return SyncStepResult(complete=kwargs["job_kind"] != "repair")
    sync = SimpleNamespace(paused=False, sync_account_step=step, retry_pending_originals=lambda *a, **k: 0)
    round_ = RefreshRound(SimpleNamespace(sync=sync), lambda identifier: object(), accounts=accounts(), schedule={})
    for _ in range(12):
        round_.step(lambda value: None)
    assert calls[-1][1] == "repair"
    clock[0] += 301
    for _ in range(3):
        round_.step(lambda value: None)
    assert calls[-3:] == [(str(i), "freshness") for i in range(3)]
    round_.step(lambda value: None)
    assert calls[-1][1] == "repair"
