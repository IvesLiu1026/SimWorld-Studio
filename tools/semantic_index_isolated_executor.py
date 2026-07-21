"""Bounded fork/process-group isolation for semantic-index phase callbacks.

The durable worker service must never trust a target callback to return before
the request deadline.  This module runs one already-selected callback in a
fresh session/process group, transfers only a detached request value and an
immutable tuple of capability bytes, and returns one bounded canonical JSON
object.  At every terminal edge the parent kills the complete process group
and reaps its leader before returning.

This is an offline-reviewed primitive, not a dispatcher or adapter registry.
It does not select code, discover configuration, open target connections, or
authorize a live operation.  Production wiring still has to pin the exact
callback source closure and run the worker as a single-threaded process.
"""

from __future__ import annotations

import ctypes
import dataclasses
import errno
import json
import math
import os
import select
import signal
import socket
import struct
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, NoReturn

import semantic_index_worker_protocol as worker_protocol


MAX_RESULT_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_CAPABILITIES = 3
MAX_CAPABILITY_BYTES = 16 * 1024
REAP_TIMEOUT_NS = 2_000_000_000
MAX_PROC_LIST_BYTES = 64 * 1024

_PR_SET_CHILD_SUBREAPER = 36
_PR_GET_CHILD_SUBREAPER = 37
_PR_SET_NO_NEW_PRIVS = 38
_LIBC = ctypes.CDLL(None, use_errno=True)
_LIBC.prctl.restype = ctypes.c_int

_READY = b"R"
_RESULT = b"V"
_FAILED = b"E"
_FRAME_HEADER = struct.Struct("!cI")

PUBLIC_ERROR_MESSAGES = {
    "ISOLATED_EXECUTOR_INPUT_INVALID": "Isolated executor input is invalid",
    "ISOLATED_EXECUTOR_UNSUPPORTED": "Isolated executor platform support is unavailable",
    "ISOLATED_EXECUTOR_THREADED": "Isolated executor requires a single-threaded worker",
    "ISOLATED_EXECUTOR_CHILDREN_PRESENT": "Isolated executor requires a worker with no pre-existing child processes",
    "ISOLATED_EXECUTOR_START_FAILED": "Isolated executor could not start",
    "ISOLATED_EXECUTOR_DEADLINE_EXCEEDED": "Isolated executor exceeded its absolute deadline",
    "ISOLATED_EXECUTOR_FAILED": "Isolated executor operation failed",
    "ISOLATED_EXECUTOR_RESULT_INVALID": "Isolated executor result is invalid",
    "ISOLATED_EXECUTOR_REAP_FAILED": "Isolated executor could not be reaped",
}


@dataclasses.dataclass(frozen=True, slots=True)
class IsolatedExecutorError(Exception):
    """Fixed, bounded failure that never retains callback-controlled text."""

    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def _fail(code: str) -> NoReturn:
    raise IsolatedExecutorError(code=code, message=PUBLIC_ERROR_MESSAGES[code])


def _platform_supported() -> bool:
    required = (
        "fork",
        "setsid",
        "killpg",
        "waitpid",
        "WNOHANG",
        "pidfd_open",
    )
    return (
        os.name == "posix"
        and sys.platform.startswith("linux")
        and all(hasattr(os, name) for name in required)
        and hasattr(signal, "pidfd_send_signal")
    )


def _prctl_set(option: int, value: int) -> None:
    try:
        result = _LIBC.prctl(option, value, 0, 0, 0)
    except (AttributeError, TypeError, ValueError):
        _fail("ISOLATED_EXECUTOR_UNSUPPORTED")
    if result != 0:
        _fail("ISOLATED_EXECUTOR_UNSUPPORTED")


def _subreaper_state() -> int:
    state = ctypes.c_int(-1)
    try:
        result = _LIBC.prctl(
            _PR_GET_CHILD_SUBREAPER,
            ctypes.byref(state),
            0,
            0,
            0,
        )
    except (AttributeError, TypeError, ValueError):
        _fail("ISOLATED_EXECUTOR_UNSUPPORTED")
    if result != 0 or state.value not in {0, 1}:
        _fail("ISOLATED_EXECUTOR_UNSUPPORTED")
    return state.value


