import socket
import threading
import time
from time import perf_counter

import pytest

from email_reader.remote_images import fetch_remote_image, fetch_remote_images, public_addresses


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.1", "192.168.0.1", "169.254.169.254", "::1", "fc00::1"]
)
def test_local_and_private_image_targets_rejected_before_connection(monkeypatch, address):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))],
    )
    with pytest.raises(ValueError, match="public"):
        public_addresses("untrusted.example", 443)


@pytest.mark.parametrize(
    "url",
    [
        "file:///private/key",
        "ftp://example.com/image",
        "https://user:pass@example.com/image",
        "https://example.com:8080/image",
    ],
)
def test_unsupported_remote_urls_never_open_connections(url, monkeypatch):
    monkeypatch.setattr(
        socket, "socket", lambda *a, **kw: pytest.fail("Must reject before connecting")
    )
    with pytest.raises(ValueError):
        fetch_remote_image(url)


def test_mixed_public_private_dns_is_rejected(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ],
    )
    with pytest.raises(ValueError):
        public_addresses("untrusted.example", 443)


def test_twenty_remote_images_use_four_workers_and_beat_forty_percent_serial():
    lock = threading.Lock()
    active = 0
    peak = 0

    def fake_fetch(_url):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return b"\x89PNG\r\n\x1a\nfixture"

    urls = [f"https://example.test/{index}.png" for index in range(20)]
    serial_started = perf_counter()
    for url in urls:
        fake_fetch(url)
    serial_elapsed = perf_counter() - serial_started

    concurrent_started = perf_counter()
    progress = []
    result = fetch_remote_images(
        urls,
        report=progress.append,
        cancel=threading.Event(),
        fetcher=fake_fetch,
    )
    concurrent_elapsed = perf_counter() - concurrent_started

    assert len(result["loaded"]) == 20
    assert result["failed"] == 0
    assert peak == 4
    assert progress[-1] == {"completed": 20, "total": 20}
    assert concurrent_elapsed <= serial_elapsed * 0.40


def test_cancelled_batch_does_not_start_any_requests():
    cancel = threading.Event()
    cancel.set()
    result = fetch_remote_images(
        ["https://example.test/a.png"], report=lambda _: None, cancel=cancel,
        fetcher=lambda _: pytest.fail("Cancelled image requests must not start"),
    )
    assert result == {"loaded": {}, "failed": 0, "cancelled": True}


def test_cancel_mid_batch_does_not_drain_queued_downloads():
    cancel = threading.Event()
    calls = []

    def fetch(url):
        calls.append(url)
        cancel.set()
        return b"\x89PNG\r\n\x1a\nfixture"

    result = fetch_remote_images(
        [f"https://example.test/{index}.png" for index in range(30)],
        report=lambda _: None, cancel=cancel, fetcher=fetch,
    )
    assert 1 <= len(calls) <= 4
    assert result["cancelled"]


def test_batch_payload_is_byte_bounded(monkeypatch):
    content = b"\x89PNG\r\n\x1a\nfixture"
    monkeypatch.setattr("email_reader.remote_images.MAX_BATCH_BYTES", len(content) * 2)
    result = fetch_remote_images(
        [f"https://example.test/{index}.png" for index in range(10)],
        report=lambda _: None, cancel=threading.Event(), fetcher=lambda _: content,
    )
    assert len(result["loaded"]) == 2
    assert result["failed"] == 8
