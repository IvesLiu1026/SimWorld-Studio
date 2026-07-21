from __future__ import annotations

import os
import pathlib
import resource
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


TESTS_DIR = pathlib.Path(__file__).resolve().parent
TOOLS_DIR = TESTS_DIR.parent
sys.path.insert(0, str(TOOLS_DIR))

import semantic_index_isolated_executor as isolated  # noqa: E402


CAPABILITIES = (b"postgres-capability-0001",)


def success_executor(request: dict, capabilities: tuple[bytes, ...]) -> dict:
    return {
        "capability_count": len(capabilities),
        "child_pid": os.getpid(),
        "child_pgid": os.getpgrp(),
        "request_id": request["request_id"],
    }


def sleeping_executor(_request: dict, _capabilities: tuple[bytes, ...]) -> dict:
    time.sleep(60)
    return {"unreachable": True}


def exploding_executor(_request: dict, _capabilities: tuple[bytes, ...]) -> dict:
    raise RuntimeError("callback-secret-must-not-cross-boundary")


def invalid_result_executor(_request: dict, _capabilities: tuple[bytes, ...]):
    return ["not", "an", "object"]


def oversized_executor(_request: dict, _capabilities: tuple[bytes, ...]) -> dict:
    return {"blob": "x" * isolated.MAX_RESULT_BYTES}


