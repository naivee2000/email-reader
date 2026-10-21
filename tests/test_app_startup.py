import fcntl
import os
import sys
from types import SimpleNamespace

from email_reader.app import build_parser, provider_oauth_client_readiness, startup_error_message


def test_process_probe_dispatches_before_private_context_or_browser_setup(monkeypatch):
    from email_reader import app, browser_launch

    def unexpected(*_args, **_kwargs):
        raise AssertionError("Diagnostic reached normal application startup")

    monkeypatch.setitem(sys.modules, "email_reader.process_diagnostics", SimpleNamespace(run_probe=lambda: 0))
    monkeypatch.setattr(app, "build_parser", unexpected)
    monkeypatch.setattr(browser_launch, "register_browser_opener", unexpected)
    assert app.main(["--check-process-launch"]) == 0


def test_provider_setup_check_is_explicit_and_separate_from_sync():
    args = build_parser().parse_args(["--check-provider-setup"])
    assert args.check_provider_setup
    assert not args.sync_once
    assert not args.check_access


def test_provider_setup_check_reads_only_client_configuration():
    class RecordingVault:
        values = {
            "google-client-id": "private-google-id",
            "google-client-secret": "private-google-secret",
            "microsoft-client-id": "",
        }

        def __init__(self):
            self.names = []

        def get(self, name):
            self.names.append(name)
            return self.values.get(name)

    vault = RecordingVault()
    assert provider_oauth_client_readiness(vault) == (True, False)
    assert vault.names == ["google-client-id", "google-client-secret", "microsoft-client-id"]


def test_keychain_startup_failure_explains_one_time_migration_without_leaking_error():
    message = startup_error_message("Keychain locked; sensitive diagnostic")
    assert "stable identity" in message
    assert "Mac password" in message
    assert "sensitive diagnostic" not in message


def test_other_startup_failure_stays_private():
    message = startup_error_message("database included private path")
    assert message == "Email Reader could not open its private data. No email was changed."
    assert "private path" not in message


def test_runtime_cleanup_preserves_live_unknown_and_inaccessible_owners(tmp_path, monkeypatch):
    from email_reader.context import _clean_stale_runtime

    names = ["runtime-101", "runtime-102", "runtime-103", "runtime-unknown", "runtime-0"]
    for name in names:
        (tmp_path / name).mkdir()
    outside = tmp_path / "originals"
    outside.mkdir()
    (tmp_path / "runtime-104").symlink_to(outside, target_is_directory=True)
    checked = []

    def probe(pid, signal):
        assert signal == 0
        checked.append(pid)
        if pid == 102:
            raise ProcessLookupError()
        if pid == 103:
            raise PermissionError()

    monkeypatch.setattr("email_reader.context.os.kill", probe)
    _clean_stale_runtime(tmp_path)
    assert set(checked) == {101, 102, 103}
    assert not (tmp_path / "runtime-102").exists()
    assert all((tmp_path / name).exists() for name in names if name != "runtime-102")
    assert (tmp_path / "runtime-104").is_symlink() and outside.is_dir()


def test_runtime_cleanup_uses_held_lock_even_if_pid_is_reused(tmp_path, monkeypatch):
    from email_reader.context import _clean_stale_runtime

    private_runtime = tmp_path / "runtime-101-random"
    private_runtime.mkdir()
    (private_runtime / "document.pdf").write_bytes(b"private temporary original")
    fd = os.open(private_runtime / ".active.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(
        "email_reader.context.os.kill",
        lambda *_: (_ for _ in ()).throw(AssertionError("new runtime used a PID probe")),
    )
    try:
        _clean_stale_runtime(tmp_path)
        assert private_runtime.exists()
    finally:
        os.close(fd)
    _clean_stale_runtime(tmp_path)
    assert not private_runtime.exists()
