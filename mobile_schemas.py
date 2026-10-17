"""Strict request and response contracts for the WMS mobile facade."""
from __future__ import annotations

import base64
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from modules.wms.physical_schemas import (
    I32,
    MAX_MEASURE,
    ContainerCode,
    PositiveQuantity,
    RawScan,
    TransferReceipt,
)

PublicCoordinate = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{43}$")]

MOBILE_ACTIONS = frozenset({
    "state", "scan", "receipt", "count_read",
    "receive", "return", "move", "open_package", "reserve", "release",
    "pick", "unpick", "pack", "ship", "task_claim", "task_complete",
    "putaway", "replenish", "count_start", "count_record", "count_location_complete",
    "item_identity_ensure", "item_label_prepare", "item_label_verify",
    "transfer_dispatch", "transfer_receive", "transfer_discrepancy_report",
})

MobileAction = Literal[
    "state", "scan", "receipt", "count_read", "receive", "return", "move",
    "open_package", "reserve", "release", "pick", "unpick", "pack", "ship",
    "task_claim", "task_complete", "putaway", "replenish", "count_start",
    "count_record", "count_location_complete", "item_identity_ensure",
    "item_label_prepare", "item_label_verify",
    "transfer_dispatch", "transfer_receive", "transfer_discrepancy_report",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PublicJwk(StrictModel):
    kty: Literal["EC"]
    crv: Literal["P-256"]
    x: PublicCoordinate
    y: PublicCoordinate

    @field_validator("x", "y")
    @classmethod
    def canonical_coordinate(cls, value: str) -> str:
        try:
            decoded = base64.urlsafe_b64decode(value + "=")
        except Exception as exc:
            raise ValueError("coordinate must be canonical base64url") from exc
        canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
        if len(decoded) != 32 or canonical != value:
            raise ValueError("coordinate must be canonical base64url")
        return value


class EnrollmentRequest(StrictModel):
    device_id: UUID
    public_jwk: PublicJwk
    organization_id: int = Field(gt=0, strict=True)
    warehouse_id: int = Field(gt=0, strict=True)
    requested_actions: list[MobileAction] = Field(min_length=1, max_length=len(MOBILE_ACTIONS))

    @field_validator("requested_actions")
    @classmethod
    def unique_actions(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("actions must be unique")
        return value


class GrantApproval(StrictModel):
    action: Literal["grant_approve"]
    device_id: UUID
    organization_id: int = Field(gt=0, strict=True)
    warehouse_id: int = Field(gt=0, strict=True)
    actions: list[MobileAction] = Field(min_length=1, max_length=len(MOBILE_ACTIONS))

    @field_validator("actions")
    @classmethod
    def unique_actions(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("actions must be unique")
        return value


class GrantRestriction(StrictModel):
    action: Literal["grant_restrict", "grant_revoke"]
    grant_id: UUID
    actions: list[MobileAction] | None = Field(default=None, min_length=1, max_length=len(MOBILE_ACTIONS))

    @model_validator(mode="after")
    def valid_action_set(self):
        if self.action == "grant_restrict" and self.actions is None:
            raise ValueError("restriction requires an action set")
        if self.action == "grant_revoke" and self.actions is not None:
            raise ValueError("revocation does not accept replacement actions")
        return self


class DeviceRevocation(StrictModel):
    action: Literal["device_revoke"]
    device_id: UUID


AdminCommand = GrantApproval | GrantRestriction | DeviceRevocation


class MobileGrantView(StrictModel):
    grant_id: str
    revision: int
    organization_id: int
    warehouse_id: int
    actions: list[str]


class MobileContext(StrictModel):
    device_id: str
    device_revision: int
    grants: list[MobileGrantView]


class MobileStatus(StrictModel):
    device_id: str
    state: Literal["pending", "active", "revoked"]
    revision: int


class MobileLocation(StrictModel):
    id: int
    code: str
    kind: str
    count_locked: bool
    capacity_qty: str | None


class MobileUnit(StrictModel):
    id: str
    warehouse_id: int
    location_id: int
    sku_code: Annotated[str, StringConstraints(strip_whitespace=False, strict=True)]
    batch_id: int
    quality: str
    packaging_id: int | None = Field(gt=0, strict=True)
    package_state: str
    container: str
    qty: str
    reserved: str
    free: str
    blocked_reason: str | None


class MobileTaskDelivery(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    binding_revision_id: str
    carrier: str
    cutoff_at: str | None
    priority: bool
    overdue: bool
    blocked_reason: str | None


class MobileTask(StrictModel):
    id: str
    kind: str
    status: str
    unit_id: str | None
    hold_id: int | None
    qty: str
    destination_id: int | None
    container: str | None
    delivery: MobileTaskDelivery | None = None


class MobileCountSummary(StrictModel):
    id: str
    kind: str
    status: str
    locations: list[int]
    completed_locations: list[int]


class MobilePick(StrictModel):
    id: int
    unit_id: str
    source: str
    container: str
    binding_revision_id: UUID | None = None
    sku_code: str
    qty: str
    packed: bool
    batch_id: int
    batch_scan: str
    item_scan: str
    packaging_id: int | None
    package_state: str
    package_factor: str
    lot_no: str


class MobileCountLine(StrictModel):
    location_id: int
    sku_code: str
    batch_id: int
    qty: str | None
    quality: str
    package_state: str
    container: str


class MobileReceipt(StrictModel):
    operation_id: str
    kind: str
    warehouse_id: int
    status: Literal["recorded"] = "recorded"
    outcome: dict


class MobileIncomingTransfer(StrictModel):
    dispatch_operation_id: UUID
    source_warehouse_id: I32
    destination_warehouse_id: I32
    transit_unit_id: UUID
    remaining: PositiveQuantity = Field(le=MAX_MEASURE)
    sku_code: Annotated[str, StringConstraints(strip_whitespace=False, strict=True, min_length=1, max_length=64)]
    batch_id: I32
    packaging_id: I32 | None
    package_state: Literal["loose", "sealed", "open"]
    quality: Literal["quarantine", "released", "blocked"]
    container: ContainerCode
    batch_scan: RawScan
    container_scan: RawScan

    @model_validator(mode="after")
    def coherent_cargo(self):
        if (self.source_warehouse_id == self.destination_warehouse_id
                or self.batch_scan != f"LOT-{self.batch_id}-{str(self.transit_unit_id).upper()}"
                or self.container_scan != f"BOX-{self.container}"):
            raise ValueError("Несогласованные данные груза в пути")
        return self


class MobileIncomingTransferManifest(StrictModel):
    protocol: Literal["wms.mobile.transfer.manifest.v1"]
    warehouse_id: I32
    items: list[MobileIncomingTransfer]

    @model_validator(mode="after")
    def coherent_destination(self):
        if any(item.destination_warehouse_id != self.warehouse_id for item in self.items):
            raise ValueError("Поступление предназначено другому складу")
        return self


class MobileTransferOutcome(StrictModel):
    transfer: TransferReceipt
    unit: MobileUnit | None = None


class MobileTransferReceipt(StrictModel):
    protocol: Literal["wms.mobile.transfer.v1"] = "wms.mobile.transfer.v1"
    operation_id: UUID
    kind: Literal["transfer_dispatch", "transfer_receive"]
    warehouse_id: int = Field(gt=0, le=2147483647, strict=True)
    status: Literal["recorded"] = "recorded"
    outcome: MobileTransferOutcome

    @model_validator(mode="after")
    def coherent_side(self):
        transfer, unit = self.outcome.transfer, self.outcome.unit
        if self.kind == "transfer_dispatch":
            if (self.warehouse_id != transfer.source_warehouse_id
                    or self.operation_id != transfer.dispatch_operation_id
                    or transfer.received_unit_id is not None or unit is not None
                    or transfer.remaining != transfer.qty):
                raise ValueError("Несогласованная квитанция отправки")
        elif (self.warehouse_id != transfer.destination_warehouse_id
                or self.operation_id == transfer.dispatch_operation_id
                or transfer.received_unit_id is None or unit is None
                or unit.id != str(transfer.received_unit_id)
                or unit.warehouse_id != self.warehouse_id or unit.location_id <= 0):
            raise ValueError("Несогласованная квитанция приёмки")
        return self


class MobileTransferDiscrepancyReportReceipt(StrictModel):
    """Closed operational projection of one immutable pending report."""

    protocol: Literal["wms.transfer.discrepancy.v1"]
    kind: Literal["transfer_discrepancy_report"]
    operation_id: UUID
    organization_id: I32
    warehouse_id: I32
    report_operation_id: UUID
    dispatch_operation_id: UUID
    source_warehouse_id: I32
    destination_warehouse_id: I32
    source_unit_id: UUID
    transit_unit_id: UUID
    reported_qty: Annotated[str, StringConstraints(strict=True, pattern=r"(?:0|[1-9][0-9]{0,13})(?:\.[0-9]{1,6})?")]
    remaining_before: Annotated[str, StringConstraints(strict=True, pattern=r"(?:0|[1-9][0-9]{0,13})(?:\.[0-9]{1,6})?")]
    remaining_after: Annotated[str, StringConstraints(strict=True, pattern=r"(?:0|[1-9][0-9]{0,13})(?:\.[0-9]{1,6})?")]
    container: ContainerCode
    status: Literal["pending"]
    evidence: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=1000)]

    @model_validator(mode="after")
    def coherent_public_report(self):
        if (
            self.operation_id != self.report_operation_id
            or self.warehouse_id != self.destination_warehouse_id
            or self.source_warehouse_id == self.destination_warehouse_id
            or Decimal(self.reported_qty) > Decimal(self.remaining_before)
            or self.remaining_before != self.remaining_after
        ):
            raise ValueError("Несогласованная квитанция расхождения")
        return self


class MobileState(StrictModel):
    warehouse_id: int
    mode: str
    locations: list[MobileLocation]
    units: list[MobileUnit]
    picks: list[MobilePick] = Field(default_factory=list)
    tasks: list[MobileTask]
    counts: list[MobileCountSummary]


class MobileCount(StrictModel):
    count_id: str
    status: str
    locations: list[int]
    completed_locations: list[int]
    lines: list[MobileCountLine]


class MobileScan(StrictModel):
    packaging_id: int | None = Field(gt=0, strict=True)
    sku_code: Annotated[str, StringConstraints(strip_whitespace=False, strict=True)]
    factor: str
    sealed_allowed: bool
    gs1: dict | None
    dates_require_confirmation: bool


class MobileMetadataModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=False)


class MobileReceivingOwner(MobileMetadataModel):
    user_id: int = Field(gt=0)
    sub: str


class MobileReceivingPolicy(MobileMetadataModel):
    sku_code: str
    sku_id: int | None
    sku_title: str | None
    sku_unit: str | None
    base_unit: str
    precision: int = Field(ge=0, le=6)
    expiry_required: bool
    manufacture_required: bool
    min_shelf_days: int = Field(ge=0)
    max_age_days: int | None = Field(gt=0)
    maintenance_days: int | None = Field(gt=0)
    manufacturer_source: str
    storage_rule: str
    warranty_rule: str
    count_class: Literal["A", "B", "C"]


class MobileReceivingPackaging(MobileMetadataModel):
    id: int = Field(gt=0)
    sku_code: str
    factor: str
    sealed_allowed: bool


class MobileReceivingBatch(MobileMetadataModel):
    id: int = Field(gt=0)
    sku_code: str
    lot_no: str
    manufacture_date: str | None
    expiry_date: str | None
    date_source: str | None
    manufacture_precision: Literal["day", "month", "year"] | None


class MobileReceivingContext(MobileMetadataModel):
    organization_id: int = Field(gt=0)
    warehouse_id: int = Field(gt=0)
    mode: Literal["preparing", "erp"]
    owner: MobileReceivingOwner
    policies: list[MobileReceivingPolicy]
    packaging: list[MobileReceivingPackaging]
    batches: list[MobileReceivingBatch]
