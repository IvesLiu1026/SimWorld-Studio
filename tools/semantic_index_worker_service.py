"""Durable, fail-closed execution service for semantic-index phase requests.

The transport layer authenticates a one-shot request and transfers its sealed
capabilities.  This module owns the next security boundary: it durably records
the request *before* invoking an executor and durably commits the validated
result *before* sending a reply.

The legacy/offline ``serve_received_request`` entry point supports only
ordinary phases.  The Production-candidate isolated entry point requires both
the ordinary ledger and a separately pinned durable control ledger; control
requests are never routed through the phase ledger.
"""

from __future__ import annotations

import dataclasses
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

import semantic_index_durable_ledger as durable_ledger
import semantic_index_control_ledger as control_ledger
import semantic_index_isolated_executor as isolated_executor
import semantic_index_worker_protocol as worker_protocol


_LEDGER_CLASS = durable_ledger.DurableIdempotencyLedger
_LEDGER_BEGIN = _LEDGER_CLASS.begin
_LEDGER_COMMIT_RESULT = _LEDGER_CLASS.commit_result
_CONTROL_LEDGER_CLASS = control_ledger.DurableControlLedger
_CONTROL_LEDGER_BEGIN = _CONTROL_LEDGER_CLASS.begin
_CONTROL_LEDGER_COMMIT_RESULT = _CONTROL_LEDGER_CLASS.commit_result
_EXECUTE_ISOLATED = isolated_executor.execute_isolated
_SHA256_REVISION_RE = re.compile(r"\Asha256:[a-f0-9]{64}\Z")


CONTROL_UNSUPPORTED_CODE = "WORKER_CONTROL_UNSUPPORTED"
CONTROL_UNSUPPORTED_MESSAGE = (
    "Worker control requests require a separate durable control ledger"
)
EXECUTOR_FAILED_CODE = "WORKER_INTERNAL"
EXECUTOR_FAILED_MESSAGE = "Worker operation failed"
REQUEST_BYTES_INVALID_CODE = "WORKER_REQUEST_BYTES_INVALID"
REQUEST_BYTES_INVALID_MESSAGE = "Worker request bytes do not match the request"
LEDGER_COMMIT_INVALID_CODE = "WORKER_LEDGER_COMMIT_INVALID"
LEDGER_COMMIT_INVALID_MESSAGE = "Worker durable result commit was inconsistent"


PhaseExecutor = Callable[
    [Mapping[str, Any], tuple[bytes, ...]],
    Mapping[str, Any],
]
ControlExecutor = Callable[
    [Mapping[str, Any], tuple[bytes, ...]],
    Mapping[str, Any],
]


@dataclasses.dataclass(frozen=True, slots=True)
class WorkerServiceOutcome:
    """One safe phase disposition and the exact bytes eligible for reply."""

    disposition: Literal["executed", "replay", "recovery_required"]
    value: dict[str, Any]
    canonical_bytes: bytes
    ledger_key: str
    request_sha256: str


def _protocol_failure(
    code: str,
    message: str,
    *,
    mutation_state: str = "none",
) -> None:
    raise worker_protocol.WorkerProtocolError(
        code=code,
        message=message,
        dependency="worker",
        retryable=False,
        mutation_state=mutation_state,
    )


def _failure_mutation_state(request: Mapping[str, Any]) -> str:
    contract = worker_protocol.operation_contract(request["operation"])
    if "ambiguous" in contract["failure_mutation_states"]:
        return "ambiguous"
    return "none"


def _require_execution_deadline(request: Mapping[str, Any]) -> None:
    """Recheck the absolute deadline after durable prepare publication."""

    if request["deadline_monotonic_ns"] <= time.monotonic_ns():
        _protocol_failure(
            "WORKER_DEADLINE_EXCEEDED",
            "Worker request exceeded its absolute deadline",
        )


