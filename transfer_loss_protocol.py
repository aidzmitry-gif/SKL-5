"""Closed, pure protocol models for the first transfer-loss packet.

This module validates immutable receipt structure. It does not fetch referenced
operations, resolve assignments, authorize actors, or prove database origin.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from modules.wms.physical_schemas import (
    I32,
    ContainerCode,
    Measure,
    PositiveMeasure,
    ResponsibleOperationId,
    ResponsibleSubject,
    StrictModel,
    Text,
)

REPORT_KIND = "transfer_discrepancy_report"
RETURN_KIND = "transfer_discrepancy_return"
CONFIRM_KIND = "transfer_loss_confirm"
LOSS_KINDS = frozenset({REPORT_KIND, RETURN_KIND, CONFIRM_KIND})
LOSS_PRIVATE = "transfer_discrepancy_v1"
LOSS_CAUSAL = "physical_causal_v1"
LOSS_PROTOCOL = "wms.transfer.discrepancy.v1"

_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _canonical_measure_text(value: Any) -> str:
    return format(value, "f")


CanonicalMeasure = Annotated[
    Measure,
    PlainSerializer(_canonical_measure_text, return_type=str, when_used="json"),
]
CanonicalPositiveMeasure = Annotated[
    PositiveMeasure,
    PlainSerializer(_canonical_measure_text, return_type=str, when_used="json"),
]


class TransferLossCommandBase(StrictModel):
    operation_id: ResponsibleOperationId
    warehouse_id: I32


class TransferDiscrepancyReport(TransferLossCommandBase):
    kind: Literal[REPORT_KIND]
    unit_id: ResponsibleOperationId
    dispatch_operation_id: ResponsibleOperationId
    qty: CanonicalPositiveMeasure
    evidence: Text

    @model_validator(mode="after")
    def separate_report_from_dispatch(self):
        if self.operation_id == self.dispatch_operation_id:
            raise ValueError("Report and dispatch operation IDs must differ")
        return self


class TransferDiscrepancyReturn(TransferLossCommandBase):
    kind: Literal[RETURN_KIND]
    report_operation_id: ResponsibleOperationId
    reason: Text
    evidence: Text

    @model_validator(mode="after")
    def separate_decision_from_report(self):
        if self.operation_id == self.report_operation_id:
            raise ValueError("Return and report operation IDs must differ")
        return self


class TransferLossConfirm(TransferLossCommandBase):
    kind: Literal[CONFIRM_KIND]
    report_operation_id: ResponsibleOperationId
    evidence: Text

    @model_validator(mode="after")
    def separate_decision_from_report(self):
        if self.operation_id == self.report_operation_id:
            raise ValueError("Confirmation and report operation IDs must differ")
        return self


TransferLossCommand = Annotated[
    TransferDiscrepancyReport | TransferDiscrepancyReturn | TransferLossConfirm,
    Field(discriminator="kind"),
]
TransferLossDecisionCommand = Annotated[
    TransferDiscrepancyReturn | TransferLossConfirm,
    Field(discriminator="kind"),
]


class ReportIdentityProof(StrictModel):
    """Report author identity and command; assignment fields are forbidden."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    actor_user_id: I32
    actor_sub: ResponsibleSubject
    command: TransferDiscrepancyReport


