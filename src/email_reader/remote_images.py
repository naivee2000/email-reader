"""Explicit, cookie-free image loads with public-address pinning and bounded responses."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from urllib.parse import urljoin, urlsplit

from email_reader.image_types import raster_mime

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_BATCH_BYTES = 32 * 1024 * 1024
IMAGE_REQUEST_TIMEOUT = 5
IMAGE_TOTAL_SECONDS = 20


def public_addresses(host: str, port: int) -> list[tuple]:
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("Remote images must use public internet addresses")
    return addresses


def fetch_remote_image(url: str, *, cancel: threading.Event | None = None) -> bytes:
    deadline = time.monotonic() + IMAGE_TOTAL_SECONDS

    def check_active():
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Image loading cancelled")
        if time.monotonic() >= deadline:
            raise TimeoutError("Image loading deadline reached")

    for _ in range(4):
        check_active()
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or port not in {80, 443}
        ):
            raise ValueError("Unsupported remote image address")
        addresses = public_addresses(parsed.hostname, port)
        check_active()
        connection = http.client.HTTPConnection(parsed.hostname, port, timeout=IMAGE_REQUEST_TIMEOUT)
        sock = None
        try:
            # Connect to the already validated address, not a second DNS lookup.
            family, socktype, protocol, _, address = addresses[0]
            sock = socket.socket(family, socktype, protocol)
            sock.settimeout(IMAGE_REQUEST_TIMEOUT)
            sock.connect(address)
            if parsed.scheme == "https":
                sock = ssl.create_default_context().wrap_socket(
                    sock, server_hostname=parsed.hostname
                )
            connection.sock = sock
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            connection.request(
                "GET", target, headers={"Accept": "image/*", "User-Agent": "EmailReader/1"}
            )
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise ValueError("Image redirect has no destination")
                destination = urljoin(url, location)
                if parsed.scheme == "https" and urlsplit(destination).scheme != "https":
                    raise ValueError("Image redirect would downgrade security")
                url = destination
                continue
            if response.status != 200:
                raise ValueError("Image could not be loaded")
            content_type = response.getheader("Content-Type", "").split(";", 1)[0].lower()
            if content_type not in {
                "image/png",
                "image/jpeg",
                "image/gif",
                "image/webp",
                "image/bmp",
                "image/tiff",
                "image/x-icon",
            }:
                raise ValueError("Unsupported remote image format")
            pieces, received = [], 0
            while True:
                check_active()
                piece = response.read1(min(64 * 1024, MAX_IMAGE_BYTES + 1 - received))
                if not piece:
                    return b"".join(pieces)
                pieces.append(piece)
                received += len(piece)
                if received > MAX_IMAGE_BYTES:
                    raise ValueError("Remote image exceeds the size limit")
        finally:
            connection.close()
            if sock:
                sock.close()
    raise ValueError("Too many image redirects")


def fetch_remote_images(
    urls: Iterable[str],
    *,
    report: Callable[[dict], None],
    cancel: threading.Event,
    fetcher: Callable[[str], bytes] = fetch_remote_image,
    max_workers: int = 4,
) -> dict:
    """Fetch an explicit batch with bounded concurrency and cancellation."""
    targets = list(urls)
    loaded: dict[str, bytes] = {}
    failed = 0
    completed = 0
    loaded_bytes = 0
    if not targets or cancel.is_set():
        return {"loaded": loaded, "failed": 0, "cancelled": cancel.is_set()}
    concurrency = max(1, min(max_workers, len(targets)))
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        def fetch(url):
            if cancel.is_set():
                raise InterruptedError("Image loading cancelled")
            return fetcher(url, cancel=cancel) if fetcher is fetch_remote_image else fetcher(url)

        remaining = iter(targets)
        futures = {}

        def submit_next():
            url = next(remaining, None)
            if url is not None and not cancel.is_set():
                futures[executor.submit(fetch, url)] = url

        for _ in range(concurrency):
            submit_next()
        while futures:
            if cancel.is_set():
                for future in futures:
                    future.cancel()
                break
            ready, _ = wait(futures, timeout=0.05, return_when=FIRST_COMPLETED)
            for future in ready:
                if cancel.is_set():
                    break
                url = futures.pop(future)
                try:
                    content = future.result()
                    if raster_mime(content) and loaded_bytes + len(content) <= MAX_BATCH_BYTES:
                        loaded[url] = content
                        loaded_bytes += len(content)
                    else:
                        failed += 1
                except Exception:
                    failed += 1
                completed += 1
                report({"completed": completed, "total": len(targets)})
                submit_next()
    return {"loaded": loaded, "failed": failed, "cancelled": cancel.is_set()}