def _read_fixed_proc_file(path: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        raw = os.read(descriptor, MAX_PROC_LIST_BYTES + 1)
        if len(raw) > MAX_PROC_LIST_BYTES or os.read(descriptor, 1):
            _fail("ISOLATED_EXECUTOR_UNSUPPORTED")
        return raw
    except IsolatedExecutorError:
        raise
    except (OSError, ValueError):
        _fail("ISOLATED_EXECUTOR_UNSUPPORTED")
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _direct_child_pids() -> tuple[int, ...]:
    raw = _read_fixed_proc_file(
        f"/proc/self/task/{os.getpid()}/children"
    )
    try:
        tokens = raw.decode("ascii", "strict").split()
        children = tuple(int(token, 10) for token in tokens)
    except (UnicodeError, ValueError):
        _fail("ISOLATED_EXECUTOR_UNSUPPORTED")
    if (
        len(children) != len(set(children))
        or any(child <= 0 for child in children)
    ):
        _fail("ISOLATED_EXECUTOR_UNSUPPORTED")
    return children


def _enable_subreaper() -> int:
    if _direct_child_pids():
        _fail("ISOLATED_EXECUTOR_CHILDREN_PRESENT")
    previous = _subreaper_state()
    if previous == 0:
        _prctl_set(_PR_SET_CHILD_SUBREAPER, 1)
        if _subreaper_state() != 1:
            _fail("ISOLATED_EXECUTOR_UNSUPPORTED")
    return previous


def _restore_subreaper(previous: int) -> None:
    if previous == 0:
        _prctl_set(_PR_SET_CHILD_SUBREAPER, 0)


def _close_inherited_descriptors(channel_fd: int) -> None:
    try:
        names = os.listdir("/proc/self/fd")
    except OSError:
        _fail("ISOLATED_EXECUTOR_UNSUPPORTED")
    for name in names:
        try:
            descriptor = int(name, 10)
        except ValueError:
            _fail("ISOLATED_EXECUTOR_UNSUPPORTED")
        if descriptor <= channel_fd:
            continue
        try:
            os.close(descriptor)
        except OSError as error:
            if error.errno != errno.EBADF:
                _fail("ISOLATED_EXECUTOR_START_FAILED")


def _validated_inputs(
    request: Mapping[str, Any],
    capabilities: Sequence[bytes],
    executor: Callable[[dict[str, Any], tuple[bytes, ...]], Mapping[str, Any]],
    deadline_monotonic_ns: int,
) -> tuple[dict[str, Any], tuple[bytes, ...]]:
    if not _platform_supported():
        _fail("ISOLATED_EXECUTOR_UNSUPPORTED")
    if threading.active_count() != 1:
        _fail("ISOLATED_EXECUTOR_THREADED")
    if type(request) is not dict or not callable(executor):
        _fail("ISOLATED_EXECUTOR_INPUT_INVALID")
    if type(deadline_monotonic_ns) is not int or deadline_monotonic_ns <= 0:
        _fail("ISOLATED_EXECUTOR_INPUT_INVALID")
    if deadline_monotonic_ns <= time.monotonic_ns():
        _fail("ISOLATED_EXECUTOR_DEADLINE_EXCEEDED")
    if type(capabilities) is not tuple or not 1 <= len(capabilities) <= MAX_CAPABILITIES:
        _fail("ISOLATED_EXECUTOR_INPUT_INVALID")
    exact_capabilities: list[bytes] = []
    for capability in capabilities:
        if (
            type(capability) is not bytes
            or not 16 <= len(capability) <= MAX_CAPABILITY_BYTES
        ):
            _fail("ISOLATED_EXECUTOR_INPUT_INVALID")
        exact_capabilities.append(capability)
    if len(set(exact_capabilities)) != len(exact_capabilities):
        _fail("ISOLATED_EXECUTOR_INPUT_INVALID")
    try:
        canonical = worker_protocol.canonical_json_bytes(request)
        if not canonical or len(canonical) > MAX_REQUEST_BYTES:
            _fail("ISOLATED_EXECUTOR_INPUT_INVALID")
        detached = json.loads(canonical.decode("utf-8"))
    except (UnicodeError, ValueError, TypeError, worker_protocol.WorkerProtocolError):
        _fail("ISOLATED_EXECUTOR_INPUT_INVALID")
    if type(detached) is not dict:
        _fail("ISOLATED_EXECUTOR_INPUT_INVALID")
    return detached, tuple(exact_capabilities)


def _remaining_timeout_ms(deadline_ns: int) -> int:
    remaining = deadline_ns - time.monotonic_ns()
    if remaining <= 0:
        _fail("ISOLATED_EXECUTOR_DEADLINE_EXCEEDED")
    return max(1, math.ceil(remaining / 1_000_000))


def _wait_readable(descriptor: int, deadline_ns: int) -> None:
    poller = select.poll()
    poller.register(descriptor, select.POLLIN | select.POLLHUP | select.POLLERR)
    while True:
        try:
            events = poller.poll(_remaining_timeout_ms(deadline_ns))
        except InterruptedError:
            continue
        if not events:
            _fail("ISOLATED_EXECUTOR_DEADLINE_EXCEEDED")
        return


def _read_exact(descriptor: int, count: int, deadline_ns: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total < count:
        _wait_readable(descriptor, deadline_ns)
        try:
            chunk = os.read(descriptor, count - total)
        except BlockingIOError:
            continue
        except InterruptedError:
            continue
        except OSError:
            _fail("ISOLATED_EXECUTOR_FAILED")
        if not chunk:
            _fail("ISOLATED_EXECUTOR_FAILED")
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _write_all(descriptor: int, raw: bytes) -> None:
    offset = 0
    while offset < len(raw):
        try:
            written = os.write(descriptor, raw[offset:])
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError(errno.EPIPE, "isolated executor channel closed")
        offset += written


def _child_channel(child_descriptor: int) -> int:
    """Move the result channel to fd 3 and close all other inherited fds."""

    channel_fd = 3
    if child_descriptor != channel_fd:
        os.dup2(child_descriptor, channel_fd, inheritable=False)
        os.close(child_descriptor)
    else:
        os.set_inheritable(channel_fd, False)

    null_fd = os.open("/dev/null", os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
    try:
        for descriptor in (0, 1, 2):
            os.dup2(null_fd, descriptor, inheritable=False)
    finally:
        if null_fd > channel_fd:
            os.close(null_fd)

    _close_inherited_descriptors(channel_fd)
    return channel_fd


def _child_main(
    descriptor: int,
    request: dict[str, Any],
    capabilities: tuple[bytes, ...],
    executor: Callable[[dict[str, Any], tuple[bytes, ...]], Mapping[str, Any]],
) -> NoReturn:
    try:
        _prctl_set(_PR_SET_NO_NEW_PRIVS, 1)
        os.setsid()
        channel = _child_channel(descriptor)
        _write_all(channel, _READY)
        result = executor(request, capabilities)
        if type(result) is not dict:
            raise ValueError("callback result is not an exact object")
        payload = worker_protocol.canonical_json_bytes(result)
        if not payload or len(payload) > MAX_RESULT_BYTES:
            raise ValueError("callback result exceeds the bounded frame")
        _write_all(channel, _FRAME_HEADER.pack(_RESULT, len(payload)) + payload)
        os.close(channel)
        os._exit(0)
    except BaseException:
        try:
            _write_all(descriptor if descriptor == 3 else 3, _FRAME_HEADER.pack(_FAILED, 0))
        except BaseException:
            pass
        os._exit(127)


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
        return
    except ProcessLookupError:
        pass
    except OSError as error:
        if error.errno not in {errno.ESRCH, errno.EPERM}:
            _fail("ISOLATED_EXECUTOR_REAP_FAILED")
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        _fail("ISOLATED_EXECUTOR_REAP_FAILED")


def _kill_pidfd(pid: int) -> None:
    descriptor = -1
    try:
        descriptor = os.pidfd_open(pid, 0)
        signal.pidfd_send_signal(descriptor, signal.SIGKILL, None, 0)
    except ProcessLookupError:
        return
    except (AttributeError, OSError, ValueError):
        _fail("ISOLATED_EXECUTOR_REAP_FAILED")
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _reap_exited_children(leader_pid: int, leader_reaped: bool) -> bool:
    while True:
        try:
            waited, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return leader_reaped
        except InterruptedError:
            continue
        except OSError:
            _fail("ISOLATED_EXECUTOR_REAP_FAILED")
        if waited == 0:
            return leader_reaped
        if waited == leader_pid:
            leader_reaped = True


def _kill_and_reap(pid: int) -> None:
    """Kill and reap the complete subreaper-owned descendant tree."""

    _kill_group(pid)
    deadline = time.monotonic_ns() + REAP_TIMEOUT_NS
    leader_reaped = False
    while True:
        leader_reaped = _reap_exited_children(pid, leader_reaped)
        children = _direct_child_pids()
        if leader_reaped and not children:
            return
        for child in children:
            _kill_pidfd(child)
        if time.monotonic_ns() >= deadline:
            _kill_group(pid)
            for child in _direct_child_pids():
                _kill_pidfd(child)
            _fail("ISOLATED_EXECUTOR_REAP_FAILED")
        time.sleep(0.001)


def _reject_trailing_bytes(descriptor: int) -> None:
    deadline = time.monotonic_ns() + REAP_TIMEOUT_NS
    poller = select.poll()
    poller.register(descriptor, select.POLLIN | select.POLLHUP | select.POLLERR)
    while True:
        try:
            chunk = os.read(descriptor, 1)
        except BlockingIOError:
            remaining = deadline - time.monotonic_ns()
            if remaining <= 0:
                _fail("ISOLATED_EXECUTOR_REAP_FAILED")
            try:
                events = poller.poll(max(1, math.ceil(remaining / 1_000_000)))
            except InterruptedError:
                continue
            if not events:
                _fail("ISOLATED_EXECUTOR_REAP_FAILED")
            continue
        except InterruptedError:
            continue
        except OSError:
            _fail("ISOLATED_EXECUTOR_RESULT_INVALID")

        if not chunk:
            return
        _fail("ISOLATED_EXECUTOR_RESULT_INVALID")


def execute_isolated(
    request: Mapping[str, Any],
    capabilities: Sequence[bytes],
    executor: Callable[[dict[str, Any], tuple[bytes, ...]], Mapping[str, Any]],
    *,
    deadline_monotonic_ns: int,
) -> dict[str, Any]:
    """Run one callback in a fresh process group and return one exact object."""

    detached, exact_capabilities = _validated_inputs(
        request,
        capabilities,
        executor,
        deadline_monotonic_ns,
    )
    try:
        parent_socket, child_socket = socket.socketpair(
            socket.AF_UNIX,
            socket.SOCK_STREAM,
        )
        parent_fd = parent_socket.detach()
        child_fd = child_socket.detach()
    except (AttributeError, OSError):
        _fail("ISOLATED_EXECUTOR_START_FAILED")
    os.set_inheritable(parent_fd, False)
    os.set_inheritable(child_fd, False)
    pid = -1
    reaped = False
    try:
        try:
            pid = os.fork()
        except OSError:
            _fail("ISOLATED_EXECUTOR_START_FAILED")
        if pid == 0:
            try:
                os.close(parent_fd)
            except OSError:
                pass
            _child_main(child_fd, detached, exact_capabilities, executor)

        os.close(child_fd)
        child_fd = -1
        os.set_blocking(parent_fd, False)
        ready = _read_exact(parent_fd, 1, deadline_monotonic_ns)
        if ready != _READY:
            _fail("ISOLATED_EXECUTOR_START_FAILED")
        header = _read_exact(parent_fd, _FRAME_HEADER.size, deadline_monotonic_ns)
        kind, size = _FRAME_HEADER.unpack(header)
        if kind == _FAILED:
            if size != 0:
                _fail("ISOLATED_EXECUTOR_RESULT_INVALID")
            _kill_and_reap(pid)
            reaped = True
            _reject_trailing_bytes(parent_fd)
            _fail("ISOLATED_EXECUTOR_FAILED")
        if kind != _RESULT or not 1 <= size <= MAX_RESULT_BYTES:
            _fail("ISOLATED_EXECUTOR_RESULT_INVALID")
        payload = _read_exact(parent_fd, size, deadline_monotonic_ns)
        _kill_and_reap(pid)
        reaped = True
        _reject_trailing_bytes(parent_fd)
        try:
            value = worker_protocol.decode_canonical_json(
                payload,
                maximum=MAX_RESULT_BYTES,
            )
        except worker_protocol.WorkerProtocolError:
            _fail("ISOLATED_EXECUTOR_RESULT_INVALID")
        if type(value) is not dict:
            _fail("ISOLATED_EXECUTOR_RESULT_INVALID")
        return value
    finally:
        if pid > 0 and not reaped:
            # Lifecycle integrity is stronger than preserving an earlier
            # callback/transport code: if the child cannot be reaped, surface
            # that fixed failure instead of pretending the boundary closed.
            _kill_and_reap(pid)
        for descriptor in (parent_fd, child_fd):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


__all__ = [
    "IsolatedExecutorError",
    "MAX_CAPABILITIES",
    "MAX_CAPABILITY_BYTES",
    "MAX_REQUEST_BYTES",
    "MAX_RESULT_BYTES",
    "PUBLIC_ERROR_MESSAGES",
    "REAP_TIMEOUT_NS",
    "execute_isolated",
]