def _validate_capabilities(
    request: Mapping[str, Any],
    canonical_request: bytes,
    capabilities: Sequence[bytes],
) -> tuple[bytes, ...]:
    if type(capabilities) is not tuple:
        _protocol_failure(
            "WORKER_CAPABILITY_INVALID",
            "Worker capabilities must be an immutable ordered tuple",
        )
    descriptors = request["credential_transport"]["descriptors"]
    if len(capabilities) != len(descriptors):
        _protocol_failure(
            "WORKER_CAPABILITY_INVALID",
            "Worker capability count does not match the request",
        )
    exact_capabilities: list[bytes] = []
    for capability, descriptor in zip(capabilities, descriptors):
        if type(capability) is not bytes or len(capability) != descriptor["byte_count"]:
            _protocol_failure(
                "WORKER_CAPABILITY_INVALID",
                "Worker capability length does not match the request",
            )
        exact_capabilities.append(capability)
    if len(exact_capabilities) > 1 and len(set(exact_capabilities)) != len(
        exact_capabilities
    ):
        _protocol_failure(
            "WORKER_CAPABILITY_INVALID",
            "Worker capabilities must be distinct",
        )
    # The receive path already performs this scan before decoding JSON.  It is
    # repeated at the service boundary so the lower-level phase API cannot be
    # used to persist a request containing a declared capability transform.
    worker_protocol._assert_no_capability_transforms(  # noqa: SLF001
        canonical_request,
        exact_capabilities,
        code="WORKER_REQUEST_CAPABILITY_LEAK",
        mutation_state="none",
    )
    return tuple(exact_capabilities)


def _validated_phase_request(
    request: Mapping[str, Any],
    canonical_request: bytes,
    capabilities: Sequence[bytes],
) -> tuple[dict[str, Any], tuple[bytes, ...]]:
    validated = worker_protocol.validate_request(request)
    expected_bytes = worker_protocol.canonical_json_bytes(validated)
    if type(canonical_request) is not bytes or canonical_request != expected_bytes:
        _protocol_failure(REQUEST_BYTES_INVALID_CODE, REQUEST_BYTES_INVALID_MESSAGE)
    return validated, _validate_capabilities(
        validated,
        canonical_request,
        capabilities,
    )


def _validate_ledger_binding(
    request: Mapping[str, Any],
    ledger: durable_ledger.DurableIdempotencyLedger,
    expected_ledger_revision: str,
) -> None:
    if (
        type(expected_ledger_revision) is not str
        or _SHA256_REVISION_RE.fullmatch(expected_ledger_revision) is None
    ):
        _protocol_failure(
            "WORKER_LEDGER_REVISION_INVALID",
            "Worker durable ledger revision configuration is invalid",
        )
    if request["idempotency_ledger_identity"] != ledger.ledger_identity:
        _protocol_failure(
            "WORKER_LEDGER_IDENTITY_MISMATCH",
            "Worker request does not match the durable ledger identity",
        )
    if request["idempotency_ledger_revision"] != expected_ledger_revision:
        _protocol_failure(
            "WORKER_LEDGER_REVISION_MISMATCH",
            "Worker request does not match the approved durable ledger revision",
        )


def _validate_result_bytes(
    result: Mapping[str, Any],
    request: Mapping[str, Any],
    capabilities: Sequence[bytes],
) -> tuple[dict[str, Any], bytes]:
    validated = worker_protocol.validate_response(result, request)
    canonical = worker_protocol.canonical_json_bytes(validated)
    # A result must be proven free of the declared capability encodings before
    # it is written to the durable ledger, not merely before socket delivery.
    worker_protocol._assert_no_capability_transforms(  # noqa: SLF001
        canonical,
        capabilities,
        code="WORKER_RESPONSE_CAPABILITY_LEAK",
        mutation_state=validated["mutation_state"],
    )
    return validated, canonical


def _decode_replay(
    canonical_result: bytes,
    request: Mapping[str, Any],
    capabilities: Sequence[bytes],
) -> dict[str, Any]:
    decoded = worker_protocol.decode_canonical_json(
        canonical_result,
        maximum=durable_ledger.MAX_RESULT_BYTES,
    )
    if type(decoded) is not dict:
        _protocol_failure(
            "WORKER_RESPONSE_STATUS_INVALID",
            "Durable worker result is not an object",
            mutation_state="ambiguous",
        )
    validated, regenerated = _validate_result_bytes(decoded, request, capabilities)
    if regenerated != canonical_result:
        _protocol_failure(
            LEDGER_COMMIT_INVALID_CODE,
            LEDGER_COMMIT_INVALID_MESSAGE,
            mutation_state=validated["mutation_state"],
        )
    return validated


