from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Sequence, TextIO

from .state import (
    PublicationSnapshot,
    PullRequestState,
    RunKey,
    RunStateStore,
    StateError,
    state_to_dict,
)
from .worktree import (
    ExistingDailyBranchError,
    ExistingPublicationError,
    RemoteMainMovedError,
    RemotePin,
    WorktreeManager,
)


def _json(value: object, *, stream: TextIO | None = None) -> None:
    stream = stream or sys.stdout
    print(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        file=stream,
    )


def _add_store_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-root", required=True, type=Path)


def _add_manager_arguments(parser: argparse.ArgumentParser) -> None:
    _add_store_argument(parser)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--worktrees-root", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--remote-branch", default="main")
    parser.add_argument("--expected-remote-url")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vista-daily-maintainer")
    commands = parser.add_subparsers(dest="command", required=True)

    preflight = commands.add_parser("preflight")
    _add_manager_arguments(preflight)

    prepare = commands.add_parser("prepare")
    _add_manager_arguments(prepare)
    prepare.add_argument("--date", required=True)
    prepare.add_argument("--candidate-id", required=True)
    prepare.add_argument("--candidate-slug", required=True)
    prepare.add_argument("--backlog-sha256", required=True)
    prepare.add_argument("--candidate-sha256", required=True)
    prepare.add_argument("--expected-base")
    prepare.add_argument(
        "--pr-state",
        choices=tuple(item.value for item in PullRequestState),
        default=PullRequestState.UNKNOWN.value,
    )
    prepare.add_argument("--pr-number", type=int)
    prepare.add_argument("--pr-url")
    prepare.add_argument("--pr-head-sha")

    status = commands.add_parser("status")
    _add_store_argument(status)
    status.add_argument("--repository", required=True)
    status.add_argument("--date", required=True)
    status.add_argument("--base-sha", required=True)

    due = commands.add_parser("due")
    _add_store_argument(due)
    due.add_argument("--repository", required=True)
    due.add_argument("--now", required=True)

    check_remote = commands.add_parser("check-remote")
    _add_manager_arguments(check_remote)
    check_remote.add_argument("--date", required=True)
    check_remote.add_argument("--base-sha", required=True)
    return parser


def _manager(arguments: argparse.Namespace, store: RunStateStore) -> WorktreeManager:
    return WorktreeManager(
        repository_root=arguments.repo_root,
        state_store=store,
        worktrees_root=arguments.worktrees_root,
        repository=arguments.repository,
        remote=arguments.remote,
        remote_branch=arguments.remote_branch,
        expected_remote_url=arguments.expected_remote_url,
    )


def _parse_datetime(value: str) -> dt.datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("--now must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--now must include a timezone offset")
    return parsed


def main(args: Sequence[str] | None = None) -> int:
    """Run the local-only CLI; callers decide whether to pass the return code to exit()."""

    parser = _build_parser()
    arguments = parser.parse_args(args)
    try:
        store = RunStateStore(arguments.state_root)
        if arguments.command == "preflight":
            result = _manager(arguments, store).preflight()
            _json(
                {
                    "repository": result.repository,
                    "repository_root": result.repository_root,
                    "remote": result.pin.remote,
                    "remote_branch": result.pin.branch,
                    "base_sha": result.pin.sha,
                }
            )
            return 0

        if arguments.command == "prepare":
            manager = _manager(arguments, store)
            pin = None
            if arguments.expected_base:
                pin = RemotePin(
                    repository=arguments.repository,
                    remote=arguments.remote,
                    branch=arguments.remote_branch,
                    sha=arguments.expected_base,
                )
            publication = PublicationSnapshot(
                state=PullRequestState(arguments.pr_state),
                number=arguments.pr_number,
                url=arguments.pr_url,
                head_sha=arguments.pr_head_sha,
            )
            result = manager.prepare(
                run_date=arguments.date,
                candidate_id=arguments.candidate_id,
                candidate_slug=arguments.candidate_slug,
                backlog_sha256=arguments.backlog_sha256,
                candidate_sha256=arguments.candidate_sha256,
                expected_pin=pin,
                publication=publication,
            )
            _json(
                {
                    "idempotent_replay": result.idempotent_replay,
                    "recovered_stale_lock": result.recovered_stale_lock,
                    "state": state_to_dict(result.state),
                }
            )
            return 0

        if arguments.command == "status":
            key = RunKey(arguments.date, arguments.repository, arguments.base_sha)
            state = store.load(key)
            if state is None:
                _json({"found": False, "run_id": key.run_id})
                return 3
            _json({"found": True, "state": state_to_dict(state)})
            return 0

        if arguments.command == "due":
            decision = store.due_period(
                arguments.repository, _parse_datetime(arguments.now)
            )
            _json(
                {
                    "run_date": decision.run_date,
                    "reason": decision.reason.value,
                    "scheduled_for": decision.scheduled_for,
                }
            )
            return 0

        if arguments.command == "check-remote":
            key = RunKey(arguments.date, arguments.repository, arguments.base_sha)
            state = store.load(key)
            if state is None:
                _json({"found": False, "run_id": key.run_id})
                return 3
            _manager(arguments, store).assert_remote_unchanged(state)
            _json({"remote_unchanged": True, "state": state_to_dict(state)})
            return 0

        parser.error("unsupported command")  # pragma: no cover
    except RemoteMainMovedError as exc:
        _json(
            {
                "error": "remote_main_moved",
                "current_sha": exc.current_sha,
                "previous_sha": exc.previous_sha,
                "state": state_to_dict(exc.state),
            },
            stream=sys.stderr,
        )
        return 5
    except (ExistingDailyBranchError, ExistingPublicationError) as exc:
        _json(
            {
                "error": type(exc).__name__,
                "state": state_to_dict(exc.state),
            },
            stream=sys.stderr,
        )
        return 4
    except (StateError, ValueError) as exc:
        _json(
            {"error": type(exc).__name__, "message": str(exc)},
            stream=sys.stderr,
        )
        return 2
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
