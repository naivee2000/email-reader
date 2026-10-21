"""Fork-free child processes for the multithreaded macOS application.

CPython's Popen falls back to fork on macOS for close_fds/session requests.
Darwin's spawn CLOEXEC_DEFAULT flag closes unrelated descriptors atomically,
including inheritable descriptors opened concurrently by native libraries.
We retain Popen's pipe/communicate/wait implementation but never its macOS
fork path. No trampoline is used: the signed app remains the direct parent.
"""

from __future__ import annotations

import ctypes
import errno
import os
import shutil
import signal
import subprocess
import sys
from contextlib import contextmanager
from functools import lru_cache

# Public Darwin SDK sys/spawn.h flags, present since macOS 10.7 / 10.15.
_CLOEXEC_DEFAULT = 0x4000
_SETSID = 0x0400
_SETPGROUP = 0x0002
_SETSIGDEF = 0x0004


@lru_cache(maxsize=1)
def _libsystem():
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    opaque = ctypes.POINTER(ctypes.c_void_p)
    signatures = {
        "posix_spawn_file_actions_init": [opaque],
        "posix_spawn_file_actions_destroy": [opaque],
        "posix_spawn_file_actions_adddup2": [opaque, ctypes.c_int, ctypes.c_int],
        "posix_spawn_file_actions_addinherit_np": [opaque, ctypes.c_int],
        "posix_spawnattr_init": [opaque],
        "posix_spawnattr_destroy": [opaque],
        "posix_spawnattr_setflags": [opaque, ctypes.c_short],
        "posix_spawnattr_setpgroup": [opaque, ctypes.c_int],
        "posix_spawnattr_setsigdefault": [opaque, ctypes.POINTER(ctypes.c_uint32)],
        "posix_spawn": [ctypes.POINTER(ctypes.c_int), ctypes.c_char_p, opaque, opaque,
                        ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_char_p)],
    }
    for name, arguments in signatures.items():
        function = getattr(library, name)
        function.argtypes = arguments
        function.restype = ctypes.c_int
    return library


def _checked(code: int) -> None:
    if code:
        raise OSError(code, os.strerror(code))


def _cstring(value) -> bytes:
    value = os.fsencode(value)
    if b"\0" in value:
        raise ValueError("A process argument contains a null byte")
    return value


@contextmanager
def _safe_stdio(stdio):
    # Preserve a borrowed/low-numbered source before another action replaces
    # that standard descriptor (for example stdout=PIPE with stderr=1).
    import fcntl

    sources, duplicates = list(stdio), []
    try:
        for destination, source in enumerate(sources):
            if 0 <= source <= 2 and source != destination:
                duplicate = fcntl.fcntl(source, fcntl.F_DUPFD_CLOEXEC, 3)
                duplicates.append(duplicate)
                sources[destination] = duplicate
        yield tuple(sources)
    finally:
        for descriptor in duplicates:
            os.close(descriptor)


