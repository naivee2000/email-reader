"""Private, killable local document extraction. No Keychain, provider or GUI access."""

from __future__ import annotations

import base64
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path

from email_reader import process_launch
from email_reader.extraction import AttachmentExtractor, ExtractionResult


class ExtractionCancelled(RuntimeError):
    pass


def worker_main() -> int:
    try:
        request = json.loads(sys.stdin.buffer.read())
        # OCR may require image files. The parent owns this private subtree and
        # removes it after success, timeout, cancellation or a native child crash.
        tempfile.tempdir = request["temporary_directory"]
        result = AttachmentExtractor().extract(
            str(request["filename"]), str(request["mime_type"]),
            base64.b64decode(request["content"], validate=True),
        )
        sys.stdout.write(json.dumps(asdict(result), ensure_ascii=False))
        return 0
    except Exception:
        # Do not print filenames, extracted text, paths or native error details.
        return 2


def extract_isolated(
    filename: str, mime_type: str, content: bytes, *, temp_root: Path,
    should_cancel=None, timeout: float = 60, command: list[str] | None = None,
) -> ExtractionResult:
    cancelled = should_cancel or (lambda: False)
    if cancelled():
        raise ExtractionCancelled("Local indexing was cancelled")
    if command is None:
        command = ([sys.executable, "--extract-worker"] if getattr(sys, "frozen", False)
                   else [sys.executable, "-m", "email_reader.extractor_worker"])
    with tempfile.TemporaryDirectory(prefix="extract-", dir=temp_root) as temporary:
        Path(temporary).chmod(0o700)
        request = json.dumps({"filename": filename, "mime_type": mime_type,
                              "content": base64.b64encode(content).decode("ascii"),
                              "temporary_directory": temporary})
        process = process_launch.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, start_new_session=True,
        )
        started = time.monotonic()
        pending_input = request
        try:
            while True:
                if cancelled():
                    raise ExtractionCancelled("Local indexing was cancelled")
                if time.monotonic() - started >= timeout:
                    raise TimeoutError("Local indexing timed out; the original is retained")
                try:
                    stdout, _ = process.communicate(input=pending_input, timeout=.1)
                    if cancelled():
                        raise ExtractionCancelled("Local indexing was cancelled")
                    if process.returncode:
                        raise RuntimeError("Local indexing stopped; the original is retained")
                    result = json.loads(stdout)
                    if (not isinstance(result.get("text"), str)
                            or not isinstance(result.get("metadata"), dict)
                            or not isinstance(result.get("warnings"), list)):
                        raise RuntimeError("Local indexing returned an invalid result")
                    return ExtractionResult(result["text"], result["metadata"], result["warnings"])
                except subprocess.TimeoutExpired:
                    pending_input = None
        finally:
            if process.poll() is None:
                # Only terminate the process group created by this operation.
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()


if __name__ == "__main__":
    raise SystemExit(worker_main())
