from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from email_reader.mail_recovery import LAST_RECOVERY_KEY, is_mail_timeout, recover_mail
from email_reader.providers.base import AuthenticationError


class FakeMail:
    def __init__(self, drafts=0, hung=False, running=True):
        self.drafts = drafts
        self.hung = hung
        self.running = running
        self.calls = []

    def applications(self):
        return [self] if self.running else []

    def open_drafts(self):
        self.calls.append("check_drafts")
        if isinstance(self.drafts, Exception):
            raise self.drafts
        return self.drafts

    def terminate(self):
        self.calls.append("quit")
        if not self.hung:
            self.running = False

    def forceTerminate(self):
        self.calls.append("force_quit")
        self.running = False

    def isTerminated(self):
        return not self.running

    def wait_closed(self, apps, seconds):
        return not self.running

    def reopen(self):
        self.calls.append("reopen")


@pytest.fixture
def recovery_context(database):
    def save(key, value):
        database.execute(
            "INSERT INTO app_settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value)
        )
    return SimpleNamespace(database=database, save_setting=save)


def test_normal_restart_does_not_force_quit(recovery_context):
    native = FakeMail()
    assert recover_mail(recovery_context, native=native) == "restarted"
    assert native.calls == ["check_drafts", "quit", "reopen"]


def test_force_quit_only_after_second_draft_check(recovery_context):
    native = FakeMail(hung=True)
    assert recover_mail(recovery_context, native=native) == "restarted"
    assert native.calls == ["check_drafts", "quit", "check_drafts", "force_quit", "reopen"]


@pytest.mark.parametrize("confirmed", [False, True])
def test_known_open_draft_never_terminated(recovery_context, confirmed):
    native = FakeMail(drafts=1, hung=True)
    assert recover_mail(recovery_context, native=native, confirmed_force=confirmed) == "drafts_open"
    assert native.calls == ["check_drafts"]


def test_frozen_unknown_drafts_require_confirmation(recovery_context):
    native = FakeMail(drafts=RuntimeError("timeout"), hung=True)
    assert recover_mail(recovery_context, native=native) == "approval_required"
    assert native.calls == ["check_drafts"]
    assert recover_mail(recovery_context, native=native, confirmed_force=True) == "restarted"
    assert native.calls == ["check_drafts", "check_drafts", "quit", "force_quit", "reopen"]


def test_permission_error_never_forces_mail(recovery_context):
    native = FakeMail(drafts=AuthenticationError("Permission required"), hung=True)
    with pytest.raises(AuthenticationError):
        recover_mail(recovery_context, native=native, confirmed_force=True)
    assert native.calls == ["check_drafts"]


def test_cooldown_persists_across_recovery_instances(recovery_context):
    now = datetime.now(UTC)
    first = FakeMail()
    assert recover_mail(recovery_context, native=first, now=now) == "restarted"
    second = FakeMail()
    assert recover_mail(recovery_context, native=second, now=now + timedelta(minutes=59)) == "cooldown"
    assert not second.calls
    assert recover_mail(recovery_context, native=second, now=now - timedelta(minutes=1)) == "cooldown"
    assert recover_mail(recovery_context, native=second, now=now + timedelta(hours=1)) == "restarted"
    assert recovery_context.database.scalar(
        "SELECT value FROM app_settings WHERE key=?", (LAST_RECOVERY_KEY,)
    )


def test_closed_mail_is_reopened_without_termination(recovery_context):
    native = FakeMail(running=False)
    assert recover_mail(recovery_context, native=native) == "restarted"
    assert native.calls == ["reopen"]


def test_failed_force_quit_does_not_launch_second_instance(recovery_context):
    native = FakeMail(hung=True)
    native.forceTerminate = lambda: native.calls.append("force_failed")
    with pytest.raises(RuntimeError, match="could not be restarted"):
        recover_mail(recovery_context, native=native)
    assert "reopen" not in native.calls


def test_only_mail_timeouts_trigger_recovery():
    assert is_mail_timeout("RuntimeError: Apple Mail timed out for account(s) 1")
    assert is_mail_timeout("Apple Mail did not respond before the operation timed out")
    for text in ["AuthenticationError", "Google request timed out", "No internet", "OperationCancelled"]:
        assert not is_mail_timeout(text)