def _darwin_spawn(executable, args, env, stdio, *, restore_signals=True,
                  start_new_session=False, process_group=-1) -> int:
    """Direct native spawn, with only explicitly configured stdio inherited."""
    library = _libsystem()
    environment = dict(os.environ if env is None else env)
    executable = os.fspath(executable)
    if not os.path.dirname(executable):
        executable = shutil.which(executable, path=os.pathsep.join(os.get_exec_path(environment)))
        if executable is None:
            raise FileNotFoundError(errno.ENOENT, "Executable was not found")
    encoded_args = [_cstring(value) for value in args]
    encoded_env = []
    for key, value in environment.items():
        key = _cstring(key)
        if not key or b"=" in key:
            raise ValueError("Invalid environment variable name")
        encoded_env.append(key + b"=" + _cstring(value))
    argv = (ctypes.c_char_p * (len(encoded_args) + 1))(*encoded_args, None)
    envp = (ctypes.c_char_p * (len(encoded_env) + 1))(*encoded_env, None)
    actions, attributes = ctypes.c_void_p(), ctypes.c_void_p()
    _checked(library.posix_spawn_file_actions_init(ctypes.byref(actions)))
    try:
        _checked(library.posix_spawnattr_init(ctypes.byref(attributes)))
        try:
            for destination, source in enumerate(stdio):
                if source == -1:
                    # An inherited standard stream can legitimately be closed.
                    try:
                        os.fstat(destination)
                    except OSError as error:
                        if error.errno == errno.EBADF:
                            continue
                        raise
                    source = destination
                if source == destination:
                    _checked(library.posix_spawn_file_actions_addinherit_np(ctypes.byref(actions), source))
                else:
                    _checked(library.posix_spawn_file_actions_adddup2(ctypes.byref(actions), source, destination))
            flags = _CLOEXEC_DEFAULT
            if start_new_session:
                flags |= _SETSID
            if process_group != -1:
                flags |= _SETPGROUP
                _checked(library.posix_spawnattr_setpgroup(ctypes.byref(attributes), process_group))
            if restore_signals:
                bits = 0
                for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"):
                    number = getattr(signal, name, None)
                    if number is not None:
                        bits |= 1 << (number - 1)
                default_signals = ctypes.c_uint32(bits)  # Darwin sigset_t.
                _checked(library.posix_spawnattr_setsigdefault(ctypes.byref(attributes), ctypes.byref(default_signals)))
                flags |= _SETSIGDEF
            _checked(library.posix_spawnattr_setflags(ctypes.byref(attributes), flags))
            child = ctypes.c_int()
            _checked(library.posix_spawn(ctypes.byref(child), _cstring(executable), ctypes.byref(actions),
                                        ctypes.byref(attributes), argv, envp))
            return child.value
        finally:
            library.posix_spawnattr_destroy(ctypes.byref(attributes))
    finally:
        library.posix_spawn_file_actions_destroy(ctypes.byref(actions))


class Popen(subprocess.Popen):
    """Popen-compatible private pipes; unsupported macOS options fail closed.

    This isolated override matches CPython 3.12–3.14's POSIX hook. A changed
    interpreter signature fails explicitly instead of silently using fork.
    """

    def _execute_child(self, args, executable, preexec_fn, close_fds, pass_fds, cwd, env,
                       startupinfo, creationflags, shell, p2cread, p2cwrite, c2pread, c2pwrite,
                       errread, errwrite, restore_signals, gid, gids, uid, umask,
                       start_new_session, process_group):
        if sys.platform != "darwin":
            return super()._execute_child(
                args, executable, preexec_fn, close_fds, pass_fds, cwd, env, startupinfo, creationflags,
                shell, p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite, restore_signals,
                gid, gids, uid, umask, start_new_session, process_group)
        if (preexec_fn is not None or pass_fds or cwd is not None or shell
                or gid is not None or gids is not None or uid is not None or umask >= 0
                or not close_fds):
            raise ValueError("Unsupported child-process option; unsafe fork fallback is disabled")
        args = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
        if not args:
            raise ValueError("An executable is required")
        executable = executable or args[0]
        sys.audit("subprocess.Popen", executable, args, cwd, env)
        with _safe_stdio((p2cread, c2pwrite, errwrite)) as stdio:
            self.pid = _darwin_spawn(executable, args, env, stdio,
                                     restore_signals=restore_signals, start_new_session=start_new_session,
                                     process_group=process_group)
        self._child_created = True
        self._close_pipe_fds(p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite)


def run(*popenargs, input=None, capture_output=False, timeout=None, check=False, **kwargs):
    """The subprocess.run contract, using this module's fork-free Popen."""
    if input is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used")
        kwargs["stdin"] = subprocess.PIPE
    if capture_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("stdout/stderr and capture_output may not both be used")
        kwargs["stdout"] = kwargs["stderr"] = subprocess.PIPE
    with Popen(*popenargs, **kwargs) as process:
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise
        except BaseException:
            process.kill()
            raise
        if check and process.returncode:
            raise subprocess.CalledProcessError(process.returncode, process.args, output=stdout, stderr=stderr)
        return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