def _validated_control_request(
    request: Mapping[str, Any],
    canonical_request: bytes,
    capabilities: Sequence[bytes],
) -> tuple[dict[str, Any], tuple[bytes, ...]]:
    validated = worker_protocol.validate_control_request(request)
    expected_bytes = worker_protocol.canonical_json_bytes(validated)
    if type(canonical_request) is not bytes or canonical_request != expected_bytes:
        _protocol_failure(REQUEST_BYTES_INVALID_CODE, REQUEST_BYTES_INVALID_MESSAGE)
    return validated, _validate_capabilities(
        validated,
        canonical_request,
        capabilities,
    )


def _validate_control_result_bytes(
    result: Mapping[str, Any],
    request: Mapping[str, Any],
    capabilities: Sequence[bytes],
) -> tuple[dict[str, Any], bytes]:
    validated = worker_protocol.validate_control_response(result, request)
    canonical = worker_protocol.canonical_json_bytes(validated)
    worker_protocol._assert_no_capability_transforms(  # noqa: SLF001
        canonical,
        capabilities,
        code="WORKER_RESPONSE_CAPABILITY_LEAK",
        mutation_state=validated["mutation_state"],
    )
    return validated, canonical


def _decode_control_replay(
    canonical_result: bytes,
    request: Mapping[str, Any],
    capabilities: Sequence[bytes],
) -> dict[str, Any]:
    decoded = worker_protocol.decode_canonical_json(
        canonical_result,
        maximum=control_ledger.MAX_RESULT_BYTES,
    )
    if type(decoded) is not dict:
        _protocol_failure(
            "WORKER_RESPONSE_STATUS_INVALID",
            "Durable worker result is not an object",
            mutation_state="ambiguous",
        )
    validated, regenerated = _validate_control_result_bytes(
        decoded,
        request,
        capabilities,
    )
    if regenerated != canonical_result:
        _protocol_failure(
            LEDGER_COMMIT_INVALID_CODE,
            LEDGER_COMMIT_INVALID_MESSAGE,
            mutation_state=validated["mutation_state"],
        )
    return validated


def _recovery_required_result(request: Mapping[str, Any]) -> dict[str, Any]:
    mutation_state = _failure_mutation_state(request)
    result = {
        "schema": worker_protocol.RESULT_SCHEMA,
        "protocol": worker_protocol.PROTOCOL_REVISION,
        "request_binding": worker_protocol.request_binding_for(request),
        "status": "failed",
        "mutation_state": mutation_state,
        "metrics": {name: 0 for name in request["expected_metrics"]},
        "artifact": None,
        "error": {
            "dependency": "worker",
            "code": "WORKER_LEDGER_CONFLICT",
            "retryable": False,
            "mutation_state": mutation_state,
        },
    }
    return worker_protocol.validate_response(result, request)


def _control_recovery_required_result(
    request: Mapping[str, Any],
) -> dict[str, Any]:
    contract = worker_protocol.control_operation_contract(request["operation"])
    mutation_state = (
        "ambiguous"
        if "ambiguous" in contract["failure_mutation_states"]
        else "none"
    )
    result = {
        "schema": worker_protocol.CONTROL_RESULT_SCHEMA,
        "protocol": worker_protocol.PROTOCOL_REVISION,
        "request_binding": worker_protocol.control_request_binding_for(request),
        "status": "failed",
        "mutation_state": mutation_state,
        "receipt": None,
        "error": {
            "dependency": "worker",
            "code": "WORKER_LEDGER_CONFLICT",
            "retryable": False,
            "mutation_state": mutation_state,
        },
    }
    return worker_protocol.validate_control_response(result, request)


