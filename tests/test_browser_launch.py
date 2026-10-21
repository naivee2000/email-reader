from __future__ import annotations

import subprocess
import webbrowser

import pytest

from email_reader import browser_launch


@pytest.mark.parametrize("url", [
    "https://accounts.example.invalid/authorize?state=fictional-private-state",
    "http://localhost:8123/callback?code=fictional",
])
def test_browser_opener_uses_private_fork_free_launcher(monkeypatch, url):
    calls = []

    def run(command, **options):
        calls.append((command, options))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(browser_launch.process_launch, "run", run)
    assert browser_launch.AppBrowser().open(url, new=1, autoraise=True)
    assert calls == [(["/usr/bin/open", url], {
        "capture_output": True, "timeout": 10, "check": False,
    })]


@pytest.mark.parametrize("url", [
    "file:///private/fictional", "mailto:fictional@example.invalid", "javascript:alert(1)",
    "https://example.invalid/\nprivate", "https://example.invalid/\x00private",
    "https://user:secret@example.invalid", "https://", "https://[invalid",
    "https://example.invalid:not-a-port", "--help", None,
])
def test_browser_opener_rejects_non_web_or_malformed_urls(monkeypatch, url):
    def forbidden(*args, **kwargs):
        raise AssertionError("An invalid URL must never start a process")

    monkeypatch.setattr(browser_launch.process_launch, "run", forbidden)
    with pytest.raises(webbrowser.Error, match="unsupported address"):
        browser_launch.AppBrowser().open(url)


@pytest.mark.parametrize("failure", [
    OSError("private process details"),
    subprocess.TimeoutExpired(["private URL"], 10, stderr="private data"),
])
def test_browser_failure_stops_fallback_without_private_output(monkeypatch, capsys, failure):
    def run(*args, **kwargs):
        raise failure

    monkeypatch.setattr(browser_launch.process_launch, "run", run)
    with pytest.raises(webbrowser.Error, match="^The sign-in page could not be opened$"):
        browser_launch.AppBrowser().open("https://example.invalid")
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_registration_changes_only_process_browser_choice(monkeypatch):
    monkeypatch.setattr(browser_launch.sys, "platform", "darwin")
    monkeypatch.setattr(webbrowser, "_browsers", {})
    monkeypatch.setattr(webbrowser, "_tryorder", [])
    browser_launch.register_browser_opener()
    assert isinstance(webbrowser.get(), browser_launch.AppBrowser)
    assert isinstance(webbrowser.get(None), browser_launch.AppBrowser)


def test_default_browser_failure_never_falls_back_to_fork(monkeypatch):
    monkeypatch.setattr(browser_launch.sys, "platform", "darwin")
    monkeypatch.setattr(webbrowser, "_browsers", {})
    monkeypatch.setattr(webbrowser, "_tryorder", [])

    class UnsafeFallback(webbrowser.BaseBrowser):
        def open(self, *args, **kwargs):
            raise AssertionError("The legacy fork-based browser was called")

    webbrowser.register("unsafe-fixture", None, UnsafeFallback())
    browser_launch.register_browser_opener()
    monkeypatch.setattr(browser_launch.process_launch, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 1))
    with pytest.raises(webbrowser.Error, match="could not be opened"):
        webbrowser.open("https://example.invalid")
