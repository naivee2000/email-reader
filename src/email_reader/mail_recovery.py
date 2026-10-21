"""Bounded Apple Mail recovery; never reads draft text or sends messages."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from email_reader import process_launch
from email_reader.providers.apple_mail import _run_applescript
from email_reader.providers.base import AuthenticationError

LAST_RECOVERY_KEY = "apple_mail_last_recovery_attempt"
RECOVERY_COOLDOWN = timedelta(hours=1)


def is_mail_timeout(message: str) -> bool:
    return "Apple Mail timed out" in message or "Apple Mail did not respond before" in message


class NativeMail:
    """Target only the system Mail app, never broad process-name matching."""

    @staticmethod
    def applications():
        from AppKit import NSRunningApplication

        return list(NSRunningApplication.runningApplicationsWithBundleIdentifier_("com.apple.mail"))

    @staticmethod
    def open_drafts() -> int:
        result = _run_applescript(
            'tell application id "com.apple.mail" to return count of outgoing messages', timeout=5
        )
        if type(result) is not int or result < 0:
            raise RuntimeError("Apple Mail draft safety could not be checked")
        return result

    @staticmethod
    def reopen() -> None:
        result = process_launch.run(
            ["/usr/bin/open", "-g", "-b", "com.apple.mail"],
            capture_output=True, timeout=10, check=False,
        )
        if result.returncode:
            raise RuntimeError("Apple Mail could not be reopened; please open Mail manually")

    @staticmethod
    def wait_closed(apps, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if all(app.isTerminated() for app in apps):
                return True
            time.sleep(0.2)
        return all(app.isTerminated() for app in apps)


def recover_mail(context, *, confirmed_force: bool = False, native=None, now=None) -> str:
    """Return restarted/cooldown/drafts_open/approval_required.

    Unknown draft state never authorizes a force quit. An explicit one-time
    confirmation can override that uncertainty, but not a known open draft.
    Persist the attempt before native calls so reopening Email Reader cannot
    create a restart loop. Do not touch account data, credentials or cursors.
    """
    native = native or NativeMail()
    now = now or datetime.now(UTC)
    last = context.database.scalar(
        "SELECT value FROM app_settings WHERE key=?", (LAST_RECOVERY_KEY,), default=""
    )
    if last and not confirmed_force:
        try:
            age = now - datetime.fromisoformat(last).astimezone(UTC)
            if age < RECOVERY_COOLDOWN:
                return "cooldown"
        except (ValueError, TypeError):
            pass
    context.save_setting(LAST_RECOVERY_KEY, now.isoformat())
    apps = native.applications()
    if not apps:
        native.reopen()
        return "restarted"
    try:
        drafts = native.open_drafts()
    except AuthenticationError:
        raise
    except RuntimeError:
        if not confirmed_force:
            return "approval_required"
    else:
        if drafts:
            return "drafts_open"
    for app in apps:
        app.terminate()  # Normal quit first; Mail may save its own state.
    if not native.wait_closed(apps, 5):
        # A quit may be waiting on a compose/save dialog. Without explicit
        # force permission, check again rather than discarding that dialog.
        if not confirmed_force:
            try:
                if native.open_drafts():
                    return "drafts_open"
            except AuthenticationError:
                raise
            except RuntimeError:
                return "approval_required"
        for app in apps:
            if not app.isTerminated():
                app.forceTerminate()
        if not native.wait_closed(apps, 5):
            raise RuntimeError("Apple Mail could not be restarted; please quit Mail manually")
    native.reopen()
    return "restarted"