class DecisionIdentityProof(StrictModel):
    """Decision identity, immutable assignment reference, and typed command."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    actor_user_id: I32
    actor_sub: ResponsibleSubject
    assignment_operation_id: ResponsibleOperationId
    assignment_revision: I32
    command: TransferLossDecisionCommand


class _PhysicalCausalV3(StrictModel):
    schema_version: int = Field(strict=True, ge=3, le=3)
    canonical_request: dict[str, Any]
    births: list[Any] = Field(max_length=0)
    count_basis: list[Any] = Field(max_length=0)
    prior_frontiers: dict[str, Any] = Field(min_length=1, max_length=1)


def _validate_causal(
    causal: _PhysicalCausalV3,
    *,
    transit_unit_id: Any,
    command: BaseModel,
) -> None:
    expected_request = command.model_dump(mode="json")
    if _canonical_json(causal.canonical_request) != _canonical_json(expected_request):
        raise ValueError("Causal request differs from the canonical private command")

    transit_key = str(transit_unit_id)
    if set(causal.prior_frontiers) != {transit_key}:
        raise ValueError("Causal prior frontier must contain only the transit unit")

    # Reuse the existing strict frontier-token validator lazily. It is pure over
    # the supplied snapshot and performs no database query or ORM mutation.
    from modules.wms.item_identity import _origin_frontier_tokens

    try:
        _origin_frontier_tokens(causal.prior_frontiers[transit_key])
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise ValueError("Causal transit frontier is malformed") from exc


class _LossReceiptBase(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    protocol: Literal[LOSS_PROTOCOL]
    kind: str
    operation_id: ResponsibleOperationId
    organization_id: I32
    warehouse_id: I32
    report_operation_id: ResponsibleOperationId
    dispatch_operation_id: ResponsibleOperationId
    source_warehouse_id: I32
    destination_warehouse_id: I32
    source_unit_id: ResponsibleOperationId
    transit_unit_id: ResponsibleOperationId
    reported_qty: CanonicalPositiveMeasure
    remaining_before: CanonicalMeasure
    remaining_after: CanonicalMeasure
    container: ContainerCode
    physical_causal_v1: _PhysicalCausalV3

    @model_validator(mode="after")
    def distinct_transfer_references(self):
        if self.source_warehouse_id == self.destination_warehouse_id:
            raise ValueError("Transfer loss requires distinct source and destination warehouses")
        if self.source_unit_id == self.transit_unit_id:
            raise ValueError("Source and transit unit IDs must differ")
        if self.report_operation_id == self.dispatch_operation_id:
            raise ValueError("Report and dispatch operation IDs must differ")
        return self


class TransferDiscrepancyReportReceipt(_LossReceiptBase):
    kind: Literal[REPORT_KIND]
    status: Literal["pending"]
    transfer_discrepancy_v1: ReportIdentityProof

    @model_validator(mode="after")
    def match_report_command(self):
        proof = self.transfer_discrepancy_v1
        command = proof.command
        if (
            self.operation_id != self.report_operation_id
            or self.warehouse_id != self.destination_warehouse_id
            or command.operation_id != self.operation_id
            or command.warehouse_id != self.warehouse_id
            or command.unit_id != self.transit_unit_id
            or command.dispatch_operation_id != self.dispatch_operation_id
            or command.qty != self.reported_qty
            or self.remaining_after != self.remaining_before
            or self.reported_qty > self.remaining_before
        ):
            raise ValueError("Report receipt does not match its command or remaining boundary")
        _validate_causal(
            self.physical_causal_v1,
            transit_unit_id=self.transit_unit_id,
            command=command,
        )
        return self


class TransferDiscrepancyReturnReceipt(_LossReceiptBase):
    kind: Literal[RETURN_KIND]
    status: Literal["returned"]
    transfer_discrepancy_v1: DecisionIdentityProof

    @model_validator(mode="after")
    def match_return_command(self):
        proof = self.transfer_discrepancy_v1
        command = proof.command
        if (
            command.kind != RETURN_KIND
            or self.operation_id == self.report_operation_id
            or self.operation_id == self.dispatch_operation_id
            or proof.assignment_operation_id in {
                self.operation_id,
                self.report_operation_id,
                self.dispatch_operation_id,
            }
            or self.warehouse_id != self.source_warehouse_id
            or command.operation_id != self.operation_id
            or command.warehouse_id != self.warehouse_id
            or command.report_operation_id != self.report_operation_id
            or self.remaining_after != self.remaining_before
        ):
            raise ValueError("Return receipt does not match its command or current boundary")
        _validate_causal(
            self.physical_causal_v1,
            transit_unit_id=self.transit_unit_id,
            command=command,
        )
        return self


class TransferLossConfirmReceipt(_LossReceiptBase):
    kind: Literal[CONFIRM_KIND]
    status: Literal["confirmed"]
    transfer_discrepancy_v1: DecisionIdentityProof

    @model_validator(mode="after")
    def match_confirmation_command(self):
        proof = self.transfer_discrepancy_v1
        command = proof.command
        if (
            command.kind != CONFIRM_KIND
            or self.operation_id == self.report_operation_id
            or self.operation_id == self.dispatch_operation_id
            or proof.assignment_operation_id in {
                self.operation_id,
                self.report_operation_id,
                self.dispatch_operation_id,
            }
            or self.warehouse_id != self.source_warehouse_id
            or command.operation_id != self.operation_id
            or command.warehouse_id != self.warehouse_id
            or command.report_operation_id != self.report_operation_id
            or self.reported_qty > self.remaining_before
            or self.remaining_after != self.remaining_before - self.reported_qty
        ):
            raise ValueError("Confirmation receipt does not match its command or arithmetic")
        _validate_causal(
            self.physical_causal_v1,
            transit_unit_id=self.transit_unit_id,
            command=command,
        )
        return self


TransferLossReceipt = Annotated[
    TransferDiscrepancyReportReceipt
    | TransferDiscrepancyReturnReceipt
    | TransferLossConfirmReceipt,
    Field(discriminator="kind"),
]

_COMMAND_ADAPTER = TypeAdapter(TransferLossCommand)
_RECEIPT_ADAPTER = TypeAdapter(TransferLossReceipt)
_I32_ADAPTER = TypeAdapter(I32)
_SUB_ADAPTER = TypeAdapter(ResponsibleSubject)
_OPERATION_ID_ADAPTER = TypeAdapter(ResponsibleOperationId)


class LossReceiptError(ValueError):
    """The stored loss receipt is malformed or internally inconsistent."""


def _canonical_json(value: Any) -> str:
    """Match physical_stock.canonical/digest without importing its ORM module."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _typed_command(body: Any) -> BaseModel:
    if isinstance(body, BaseModel):
        body = body.model_dump(mode="json")
    return _COMMAND_ADAPTER.validate_python(body)