class IsolatedExecutorTests(unittest.TestCase):
    def request(self) -> dict:
        return {
            "request_id": "request-001",
            "nested": {"items": [1, 2, 3]},
        }

    def execute(self, executor, *, timeout_ms: int = 2_000) -> dict:
        return isolated.execute_isolated(
            self.request(),
            CAPABILITIES,
            executor,
            deadline_monotonic_ns=time.monotonic_ns() + timeout_ms * 1_000_000,
        )

    def assert_error(self, code: str, callback) -> isolated.IsolatedExecutorError:
        with self.assertRaises(isolated.IsolatedExecutorError) as caught:
            callback()
        self.assertEqual(code, caught.exception.code)
        self.assertEqual(
            isolated.PUBLIC_ERROR_MESSAGES[code],
            caught.exception.message,
        )
        return caught.exception

    def test_success_runs_in_a_distinct_session_process_group(self) -> None:
        result = self.execute(success_executor)
        self.assertEqual("request-001", result["request_id"])
        self.assertEqual(1, result["capability_count"])
        self.assertEqual(result["child_pid"], result["child_pgid"])
        self.assertNotEqual(os.getpid(), result["child_pid"])
        self.assertNotEqual(os.getpgrp(), result["child_pgid"])

    def test_request_mutation_in_child_cannot_change_parent_value(self) -> None:
        request = self.request()

        def mutate(value, _capabilities):
            value["nested"]["items"].append(4)
            return {"observed": value["nested"]["items"]}

        result = isolated.execute_isolated(
            request,
            CAPABILITIES,
            mutate,
            deadline_monotonic_ns=time.monotonic_ns() + 2_000_000_000,
        )
        self.assertEqual([1, 2, 3, 4], result["observed"])
        self.assertEqual([1, 2, 3], request["nested"]["items"])

    def test_absolute_deadline_kills_and_reaps_the_child(self) -> None:
        started = time.monotonic()
        self.assert_error(
            "ISOLATED_EXECUTOR_DEADLINE_EXCEEDED",
            lambda: self.execute(sleeping_executor, timeout_ms=120),
        )
        self.assertLess(time.monotonic() - started, 3.0)

    def test_callback_exception_is_fixed_and_does_not_leak_text(self) -> None:
        error = self.assert_error(
            "ISOLATED_EXECUTOR_FAILED",
            lambda: self.execute(exploding_executor),
        )
        self.assertNotIn("callback-secret", str(error))

    def test_non_object_and_oversized_results_fail_closed(self) -> None:
        self.assert_error(
            "ISOLATED_EXECUTOR_FAILED",
            lambda: self.execute(invalid_result_executor),
        )
        self.assert_error(
            "ISOLATED_EXECUTOR_FAILED",
            lambda: self.execute(oversized_executor),
        )

    def test_invalid_input_never_forks(self) -> None:
        deadline = time.monotonic_ns() + 1_000_000_000
        for request, capabilities in (
            ({"request_id": "request-001"}, [CAPABILITIES[0]]),
            ({"request_id": "request-001"}, (b"short",)),
            ({"request_id": "request-001"}, (CAPABILITIES[0], CAPABILITIES[0])),
        ):
            with self.subTest(capabilities=capabilities):
                self.assert_error(
                    "ISOLATED_EXECUTOR_INPUT_INVALID",
                    lambda request=request, capabilities=capabilities: (
                        isolated.execute_isolated(
                            request,
                            capabilities,
                            success_executor,
                            deadline_monotonic_ns=deadline,
                        )
                    ),
                )

    def test_expired_deadline_fails_before_fork(self) -> None:
        self.assert_error(
            "ISOLATED_EXECUTOR_DEADLINE_EXCEEDED",
            lambda: isolated.execute_isolated(
                self.request(),
                CAPABILITIES,
                success_executor,
                deadline_monotonic_ns=time.monotonic_ns(),
            ),
        )

    def test_multithreaded_parent_fails_closed_before_fork(self) -> None:
        ready = threading.Event()
        stop = threading.Event()

        def holder() -> None:
            ready.set()
            stop.wait(timeout=5)

        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(ready.wait(timeout=1))
        try:
            self.assert_error(
                "ISOLATED_EXECUTOR_THREADED",
                lambda: self.execute(success_executor),
            )
        finally:
            stop.set()
            thread.join(timeout=1)

    def test_success_path_kills_background_descendant_group_member(self) -> None:
        if not pathlib.Path("/proc").is_dir():
            self.skipTest("Linux /proc is required for descendant liveness evidence")
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = pathlib.Path(temporary) / "descendant.pid"

            def spawn_descendant(_request, _capabilities):
                child = os.fork()
                if child == 0:
                    time.sleep(60)
                    os._exit(0)
                pid_file.write_text(str(child), encoding="ascii")
                return {"descendant_pid": child}

            result = self.execute(spawn_descendant)
            descendant = result["descendant_pid"]
            self.assertEqual(str(descendant), pid_file.read_text(encoding="ascii"))
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                stat_path = pathlib.Path(f"/proc/{descendant}/stat")
                if not stat_path.exists():
                    break
                fields = stat_path.read_text(encoding="ascii").split()
                if len(fields) >= 3 and fields[2] == "Z":
                    break
                time.sleep(0.01)
            else:
                self.fail("isolated descendant remained runnable after result")

    def test_success_path_kills_descendant_that_escapes_process_group(self) -> None:
        if not pathlib.Path("/proc").is_dir():
            self.skipTest("Linux /proc is required for descendant liveness evidence")
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = pathlib.Path(temporary) / "escaped-descendant.pid"

            def spawn_escaped_descendant(_request, _capabilities):
                child = os.fork()
                if child == 0:
                    os.setsid()
                    time.sleep(60)
                    os._exit(0)
                pid_file.write_text(str(child), encoding="ascii")
                return {"descendant_pid": child}

            result = self.execute(spawn_escaped_descendant)
            descendant = result["descendant_pid"]
            self.assertEqual(str(descendant), pid_file.read_text(encoding="ascii"))
            self.assertFalse(pathlib.Path(f"/proc/{descendant}").exists())

    def test_child_closes_high_numbered_inherited_descriptor(self) -> None:
        soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
        high_descriptor = min(4096, hard_limit - 1)
        if high_descriptor < 2048:
            self.skipTest("process descriptor hard limit is too low for high-fd coverage")
        if soft_limit <= high_descriptor:
            resource.setrlimit(
                resource.RLIMIT_NOFILE,
                (high_descriptor + 1, hard_limit),
            )
        inherited = os.open("/dev/null", os.O_RDONLY)
        try:
            os.dup2(inherited, high_descriptor, inheritable=True)

            def inspect_descriptor(_request, _capabilities):
                try:
                    os.fstat(high_descriptor)
                except OSError:
                    return {"inherited": False}
                return {"inherited": True}

            self.assertEqual(
                {"inherited": False},
                self.execute(inspect_descriptor),
            )
        finally:
            os.close(inherited)
            try:
                os.close(high_descriptor)
            except OSError:
                pass
            if soft_limit <= high_descriptor:
                resource.setrlimit(
                    resource.RLIMIT_NOFILE,
                    (soft_limit, hard_limit),
                )

    def test_trailing_eagain_waits_boundedly_for_descendant_fd_close(self) -> None:
        poller = mock.Mock()
        poller.poll.return_value = [(123, isolated.select.POLLHUP)]
        with (
            mock.patch.object(isolated.select, "poll", return_value=poller),
            mock.patch.object(
                isolated.os,
                "read",
                side_effect=[BlockingIOError(), b""],
            ),
        ):
            isolated._reject_trailing_bytes(123)
        poller.register.assert_called_once()
        poller.poll.assert_called_once()


if __name__ == "__main__":
    unittest.main()