def execute_phase_request(
    request: Mapping[str, Any],
    *,
    canonical_request: bytes,
    capabilities: Sequence[bytes],
    ledger: durable_ledger.DurableIdempotencyLedger,
    expected_ledger_revision: str,
    executor: PhaseExecutor,
) -> WorkerServiceOutcome:
    """Execute, replay, or quarantine one already-authenticated phase request.

    ``expected_ledger_revision`` must come from independently pinned worker
    configuration.  Passing the request's own value would collapse the trust
    boundary and is not a supported production wiring.

    ``ledger.begin`` is the last operation before the executor callback.  Once
    it publishes a prepare record, every exceptional path intentionally leaves
    that record in place.  A later call therefore returns recovery-required
    and cannot blindly invoke the callback again.
    """

    # A subclass can override begin/commit and silently remove the durability
    # boundary.  Until implementation attestations are independently sealed,
    # only the audited concrete ledger implementation is accepted.
    if type(ledger) is not _LEDGER_CLASS:
        _protocol_failure(
            "WORKER_LEDGER_INVALID",
            "Worker durable ledger is invalid",
        )
    if not callable(executor):
        _protocol_failure(
            "WORKER_EXECUTOR_INVALID",
            "Worker executor is invalid",
        )
    validated_request, exact_capabilities = _validated_phase_request(
        request,
        canonical_request,
        capabilities,
    )
    _validate_ledger_binding(
        validated_request,
        ledger,
        expected_ledger_revision,
    )

    begin = _LEDGER_BEGIN(ledger, canonical_request)
    if begin.action == "recovery_required":
        value = _recovery_required_result(validated_request)
        canonical = worker_protocol.canonical_json_bytes(value)
        worker_protocol._assert_no_capability_transforms(  # noqa: SLF001
            canonical,
            exact_capabilities,
            code="WORKER_RESPONSE_CAPABILITY_LEAK",
            mutation_state=value["mutation_state"],
        )
        return WorkerServiceOutcome(
            disposition="recovery_required",
            value=value,
            canonical_bytes=canonical,
            ledger_key=begin.ledger_key,
            request_sha256=begin.request_sha256,
        )

    if begin.action == "replay":
        if begin.result_bytes is None:
            _protocol_failure(
                LEDGER_COMMIT_INVALID_CODE,
                LEDGER_COMMIT_INVALID_MESSAGE,
                mutation_state="ambiguous",
            )
        value = _decode_replay(
            begin.result_bytes,
            validated_request,
            exact_capabilities,
        )
        return WorkerServiceOutcome(
            disposition="replay",
            value=value,
            canonical_bytes=begin.result_bytes,
            ledger_key=begin.ledger_key,
            request_sha256=begin.request_sha256,
        )

    if begin.action != "execute" or begin.result_bytes is not None:
        _protocol_failure(
            LEDGER_COMMIT_INVALID_CODE,
            LEDGER_COMMIT_INVALID_MESSAGE,
            mutation_state="ambiguous",
        )
    _require_execution_deadline(validated_request)

    # Give the callback a detached JSON value.  It cannot mutate the private
    # request later used to validate and bind its result.
    executor_request = json.loads(
        worker_protocol.canonical_json_bytes(validated_request).decode("utf-8")
    )
    # Validation, ledger fsync, and detachment can consume the final deadline
    # budget.  Never begin a callback after the absolute boundary.
    _require_execution_deadline(validated_request)
    try:
        result = executor(executor_request, exact_capabilities)
    except Exception:
        _protocol_failure(
            EXECUTOR_FAILED_CODE,
            EXECUTOR_FAILED_MESSAGE,
            mutation_state=_failure_mutation_state(validated_request),
        )

    validated_result, canonical_result = _validate_result_bytes(
        result,
        validated_request,
        exact_capabilities,
    )
    committed = _LEDGER_COMMIT_RESULT(ledger, canonical_request, canonical_result)
    if type(committed) is not bytes or committed != canonical_result:
        _protocol_failure(
            LEDGER_COMMIT_INVALID_CODE,
            LEDGER_COMMIT_INVALID_MESSAGE,
            mutation_state=validated_result["mutation_state"],
        )
    return WorkerServiceOutcome(
        disposition="executed",
        value=validated_result,
        canonical_bytes=committed,
        ledger_key=begin.ledger_key,
        request_sha256=begin.request_sha256,
    )


def execute_phase_request_isolated(
    request: Mapping[str, Any],
    *,
    canonical_request: bytes,
    capabilities: Sequence[bytes],
    ledger: durable_ledger.DurableIdempotencyLedger,
    expected_ledger_revision: str,
    executor: PhaseExecutor,
) -> WorkerServiceOutcome:
    """Execute a phase through the mandatory fork/process-group boundary.

    This is the only candidate entry point for a future registered Production
    worker.  The lower-level :func:`execute_phase_request` remains available to
    deterministic offline fixture tests, but it invokes its callback in the
    current process and is not a Production execution path.

    If the isolated child fails or reaches its absolute deadline, the ordinary
    phase service emits its fixed worker failure and deliberately leaves the
    durable prepare without a result.  A later ordinary request cannot blindly
    retry it; recovery must use the separately authorized control path.
    """

    if not callable(executor):
        _protocol_failure(
            "WORKER_EXECUTOR_INVALID",
            "Worker executor is invalid",
        )

    def isolated_boundary(
        detached_request: Mapping[str, Any],
        exact_capabilities: tuple[bytes, ...],
    ) -> Mapping[str, Any]:
        if type(detached_request) is not dict:
            _protocol_failure(
                "WORKER_EXECUTOR_INVALID",
                "Worker executor is invalid",
            )
        return _EXECUTE_ISOLATED(
            detached_request,
            exact_capabilities,
            executor,
            deadline_monotonic_ns=detached_request["deadline_monotonic_ns"],
        )

    return execute_phase_request(
        request,
        canonical_request=canonical_request,
        capabilities=capabilities,
        ledger=ledger,
        expected_ledger_revision=expected_ledger_revision,
        executor=isolated_boundary,
    )