def loss_request_hash(
    organization_id: int,
    warehouse_id: int,
    actor_user_id: int,
    actor_sub: str,
    body: Any,
) -> str:
    """Hash a canonical loss command with its exact managed actor and scope."""
    organization_id = _I32_ADAPTER.validate_python(organization_id)
    warehouse_id = _I32_ADAPTER.validate_python(warehouse_id)
    actor_user_id = _I32_ADAPTER.validate_python(actor_user_id)
    actor_sub = _SUB_ADAPTER.validate_python(actor_sub)
    command = _typed_command(body)
    if command.warehouse_id != warehouse_id:
        raise ValueError("Command warehouse differs from hash scope")
    payload = {
        "organization_id": organization_id,
        "warehouse_id": warehouse_id,
        "actor_user_id": actor_user_id,
        "actor_sub": actor_sub,
        "body": command.model_dump(mode="json"),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def loss_operation_indicator(kind: Any, result: Any) -> bool:
    """Identify loss operations before projection, including damaged results."""
    if isinstance(kind, str) and kind in LOSS_KINDS:
        return True
    if not isinstance(result, dict):
        return False
    result_kind = result.get("kind")
    return LOSS_PRIVATE in result or (
        isinstance(result_kind, str) and result_kind in LOSS_KINDS
    )


def recorded_receipt(operation: Any) -> TransferLossReceipt:
    """Validate a closed immutable operation snapshot without fetching references."""
    required = {
        "id",
        "organization_id",
        "operation_id",
        "warehouse_id",
        "kind",
        "request_hash",
        "actor",
        "result",
    }
    allowed = required | {"created_at"}
    try:
        if not isinstance(operation, dict) or frozenset(operation) not in {frozenset(required), frozenset(allowed)}:
            raise ValueError("Operation snapshot shape is not closed")
        _I32_ADAPTER.validate_python(operation["id"])
        organization_id = _I32_ADAPTER.validate_python(operation["organization_id"])
        warehouse_id = _I32_ADAPTER.validate_python(operation["warehouse_id"])
        operation_id = _OPERATION_ID_ADAPTER.validate_python(operation["operation_id"])
        actor = _SUB_ADAPTER.validate_python(operation["actor"])
        if not isinstance(operation["kind"], str) or operation["kind"] not in LOSS_KINDS:
            raise ValueError("Operation snapshot kind is not a loss command")
        if not isinstance(operation["request_hash"], str) or not _HEX_DIGEST.fullmatch(operation["request_hash"]):
            raise ValueError("Operation request hash is malformed")
        if "created_at" in operation:
            created_at = operation["created_at"]
            if created_at is not None and not isinstance(created_at, str):
                raise ValueError("Operation snapshot created_at is malformed")

        receipt = _RECEIPT_ADAPTER.validate_python(operation["result"])
        canonical_result = receipt.model_dump(mode="json")
        if canonical_result != operation["result"]:
            raise ValueError("Stored loss receipt is not canonical")
        if (
            receipt.kind != operation["kind"]
            or receipt.organization_id != organization_id
            or receipt.warehouse_id != warehouse_id
            or receipt.operation_id != operation_id
        ):
            raise ValueError("Stored loss receipt headers differ from the operation snapshot")

        proof = getattr(receipt, LOSS_PRIVATE)
        if actor != proof.actor_sub:
            raise ValueError("Stored loss receipt actor differs from the operation actor")
        expected_hash = loss_request_hash(
            receipt.organization_id,
            receipt.warehouse_id,
            proof.actor_user_id,
            proof.actor_sub,
            proof.command,
        )
        if expected_hash != operation["request_hash"]:
            raise ValueError("Stored loss receipt request hash differs from its proof")
        return receipt
    except LossReceiptError:
        raise
    except (ValidationError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise LossReceiptError("Stored transfer-loss receipt is malformed") from exc


def public_loss_result(operation: Any) -> dict[str, Any]:
    """Return the public receipt only after full closed-receipt validation."""
    receipt = recorded_receipt(operation)
    return receipt.model_dump(mode="json", exclude={LOSS_PRIVATE, LOSS_CAUSAL})


__all__ = [
    "CONFIRM_KIND",
    "DecisionIdentityProof",
    "LOSS_KINDS",
    "LOSS_PRIVATE",
    "LossReceiptError",
    "REPORT_KIND",
    "RETURN_KIND",
    "ReportIdentityProof",
    "TransferDiscrepancyReport",
    "TransferDiscrepancyReturn",
    "TransferLossCommand",
    "TransferLossConfirm",
    "TransferLossReceipt",
    "loss_operation_indicator",
    "loss_request_hash",
    "public_loss_result",
    "recorded_receipt",
]
