"""No production mail: native launch invariants with fictional pipe payloads."""

import json
import os
import signal
import subprocess
import sys
from time import perf_counter

import pytest

from email_reader import process_launch

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Darwin fork-free spawn regression")


@pytest.fixture(autouse=True)
def forbid_fork(monkeypatch):
    def blocked(*_args, **_kwargs):
        pytest.fail("A macOS child launch reached an unsafe fork path")
    monkeypatch.setattr(subprocess, "_fork_exec", blocked)
    monkeypatch.setattr(os, "fork", blocked)


def python(code):
    return [sys.executable, "-c", code]


def test_direct_parent_identity_private_stdio_and_new_session():
    result = process_launch.run(python(
        "import os,sys,json; print(json.dumps([os.getppid(),os.getpid(),os.getsid(0),os.getpgrp(),sys.stdin.read()])); "
        "print('fictional diagnostic',file=sys.stderr)"),
        input="fictional secret only on stdin", capture_output=True, text=True,
        start_new_session=True, timeout=5, check=True)
    parent, pid, session, group, received = json.loads(result.stdout)
    assert parent == os.getpid()  # The signed Keychain helper must see the app, not a launcher.
    assert pid == session == group
    assert received == "fictional secret only on stdin"
    assert result.stderr == "fictional diagnostic\n"
    assert all("fictional secret" not in argument for argument in result.args)


def test_inheritable_native_descriptor_is_not_exposed_to_child(tmp_path):
    secret = tmp_path / "fictional-private-data"
    secret.write_bytes(b"fixture bytes")
    descriptor = os.open(secret, os.O_RDONLY)
    try:
        os.set_inheritable(descriptor, True)  # Simulates a non-Python native library.
        result = process_launch.run(python(
            "import os,json; "
            f"\ntry: os.fstat({descriptor}); print('leaked')"
            "\nexcept OSError: print('closed')"),
            capture_output=True, text=True, timeout=5, check=True)
        assert result.stdout.strip() == "closed"
        assert os.read(descriptor, 100) == b"fixture bytes"
    finally:
        os.close(descriptor)


def test_large_duplex_communicate_resumes_after_timeout():
    payload = "fictional text\n" * 160_000
    with process_launch.Popen(python(
        "import sys,time; time.sleep(.1); data=sys.stdin.read(); "
        "sys.stdout.write(data); sys.stderr.write('finished')"),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
        with pytest.raises(subprocess.TimeoutExpired):
            child.communicate(input=payload, timeout=.01)
        stdout, stderr = child.communicate(timeout=10)
        assert stdout == payload and stderr == "finished" and child.returncode == 0


def test_timeout_reaps_owned_child_promptly():
    started = perf_counter()
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        process_launch.run(python("import os,time; print(os.getpid(),flush=True); time.sleep(20)"),
                           capture_output=True, text=True, timeout=.2)
    assert perf_counter() - started < 2
    pid = int(raised.value.output.strip())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_owned_process_group_can_be_cancelled_without_parent_signal():
    with process_launch.Popen(python("import os,time; print(os.getpgrp(),flush=True); time.sleep(20)"),
                              stdout=subprocess.PIPE, text=True, start_new_session=True) as child:
        assert int(child.stdout.readline()) == child.pid != os.getpgrp()
        os.killpg(child.pid, signal.SIGTERM)
        child.communicate(timeout=2)
        assert child.returncode == -signal.SIGTERM


def test_errors_path_environment_and_no_unsafe_fallback(tmp_path):
    result = process_launch.run(["printf", "%s", "fictional"], capture_output=True, text=True,
                               env={"PATH": "/usr/bin:/bin"}, timeout=5)
    assert result.stdout == "fictional"
    with pytest.raises(subprocess.CalledProcessError) as raised:
        process_launch.run(python("import sys; print('fictional'); sys.exit(7)"),
                           capture_output=True, text=True, timeout=5, check=True)
    assert raised.value.returncode == 7 and raised.value.stdout == "fictional\n"
    with pytest.raises(FileNotFoundError):
        process_launch.run([str(tmp_path / "absent")])
    with pytest.raises(ValueError, match="unsafe fork"):
        process_launch.run(["/usr/bin/true"], cwd=tmp_path)
    with pytest.raises(ValueError, match="unsafe fork"):
        process_launch.run(["/usr/bin/true"], close_fds=False)


def test_failed_spawn_does_not_leak_pipe_descriptors(tmp_path):
    before = len(os.listdir("/dev/fd"))
    for _ in range(20):
        with pytest.raises(FileNotFoundError):
            process_launch.run([str(tmp_path / "absent")], input=b"fictional", capture_output=True)
    assert len(os.listdir("/dev/fd")) <= before


def test_borrowed_standard_stream_is_not_clobbered_by_redirection():
    # Nested fictional launcher borrows its stdout as the child's stderr while
    # collecting child stdout privately; descriptor 1 must be copied first.
    child_code = "import sys; print('OUT'); print('ERR',file=sys.stderr)"
    outer_code = (
        "import sys,subprocess; from email_reader.process_launch import run; "
        f"result=run([sys.executable,'-c',{child_code!r}],stdout=subprocess.PIPE,stderr=1,text=True); "
        "print('CAPTURE:'+result.stdout.strip())"
    )
    result = process_launch.run(python(outer_code), capture_output=True, text=True, timeout=5, check=True)
    assert set(result.stdout.splitlines()) == {"ERR", "CAPTURE:OUT"}
    assert result.stderr == ""