def execute_control_request_isolated(
    request: Mapping[str, Any],
    *,
    canonical_request: bytes,
    capabilities: Sequence[bytes],
    ledger: control_ledger.DurableControlLedger,
    expected_pins: control_ledger.ControlLedgerPins,
    executor: ControlExecutor,
) -> WorkerServiceOutcome:
    """Execute/replay one authenticated control request through its own ledger.

    ``expected_pins`` must be the exact object used to construct ``ledger`` and
    must originate from independently verified plan/launcher state.  Constructing
    it from fields echoed by ``request`` collapses this trust boundary and is not
    Production wiring.
    """

    if type(ledger) is not _CONTROL_LEDGER_CLASS:
        _protocol_failure(
            "WORKER_LEDGER_INVALID",
            "Worker durable control ledger is invalid",
        )
    if (
        type(expected_pins) is not control_ledger.ControlLedgerPins
        or ledger.pins is not expected_pins
    ):
        _protocol_failure(
            "WORKER_LEDGER_REVISION_INVALID",
            "Worker durable control ledger policy is invalid",
        )
    if not callable(executor):
        _protocol_failure(
            "WORKER_EXECUTOR_INVALID",
            "Worker executor is invalid",
        )
    validated_request, exact_capabilities = _validated_control_request(
        request,
        canonical_request,
        capabilities,
    )
    begin = _CONTROL_LEDGER_BEGIN(ledger, canonical_request)
    if begin.action == "recovery_required":
        value = _control_recovery_required_result(validated_request)
        canonical = worker_protocol.canonical_json_bytes(value)
        worker_protocol._assert_no_capability_transforms(  # noqa: SLF001
            canonical,
            exact_capabilities,
            code="WORKER_RESPONSE_CAPABILITY_LEAK",
            mutation_state=value["mutation_state"],
        )
        return WorkerServiceOutcome(
            disposition="recovery_required",
            value=value,
            canonical_bytes=canonical,
            ledger_key=begin.ledger_key,
            request_sha256=begin.request_sha256,
        )
    if begin.action == "replay":
        if begin.result_bytes is None:
            _protocol_failure(
                LEDGER_COMMIT_INVALID_CODE,
                LEDGER_COMMIT_INVALID_MESSAGE,
                mutation_state="ambiguous",
            )
        value = _decode_control_replay(
            begin.result_bytes,
            validated_request,
            exact_capabilities,
        )
        return WorkerServiceOutcome(
            disposition="replay",
            value=value,
            canonical_bytes=begin.result_bytes,
            ledger_key=begin.ledger_key,
            request_sha256=begin.request_sha256,
        )
    if begin.action != "execute" or begin.result_bytes is not None:
        _protocol_failure(
            LEDGER_COMMIT_INVALID_CODE,
            LEDGER_COMMIT_INVALID_MESSAGE,
            mutation_state="ambiguous",
        )
    _require_execution_deadline(validated_request)

    executor_request = json.loads(
        worker_protocol.canonical_json_bytes(validated_request).decode("utf-8")
    )
    _require_execution_deadline(validated_request)
    try:
        result = _EXECUTE_ISOLATED(
            executor_request,
            exact_capabilities,
            executor,
            deadline_monotonic_ns=validated_request["deadline_monotonic_ns"],
        )
    except Exception:
        _protocol_failure(
            EXECUTOR_FAILED_CODE,
            EXECUTOR_FAILED_MESSAGE,
            mutation_state=(
                "ambiguous"
                if "ambiguous"
                in worker_protocol.control_operation_contract(
                    validated_request["operation"]
                )["failure_mutation_states"]
                else "none"
            ),
        )

    validated_result, canonical_result = _validate_control_result_bytes(
        result,
        validated_request,
        exact_capabilities,
    )
    committed = _CONTROL_LEDGER_COMMIT_RESULT(
        ledger,
        canonical_request,
        canonical_result,
    )
    if type(committed) is not bytes or committed != canonical_result:
        _protocol_failure(
            LEDGER_COMMIT_INVALID_CODE,
            LEDGER_COMMIT_INVALID_MESSAGE,
            mutation_state=validated_result["mutation_state"],
        )
    return WorkerServiceOutcome(
        disposition="executed",
        value=validated_result,
        canonical_bytes=committed,
        ledger_key=begin.ledger_key,
        request_sha256=begin.request_sha256,
    )


def serve_received_request(
    received: worker_protocol.ReceivedWorkerRequest,
    *,
    ledger: durable_ledger.DurableIdempotencyLedger,
    expected_ledger_revision: str,
    executor: PhaseExecutor,
) -> WorkerServiceOutcome:
    """Durably handle and reply to one authenticated one-shot worker session.

    The expected ledger revision is an independently pinned worker input, not
    a value derived from ``received``.
    """

    received = worker_protocol._require_authenticated_received_request(  # noqa: SLF001
        received
    )
    try:
        schema = (
            received.value.get("schema") if isinstance(received.value, dict) else None
        )
        if schema == worker_protocol.CONTROL_REQUEST_SCHEMA:
            # No phase-ledger fallback is legal for control mutations.
            worker_protocol.validate_control_request(received.value)
            _protocol_failure(CONTROL_UNSUPPORTED_CODE, CONTROL_UNSUPPORTED_MESSAGE)
        outcome = execute_phase_request(
            received.value,
            canonical_request=received.canonical_bytes,
            capabilities=received.capabilities,
            ledger=ledger,
            expected_ledger_revision=expected_ledger_revision,
            executor=executor,
        )
        sent = worker_protocol.send_committed_worker_response(
            received,
            outcome.canonical_bytes,
        )
        if sent.canonical_bytes != outcome.canonical_bytes:
            _protocol_failure(
                LEDGER_COMMIT_INVALID_CODE,
                LEDGER_COMMIT_INVALID_MESSAGE,
                mutation_state=outcome.value["mutation_state"],
            )
        return outcome
    except BaseException:
        # If result delivery fails after commit, the immutable ledger entry is
        # retained and the next request receives the exact replay.
        received.close()
        raise


def serve_received_request_isolated(
    received: worker_protocol.ReceivedWorkerRequest,
    *,
    ledger: durable_ledger.DurableIdempotencyLedger,
    expected_ledger_revision: str,
    executor: PhaseExecutor,
    control_ledger_instance: control_ledger.DurableControlLedger,
    expected_control_pins: control_ledger.ControlLedgerPins,
    control_executor: ControlExecutor,
) -> WorkerServiceOutcome:
    """Serve one phase/control request through mandatory isolated boundaries."""

    received = worker_protocol._require_authenticated_received_request(  # noqa: SLF001
        received
    )
    try:
        schema = (
            received.value.get("schema") if isinstance(received.value, dict) else None
        )
        if schema == worker_protocol.CONTROL_REQUEST_SCHEMA:
            outcome = execute_control_request_isolated(
                received.value,
                canonical_request=received.canonical_bytes,
                capabilities=received.capabilities,
                ledger=control_ledger_instance,
                expected_pins=expected_control_pins,
                executor=control_executor,
            )
        else:
            outcome = execute_phase_request_isolated(
                received.value,
                canonical_request=received.canonical_bytes,
                capabilities=received.capabilities,
                ledger=ledger,
                expected_ledger_revision=expected_ledger_revision,
                executor=executor,
            )
        sent = worker_protocol.send_committed_worker_response(
            received,
            outcome.canonical_bytes,
        )
        if sent.canonical_bytes != outcome.canonical_bytes:
            _protocol_failure(
                LEDGER_COMMIT_INVALID_CODE,
                LEDGER_COMMIT_INVALID_MESSAGE,
                mutation_state=outcome.value["mutation_state"],
            )
        return outcome
    except BaseException:
        received.close()
        raise


__all__ = [
    "CONTROL_UNSUPPORTED_CODE",
    "CONTROL_UNSUPPORTED_MESSAGE",
    "ControlExecutor",
    "PhaseExecutor",
    "WorkerServiceOutcome",
    "execute_control_request_isolated",
    "execute_phase_request",
    "execute_phase_request_isolated",
    "serve_received_request",
    "serve_received_request_isolated",
]
