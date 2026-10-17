"""Strict, decimal-only TSD commands; unknown fields never disappear silently."""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
    model_validator,
)


def exact_decimal(value):
    if isinstance(value, (float, bool)):
        raise ValueError("Передавайте количество десятичной строкой")
    return value


Quantity = Annotated[Decimal, BeforeValidator(exact_decimal), Field(ge=0, max_digits=20, decimal_places=6, allow_inf_nan=False)]
PositiveQuantity = Annotated[Decimal, BeforeValidator(exact_decimal), Field(gt=0, max_digits=20, decimal_places=6, allow_inf_nan=False)]
Text = Annotated[str, Field(min_length=1, max_length=1000)]
Code = Annotated[str, Field(min_length=1, max_length=64)]
RawScan = Annotated[str, StringConstraints(strip_whitespace=False), Field(strict=True, min_length=1, max_length=256)]
ContainerCode = Annotated[str, Field(pattern=r"^[A-Z0-9][A-Z0-9._-]{0,23}$")]
Measurement = Annotated[str, Field(min_length=1, max_length=100)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

CAPACITY_EDGE_WS = (
    "\u0009\u000a\u000b\u000c\u000d\u001c\u001d\u001e\u001f\u0020"
    "\u0085\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008"
    "\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)
CAPACITY_TEXT_PATTERN = (
    f"^[^{CAPACITY_EDGE_WS}]"
    f"(?:[\\s\\S]*[^{CAPACITY_EDGE_WS}])?$"
)

I32 = Annotated[int, Field(strict=True, ge=1, le=2147483647)]
I32Zero = Annotated[int, Field(strict=True, ge=0, le=2147483647)]
CapacityText = Annotated[str, StringConstraints(strip_whitespace=False), Field(strict=True, min_length=1, max_length=1000, pattern=CAPACITY_TEXT_PATTERN)]
def assembly_nonblank(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Assembly document content must contain non-whitespace text")
    return value


AssemblyDocumentText = Annotated[
    str, StringConstraints(strip_whitespace=False), Field(strict=True, min_length=1, max_length=32768), BeforeValidator(assembly_nonblank),
]
AssemblyWorkText = Annotated[
    str, StringConstraints(strip_whitespace=False), Field(strict=True, min_length=1, max_length=4000), BeforeValidator(assembly_nonblank),
]
CapacityCode = Annotated[str, StringConstraints(strip_whitespace=False), Field(strict=True, min_length=1, max_length=64, pattern=CAPACITY_TEXT_PATTERN)]
SourceRevision = Annotated[str, StringConstraints(strip_whitespace=False), Field(strict=True, min_length=1, max_length=200, pattern=CAPACITY_TEXT_PATTERN)]
HexDigest = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]

MAX_MEASURE = Decimal("99999999999999.999999")

def capacity_decimal(value):
    if isinstance(value, (float, bool)):
        raise ValueError("Передавайте точное десятичное значение строкой")
    if not isinstance(value, (str, int, Decimal)):
        raise ValueError("Недопустимое десятичное значение")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Недопустимое десятичное значение") from exc

    if not result.is_finite():
        raise ValueError("Значение должно быть конечным")
    if result.as_tuple().exponent < -6:
        raise ValueError("Допустимо не более шести десятичных знаков")
    if result > MAX_MEASURE:
        raise ValueError("Значение превышает Numeric(20,6)")

    # Normalize zero only after rejecting excess scale.
    if result == 0:
        return Decimal("0")
    return result

Measure = Annotated[
    Decimal, BeforeValidator(capacity_decimal),
    Field(ge=0, le=MAX_MEASURE, max_digits=20, decimal_places=6,
          allow_inf_nan=False),
]
PositiveMeasure = Annotated[
    Decimal, BeforeValidator(capacity_decimal),
    Field(gt=0, le=MAX_MEASURE, max_digits=20, decimal_places=6,
          allow_inf_nan=False),
]

ResourceKind = Literal[
    "warehouse", "storage_area", "floor", "rack", "shelf",
    "cell", "staging", "equipment",
]
Orientation = Literal["LWH", "LHW", "WLH", "WHL", "HLW", "HWL"]
LoadType = Literal["total_mass", "concentrated", "distributed"]
SupportRule = Literal["full_base", "layered"]
AccessMethod = Literal["manual", "equipment"]

def unique_values(values, label):
    if len(values) != len(set(values)):
        raise ValueError(f"Повторяющиеся {label}")

class CapacityCommandBase(StrictModel):
    protocol: Literal[2]
    operation_id: UUID
    warehouse_id: I32

class LocationMappingExpectation(StrictModel):
    location_id: I32
    expected_mapping_revision: I32Zero

class ResourceConfirm(CapacityCommandBase):
    kind: Literal["capacity_resource_confirm"]
    resource_id: UUID
    expected_revision: I32Zero
    parent_id: UUID | None
    resource_kind: ResourceKind
    location_mappings: list[LocationMappingExpectation] = Field(max_length=1000)
    source_ref: CapacityText
    source_revision: SourceRevision
    evidence: CapacityText

    @model_validator(mode="after")
    def validate_resource(self):
        if self.parent_id == self.resource_id:
            raise ValueError("Ресурс не может быть собственным предком")
        if (self.resource_kind == "warehouse") != (self.parent_id is None):
            raise ValueError("Только корневой warehouse не имеет предка")
        unique_values(
            [x.location_id for x in self.location_mappings], "ячейки",
        )
        return self

class PlacementSlotInput(StrictModel):
    slot_key: CapacityCode
    x_mm: Measure
    y_mm: Measure
    z_mm: Measure
    width_mm: PositiveMeasure
    height_mm: PositiveMeasure
    depth_mm: PositiveMeasure

class ProfileApprove(CapacityCommandBase):
    kind: Literal["capacity_profile_approve"]
    profile_id: UUID
    expected_revision: I32Zero
    expected_mapping_revision: I32 | None

    packaging_id: I32 | None
    location_id: I32 | None
    resource_id: UUID | None

    length_mm: PositiveMeasure | None
    width_mm: PositiveMeasure | None
    height_mm: PositiveMeasure | None
    opening_width_mm: PositiveMeasure | None
    opening_height_mm: PositiveMeasure | None
    clearance_mm: Measure | None
    gross_limit_kg: PositiveMeasure | None
    tare_kg: Measure | None
    top_load_limit_kg: Measure | None

    max_layers: I32 | None
    orientations: list[Orientation] | None = Field(max_length=6)
    load_type: LoadType | None
    support_rule: SupportRule | None
    access_method: AccessMethod | None
    equipment_ref: CapacityText | None
    restrictions: list[CapacityText] | None = Field(max_length=1000)

    slots: list[PlacementSlotInput] = Field(max_length=1000)
    source_ref: CapacityText
    source_revision: SourceRevision
    evidence: CapacityText

    @model_validator(mode="after")
    def validate_profile(self):
        targets = (self.packaging_id, self.location_id, self.resource_id)
        if sum(x is not None for x in targets) != 1:
            raise ValueError("Требуется ровно одна цель профиля")
        packaging = self.packaging_id is not None
        if packaging != (self.expected_mapping_revision is None):
            raise ValueError("Mapping revision требуется только не-packaging профилю")
        if self.orientations is not None:
            if not self.orientations:
                raise ValueError("Подтверждённые ориентации не могут быть пустыми")
            unique_values(self.orientations, "ориентации")
        if self.restrictions is not None:
            unique_values(self.restrictions, "ограничения")
        unique_values([s.slot_key for s in self.slots], "слоты")
        if self.slots and self.location_id is None:
            raise ValueError("Слоты допустимы только для location-профиля")
        if self.slots and any(x is None for x in (
            self.length_mm, self.width_mm, self.height_mm,
        )):
            raise ValueError("Для слотов требуется подтверждённый внешний envelope")
        if self.access_method == "equipment" and self.equipment_ref is None:
            raise ValueError("Неизвестно средство доступа")
        if self.access_method != "equipment" and self.equipment_ref is not None:
            raise ValueError("Средство доступа требует equipment")
        if self.gross_limit_kg is not None and self.tare_kg is not None:
            if self.tare_kg > self.gross_limit_kg:
                raise ValueError("Тара превышает полный предел массы")

        # X — длина; Y — высота; Z — ширина.
        for s in self.slots:
            if (s.x_mm + s.width_mm > self.length_mm
                    or s.y_mm + s.height_mm > self.height_mm
                    or s.z_mm + s.depth_mm > self.width_mm):
                raise ValueError("Слот выходит за подтверждённый envelope")
        for i, a in enumerate(self.slots):
            for b in self.slots[i + 1:]:
                intersects = (
                    a.x_mm < b.x_mm + b.width_mm
                    and b.x_mm < a.x_mm + a.width_mm
                    and a.y_mm < b.y_mm + b.height_mm
                    and b.y_mm < a.y_mm + a.height_mm
                    and a.z_mm < b.z_mm + b.depth_mm
                    and b.z_mm < a.z_mm + a.depth_mm
                )
                if intersects:
                    raise ValueError("Подтверждённые слоты пересекаются")
        return self

class CarrierPositionInput(StrictModel):
    location_profile_id: UUID
    slot_key: CapacityCode

class CarrierObserve(CapacityCommandBase):
    kind: Literal["capacity_carrier_observe"]
    carrier_id: UUID
    expected_revision: I32Zero
    expected_content_digest: HexDigest | None
    location_id: I32

    member_unit_ids: list[UUID] = Field(max_length=1000)
    source_operation_ids: list[UUID] = Field(max_length=1000)
    approved_count_id: UUID | None

    packaging_profile_id: UUID | None
    positions: list[CarrierPositionInput] = Field(max_length=1000)
    gross_kg: Measure | None
    tare_kg: Measure | None
    observed_empty: StrictBool
    confirm_measurements: StrictBool
    evidence: CapacityText

    @model_validator(mode="after")
    def validate_observation(self):
        unique_values(self.member_unit_ids, "stock units")
        unique_values(self.source_operation_ids, "source operations")
        unique_values(
            [(p.location_profile_id, p.slot_key) for p in self.positions],
            "позиции",
        )
        if self.expected_revision == 0:
            if self.expected_content_digest is not None:
                raise ValueError("Новый carrier не имеет прежнего content digest")
        elif self.expected_content_digest is None:
            raise ValueError("Требуется прежний content digest")
        if not self.source_operation_ids and self.approved_count_id is None:
            raise ValueError("Требуется физический источник или утверждённый пересчёт")
        if self.gross_kg is not None and self.tare_kg is not None:
            if self.gross_kg < self.tare_kg:
                raise ValueError("Полная масса меньше тары")
        if self.confirm_measurements:
            if self.gross_kg is None or self.tare_kg is None:
                raise ValueError("Подтверждение требует измеренной массы и тары")
        if self.observed_empty and self.member_unit_ids:
            raise ValueError("Пустой carrier не содержит положительных stock units")
        return self



# Equipment observations are metadata only; availability grants no placement rights.
EQUIPMENT_STATE_KIND = "capacity_equipment_state_observe"


def equipment_protocol(value):
    if type(value) is not int or value != 2:
        raise ValueError("equipment_protocol_requires_integer_two")
    return value


def equipment_date(value):
    if type(value) is not str or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        raise ValueError("equipment_date_requires_offset_iso_string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("equipment_date_requires_timezone")
    return parsed


EquipmentDate = Annotated[AwareDatetime, BeforeValidator(equipment_date)]
EquipmentProtocol = Annotated[Literal[2], BeforeValidator(equipment_protocol)]


class EquipmentStateObserve(StrictModel):
    protocol: EquipmentProtocol
    kind: Literal["capacity_equipment_state_observe"]
    operation_id: UUID
    warehouse_id: I32
    equipment_resource_id: UUID
    expected_state_revision: I32Zero
    expected_topology_revision: I32
    equipment_profile_id: UUID
    expected_profile_revision: I32
    expected_profile_digest: HexDigest
    state: Literal["available", "unavailable", "unknown"]
    valid_from: EquipmentDate
    valid_until: EquipmentDate
    source_ref: CapacityText
    source_revision: SourceRevision
    evidence: CapacityText
    operating_gross_limit_kg: PositiveMeasure | None
    operating_height_limit_mm: PositiveMeasure | None
    limits_source_ref: CapacityText | None
    limits_source_revision: SourceRevision | None
    limits_evidence: CapacityText | None

    @model_validator(mode="after")
    def equipment_values(self):
        if self.valid_until <= self.valid_from:
            raise ValueError("equipment_validity_interval")
        known = self.operating_gross_limit_kg is not None or self.operating_height_limit_mm is not None
        evidence = (self.limits_source_ref, self.limits_source_revision, self.limits_evidence)
        if (known and any(x is None for x in evidence)) or (not known and any(x is not None for x in evidence)):
            raise ValueError("equipment_limits_require_separate_evidence")
        return self


def normalize_equipment_request(data):
    from datetime import timezone

    result = data.model_dump(mode="json")
    for key in ("operation_id", "equipment_resource_id", "equipment_profile_id"):
        result[key] = str(getattr(data, key))
    for key in ("operating_gross_limit_kg", "operating_height_limit_mm"):
        value = getattr(data, key)
        result[key] = None if value is None else format(value, ".6f")
    for key in ("valid_from", "valid_until"):
        result[key] = getattr(data, key).astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return result


# Explicit physical context approval; it does not authorize placement.
MEASUREMENTS_KIND = "capacity_measurements_approve"
MEASUREMENTS_PRIVATE_KEY = "_placement_measured_context_v1"


class MeasurementSource(StrictModel):
    source_kind: Literal["measured", "manufacturer_confirmed"]
    source_ref: CapacityText
    source_revision: SourceRevision
    evidence: CapacityText
    measured_at: EquipmentDate | None


MeasuredT = TypeVar("MeasuredT")


class MeasurementConfirmed(StrictModel, Generic[MeasuredT]):
    state: Literal["confirmed"]
    value: MeasuredT
    source: MeasurementSource


class MeasurementUnknown(StrictModel):
    state: Literal["unknown"]
    reason: CapacityText


class MeasurementFrameNew(StrictModel):
    frame_key: CapacityCode
    kind: Literal["new"]
    anchor_location_id: I32
    datum_mark: CapacityText
    axes: Literal["x_length_y_height_z_width_v1"]
    source: MeasurementSource


class MeasurementFrameReuse(StrictModel):
    frame_key: CapacityCode
    kind: Literal["reuse"]
    anchor_location_id: I32
    datum_mark: CapacityText
    axes: Literal["x_length_y_height_z_width_v1"]
    source: MeasurementSource
    frame_id: UUID
    approval_operation_id: UUID


MeasurementFrame = Annotated[MeasurementFrameNew | MeasurementFrameReuse, Field(discriminator="kind")]


class MeasuredBox(StrictModel):
    x_mm: Measure
    y_mm: Measure
    z_mm: Measure
    length_mm: PositiveMeasure
    height_mm: PositiveMeasure
    width_mm: PositiveMeasure


class MeasuredSlot(StrictModel):
    slot_key: CapacityCode
    box: MeasuredBox


class MeasuredProfileFrame(StrictModel):
    profile_id: UUID
    expected_digest: HexDigest
    frame_key: CapacityCode
    relation: Literal["same_physical_datum"]
    source: MeasurementSource
    measured_slots: Annotated[MeasurementConfirmed[Annotated[list[MeasuredSlot], Field(max_length=1000)]] | MeasurementUnknown, Field(discriminator="state")]


class MeasuredOpening(StrictModel):
    phase_location_id: I32
    frame_key: CapacityCode
    width_axis: Literal["x", "y", "z"]
    height_axis: Literal["x", "y", "z"]
    width_mm: PositiveMeasure
    height_mm: PositiveMeasure
    clearance_mm: Measure
    source: MeasurementSource

    @model_validator(mode="after")
    def distinct_axes(self):
        if self.width_axis == self.height_axis:
            raise ValueError("measurement_opening_axes_equal")
        return self


class MeasuredCorridor(StrictModel):
    location_id: I32
    frame_key: CapacityCode
    box: MeasuredBox
    protected_unit_ids: list[UUID] = Field(max_length=1000)
    source: MeasurementSource

    @model_validator(mode="after")
    def unique_units(self):
        unique_values(self.protected_unit_ids, "protected units")
        return self


class MeasuredRoute(StrictModel):
    source_location_id: I32
    destination_location_id: I32
    source_profile_id: UUID
    destination_profile_id: UUID
    source_profile_digest: HexDigest
    destination_profile_digest: HexDigest
    phase_location_ids: list[I32] = Field(min_length=2, max_length=1000)
    slot_groups: list[Annotated[list[CapacityCode], Field(min_length=1, max_length=1000)]] = Field(min_length=1, max_length=1000)
    openings: Annotated[MeasurementConfirmed[Annotated[list[MeasuredOpening], Field(min_length=1, max_length=1000)]] | MeasurementUnknown, Field(discriminator="state")]
    corridors: Annotated[MeasurementConfirmed[Annotated[list[MeasuredCorridor], Field(max_length=1000)]] | MeasurementUnknown, Field(discriminator="state")]
    source: MeasurementSource

    @model_validator(mode="after")
    def route_shape(self):
        if self.source_location_id == self.destination_location_id:
            raise ValueError("measurement_route_same_endpoint")
        unique_values(self.phase_location_ids, "route phases")
        if self.phase_location_ids[0] != self.source_location_id or self.phase_location_ids[-1] != self.destination_location_id:
            raise ValueError("measurement_route_endpoint_order")
        unique_values([tuple(x) for x in self.slot_groups], "slot groups")
        for group in self.slot_groups:
            unique_values(group, "slot group keys")
        return self


class MeasuredResourceBearing(StrictModel):
    kind: Literal["resource"]
    resource_id: UUID


class MeasuredCarrierBearing(StrictModel):
    kind: Literal["carrier"]
    carrier_id: UUID
    observation_operation_id: UUID
    expected_content_revision: I32
    expected_content_digest: HexDigest


class MeasuredSupport(StrictModel):
    location_id: I32
    profile_id: UUID
    profile_digest: HexDigest
    frame_key: CapacityCode
    plane_y_mm: Measure
    x_min_mm: Measure
    x_max_mm: Measure
    z_min_mm: Measure
    z_max_mm: Measure
    bearing: Annotated[MeasuredResourceBearing | MeasuredCarrierBearing, Field(discriminator="kind")]
    layer: I32Zero
    top_load_limit_kg: Measure | None
    source: MeasurementSource

    @model_validator(mode="after")
    def positive_intervals(self):
        if self.x_min_mm >= self.x_max_mm or self.z_min_mm >= self.z_max_mm:
            raise ValueError("measurement_support_intervals")
        if (self.bearing.kind == "resource") != (self.layer == 0):
            raise ValueError("measurement_support_layer_kind")
        return self


class MeasuredCarrierRelation(StrictModel):
    carrier_id: UUID
    observation_operation_id: UUID
    expected_content_revision: I32
    expected_content_digest: HexDigest
    location_profile_id: UUID
    slot_keys: list[CapacityCode] = Field(min_length=1, max_length=1000)
    support_carrier_id: UUID | None
    frame_key: CapacityCode
    source: MeasurementSource

    @model_validator(mode="after")
    def relation_shape(self):
        unique_values(self.slot_keys, "carrier slots")
        if self.carrier_id == self.support_carrier_id:
            raise ValueError("measurement_self_support")
        return self


class CapacityMeasurementsApprove(CapacityCommandBase):
    protocol: EquipmentProtocol
    kind: Literal["capacity_measurements_approve"]
    expected_revision: I32Zero
    previous_operation_id: UUID | None
    frames: list[MeasurementFrame] = Field(max_length=1000)
    profile_frames: list[MeasuredProfileFrame] = Field(max_length=1000)
    routes: Annotated[MeasurementConfirmed[Annotated[list[MeasuredRoute], Field(max_length=1000)]] | MeasurementUnknown, Field(discriminator="state")]
    supports: Annotated[MeasurementConfirmed[Annotated[list[MeasuredSupport], Field(max_length=1000)]] | MeasurementUnknown, Field(discriminator="state")]
    carrier_support_relations: Annotated[MeasurementConfirmed[Annotated[list[MeasuredCarrierRelation], Field(max_length=1000)]] | MeasurementUnknown, Field(discriminator="state")]
    source: MeasurementSource

    @model_validator(mode="after")
    def unique_context(self):
        unique_values([x.frame_key for x in self.frames], "frame keys")
        unique_values([x.profile_id for x in self.profile_frames], "profile frame IDs")
        if (self.expected_revision == 0) != (self.previous_operation_id is None):
            raise ValueError("measurement_previous_revision")
        return self


def normalize_measurements_request(data):
    """Only this new kind; historical native snapshot bytes use their own codec."""
    from datetime import timezone
    from modules.wms.physical_models import _cap_fixed

    def walk(value):
        if isinstance(value, BaseModel):
            return {key: walk(getattr(value, key)) for key in type(value).model_fields}
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, Decimal):
            return _cap_fixed(value)
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        if isinstance(value, list):
            return [walk(x) for x in value]
        return value
    return walk(data)


CapacityCommand = Annotated[
    ResourceConfirm | ProfileApprove | CarrierObserve | EquipmentStateObserve | CapacityMeasurementsApprove,
    Field(discriminator="kind"),
]


class ServiceResult(StrictModel):
    result: Literal["passed", "failed"]
    evidence: Text
    measurements: dict[Annotated[str, Field(min_length=1, max_length=64)], Measurement] = Field(min_length=1, max_length=30)


class Command(StrictModel):
    operation_id: UUID
    warehouse_id: int = Field(gt=0, strict=True)


class Receive(Command):
    kind: Literal["receive", "return"]
    sku_code: Code
    batch_id: int = Field(gt=0, strict=True)
    location_id: int = Field(gt=0, strict=True)
    qty: PositiveQuantity
    characteristic: str = Field(default="", max_length=128)
    purpose: str = Field(default="", max_length=128)
    packaging_id: int | None = Field(default=None, gt=0, strict=True)
    package_state: Literal["loose", "sealed", "open"] = "loose"
    source_ref: Text
    depends_on: UUID | None = None


class UnitCommand(Command):
    unit_id: UUID
    qty: PositiveQuantity


class Quality(UnitCommand):
    kind: Literal["quality"]
    quality: Literal["released", "blocked"]
    evidence: Text


class Maintenance(UnitCommand):
    kind: Literal["maintenance"]
    result: Literal["passed", "failed"]
    checked_on: date
    next_service: date | None = None
    evidence: Text
    measurements: dict[Annotated[str, Field(min_length=1, max_length=64)], Measurement] = Field(min_length=1, max_length=30)


class Move(UnitCommand):
    kind: Literal["move"]
    destination_id: int = Field(gt=0, strict=True)
    destination_warehouse_id: int | None = Field(default=None, gt=0, strict=True)
    depends_on: UUID | None = None
    evidence: Text


class TransferDispatch(UnitCommand):
    kind: Literal["transfer_dispatch"]
    qty: PositiveQuantity = Field(le=MAX_MEASURE)
    warehouse_id: I32
    destination_warehouse_id: I32
    container: ContainerCode
    location_scan: RawScan
    item_scan: RawScan
    batch_scan: RawScan
    container_scan: RawScan
    evidence: Text

    @model_validator(mode="after")
    def different_warehouses(self):
        if self.warehouse_id == self.destination_warehouse_id:
            raise ValueError("Межскладская отправка требует разных складов")
        return self


class TransferReceive(UnitCommand):
    kind: Literal["transfer_receive"]
    qty: PositiveQuantity = Field(le=MAX_MEASURE)
    warehouse_id: I32
    dispatch_operation_id: UUID
    destination_id: I32
    location_scan: RawScan
    item_scan: RawScan
    batch_scan: RawScan
    container_scan: RawScan
    evidence: Text

    @model_validator(mode="after")
    def different_operations(self):
        if self.operation_id == self.dispatch_operation_id:
            raise ValueError("Приёмка требует отдельного идентификатора операции")
        return self


TransferCommand = Annotated[TransferDispatch | TransferReceive, Field(discriminator="kind")]


class TransferReceipt(StrictModel):
    protocol: Literal["wms.transfer.v1"]
    dispatch_operation_id: UUID
    source_warehouse_id: I32
    destination_warehouse_id: I32
    source_unit_id: UUID
    transit_unit_id: UUID
    received_unit_id: UUID | None
    qty: PositiveQuantity = Field(le=MAX_MEASURE)
    remaining: Quantity = Field(le=MAX_MEASURE)
    container: ContainerCode

    @model_validator(mode="after")
    def distinct_identities(self):
        if self.source_warehouse_id == self.destination_warehouse_id:
            raise ValueError("Межскладская операция требует разных складов")
        unit_ids = [self.source_unit_id, self.transit_unit_id]
        if self.received_unit_id is not None:
            unit_ids.append(self.received_unit_id)
        if len(set(unit_ids)) != len(unit_ids):
            raise ValueError("Остатки перехода должны иметь разные идентификаторы")
        return self


class OpenPackage(UnitCommand):
    kind: Literal["open_package"]
    evidence: Text


class Reserve(Command):
    kind: Literal["reserve"]
    sku_code: Code
    qty: PositiveQuantity
    source: str = Field(min_length=1, max_length=200)
    container: ContainerCode
    characteristic: str = Field(default="", max_length=128)
    purpose: str = Field(default="", max_length=128)
    min_shelf_days: int = Field(default=0, ge=0, strict=True)
    due_at: AwareDatetime | None = None


class Release(Command):
    kind: Literal["release"]
    source: str = Field(min_length=1, max_length=200)
    evidence: Text


class Pick(Command):
    kind: Literal["pick"]
    hold_id: int = Field(gt=0, strict=True)
    qty: PositiveQuantity
    location_scan: Code
    batch_scan: Code
    item_scan: str = Field(min_length=1, max_length=256)
    container_scan: Code
    destination_id: int = Field(gt=0, strict=True)
    task_id: UUID | None = None


class Unpick(Command):
    kind: Literal["unpick"]
    pick_id: int = Field(gt=0, strict=True)
    evidence: Text


class Pack(Command):
    kind: Literal["pack"]
    source: str = Field(min_length=1, max_length=200)
    container: Code
    destination_id: int = Field(gt=0, strict=True)
    confirmed: dict[str, PositiveQuantity] = Field(min_length=1)
    evidence: Text


class ShipmentLine(StrictModel):
    pick_id: int = Field(gt=0, strict=True)
    qty: PositiveQuantity


class PackV2Line(StrictModel):
    pick_id: I32
    batch_scan: RawScan
    item_scan: RawScan
    qty: PositiveQuantity


class ShippingPackage(StrictModel):
    code: CapacityCode
    gross_kg: PositiveMeasure
    lines: list[ShipmentLine] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_contents(self):
        unique_values([line.pick_id for line in self.lines], "строки отправляемого места")
        return self


class PackV2(Command):
    kind: Literal["pack_v2"]
    protocol: Literal[2]
    source: str = Field(min_length=1, max_length=200)
    container: Code
    destination_id: int = Field(gt=0, strict=True)
    container_scan: RawScan
    lines: list[PackV2Line] = Field(min_length=1, max_length=1000)
    shipping_packages: list[ShippingPackage] | None = Field(default=None, min_length=1, max_length=1000)
    evidence: Text
    binding_revision_id: UUID | None = None

    @model_validator(mode="before")
    @classmethod
    def bounded_shipping_contents(cls, value):
        if isinstance(value, dict) and isinstance(value.get("shipping_packages"), list):
            count = 0
            for package in value["shipping_packages"]:
                if isinstance(package, dict) and isinstance(package.get("lines"), list):
                    count += len(package["lines"])
                    if count > 10000:
                        raise ValueError("В отправляемых местах допустимо не более 10000 строк")
        return value

    @model_validator(mode="after")
    def unique_pick_lines(self):
        unique_values([line.pick_id for line in self.lines], "идентификаторы подбора")
        if self.shipping_packages is not None:
            unique_values([package.code for package in self.shipping_packages], "коды отправляемых мест")
            if sum(len(package.lines) for package in self.shipping_packages) > 10000:
                raise ValueError("В отправляемых местах допустимо не более 10000 строк")
            actual = {}
            for package in self.shipping_packages:
                for line in package.lines:
                    actual[line.pick_id] = actual.get(line.pick_id, Decimal("0")) + line.qty
            if actual != {line.pick_id: line.qty for line in self.lines}:
                raise ValueError("Состав отправляемых мест должен точно совпадать с подбором")
            if sum((package.gross_kg for package in self.shipping_packages), Decimal("0")) > MAX_MEASURE:
                raise ValueError("Общий вес отправляемых мест превышает допустимый")
        return self

    def shipping_package_proof(self):
        if self.shipping_packages is None:
            return None
        proof = {
            "source": self.source,
            "container": self.container,
            "packages": [package.model_dump(mode="json") for package in self.shipping_packages],
            "place_count": len(self.shipping_packages),
            "gross_kg_total": format(
                sum((package.gross_kg for package in self.shipping_packages), Decimal("0")), "f",
            ),
        }
        if self.binding_revision_id is not None:
            proof["binding_revision_id"] = str(self.binding_revision_id)
        return proof


class DeliveryBindingAllocation(StrictModel):
    task_id: UUID
    qty: PositiveQuantity


class DeliveryBind(Command):
    kind: Literal["delivery_bind"]
    execution_id: UUID
    source_kind: Text
    source_key: Text
    source_revision: StrictStr | StrictInt
    expected_intent_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_source_state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_id: int = Field(gt=0, strict=True)
    expected_version: int = Field(gt=0, strict=True)
    expected_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_reservation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_revision_id: UUID | None = None
    allocations: list[DeliveryBindingAllocation] = Field(min_length=1, max_length=1000)
    evidence: Text

    @model_validator(mode="before")
    @classmethod
    def strict_source_revision(cls, value):
        if isinstance(value, dict):
            revision = value.get("source_revision")
            if revision is not None and type(revision) not in (str, int):
                raise ValueError("Версия первичного источника должна быть строкой или целым числом")
        return value

    @model_validator(mode="after")
    def unique_binding_tasks(self):
        unique_values([row.task_id for row in self.allocations], "задания привязки доставки")
        if self.previous_revision_id is not None:
            raise ValueError("Замена привязки доставки в этой версии не поддерживается")
        return self


class Ship(Command):
    kind: Literal["ship"]
    source: str = Field(min_length=1, max_length=200)
    lines: list[ShipmentLine] = Field(min_length=1, max_length=1000)
    container_scans: dict[ContainerCode, Code] = Field(default_factory=dict, max_length=1000)
    shipment_mode: Literal["full", "partial"] = "full"
    partial_reason: str = Field(default="", max_length=1000)
    accompanying_documents: list[Text] = Field(min_length=1, max_length=30)
    depends_on: UUID | None = None
    correction_of: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def partial_confirmation(self):
        if self.shipment_mode == "partial" and not self.partial_reason:
            raise ValueError("Для частичной отгрузки укажите причину")
        if self.shipment_mode == "full" and self.partial_reason:
            raise ValueError("Причина частичной отгрузки требует явного выбора частичной отгрузки")
        return self


class TaskClaim(Command):
    kind: Literal["task_claim"]
    task_id: UUID


class Replenish(Command):
    kind: Literal["replenish"]
    sku_code: Code | None = None
    demand: dict[str, Quantity] = Field(default_factory=dict)


class TaskComplete(Command):
    kind: Literal["task_complete"]
    task_id: UUID
    evidence: Text


class CountStart(Command):
    kind: Literal["count_start"]
    count_kind: Literal["opening", "cycle", "exception"]
    location_ids: list[Annotated[int, Field(gt=0, strict=True)]] = Field(min_length=1, max_length=10000)
    evidence: Text


class CountRecord(Command):
    kind: Literal["count_record"]
    count_id: UUID
    location_id: int = Field(gt=0, strict=True)
    unit_id: UUID | None = None
    sku_code: Code
    batch_id: int = Field(gt=0, strict=True)
    qty: Quantity | None = None
    characteristic: str = Field(default="", max_length=128)
    purpose: str = Field(default="", max_length=128)
    packaging_id: int | None = Field(default=None, gt=0, strict=True)
    package_state: Literal["loose", "sealed", "open"] = "loose"
    quality: Literal["quarantine", "released", "blocked"]
    last_checked: date | None = None
    next_service: date | None = None
    inspection_ref: str = Field(default="", max_length=1000)
    container: str = Field(default="", max_length=64)
    arrived_at: datetime | None = None
    arrival_source: str = Field(default="", max_length=1000)
    service_result: ServiceResult | None = None


class AdoptReservation(Command):
    kind: Literal["adopt_reservation"]
    reservation_id: int = Field(gt=0, strict=True)
    expected_version: int = Field(gt=0, strict=True)
    expected_qty: PositiveQuantity
    container: ContainerCode
    characteristic: str = Field(default="", max_length=128)
    purpose: str = Field(default="", max_length=128)
    evidence: Text


class CountFinishLocation(Command):
    kind: Literal["count_location_complete"]
    count_id: UUID
    location_id: int = Field(gt=0, strict=True)
    empty_confirmed: bool = Field(default=False, strict=True)


class CountDecision(Command):
    kind: Literal["count_approve", "count_cancel"]
    count_id: UUID
    evidence: Text
    review_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    depends_on: UUID | None = None


class Cutover(Command):
    kind: Literal["cutover"]
    opening_count_id: UUID
    source_snapshot_ref: Text
    source_matches_working_base: bool = Field(strict=True)
    open_orders_reviewed: bool = Field(strict=True)
    transferred_reserves: list[str] = Field(default_factory=list, max_length=10000)
    previously_posted_documents: list[str] = Field(default_factory=list, max_length=10000)
    evidence: Text


class AssemblyBind(Command):
    kind: Literal["assembly_bind"]
    specification_id: int = Field(gt=0, strict=True)
    container: ContainerCode
    output_keys: list[Code] = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def unique_output_keys(self):
        if len(set(self.output_keys)) != len(self.output_keys):
            raise ValueError("Ключ изделия в заказе сборки не может повторяться")
        return self


class AssemblyKitMove(Command):
    kind: Literal["assembly_kit_move"]
    execution_id: UUID
    task_id: UUID
    hold_id: int = Field(gt=0, strict=True)
    qty: PositiveQuantity
    location_scan: Code
    batch_scan: Code
    item_scan: str = Field(min_length=1, max_length=256)
    container_scan: Code
    destination_id: int = Field(gt=0, strict=True)


class AssemblyComponentScan(StrictModel):
    input_link_id: int = Field(gt=0, strict=True)
    location_scan: Code
    batch_scan: Code
    item_scan: str = Field(min_length=1, max_length=256)
    container_scan: Code


class AssemblyConsume(Command):
    kind: Literal["assembly_consume"]
    execution_id: UUID
    output_key: Code
    scans: list[AssemblyComponentScan] = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def unique_scans(self):
        ids = [row.input_link_id for row in self.scans]
        if len(ids) != len(set(ids)):
            raise ValueError("Строка комплекта для списания не может повторяться")
        return self


class AssemblyReturnLine(StrictModel):
    input_link_id: int = Field(gt=0, strict=True)
    qty: PositiveQuantity
    location_scan: Code
    batch_scan: Code
    item_scan: str = Field(min_length=1, max_length=256)
    container_scan: Code


class AssemblyHoldRelease(StrictModel):
    hold_id: int = Field(gt=0, strict=True)
    qty: PositiveQuantity


class AssemblyUnusedReturn(Command):
    kind: Literal["assembly_unused_return"]
    execution_id: UUID
    returns: list[AssemblyReturnLine] = Field(default_factory=list, max_length=5000)
    hold_releases: list[AssemblyHoldRelease] = Field(default_factory=list, max_length=5000)
    evidence: Text

    @model_validator(mode="after")
    def unique_links_and_holds(self):
        links = [line.input_link_id for line in self.returns]
        holds = [line.hold_id for line in self.hold_releases]
        if not self.returns and not self.hold_releases:
            raise ValueError("Укажите фактически возвращаемый комплект или освобождаемый резерв")
        if len(links) != len(set(links)) or len(holds) != len(set(holds)):
            raise ValueError("Строка возврата в сборку не может повторяться")
        return self


def assembly_uuid(value):
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Assembly identifiers must be canonical UUID strings")
    return value


def assembly_date(value):
    if type(value) is date:
        return value
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Assembly dates must use ISO calendar dates")
    return value


AssemblyUUID = Annotated[UUID, BeforeValidator(assembly_uuid)]
AssemblyDate = Annotated[date, BeforeValidator(assembly_date)]
AssemblyProtocol = Annotated[int, Field(strict=True, ge=1, le=1)]
AssemblyIssuedAt = Annotated[int, Field(strict=True)]


class AssemblyProofModel(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False, frozen=True)


class AssemblyPrincipalProof(AssemblyProofModel):
    user_id: I32
    sub: SourceRevision
    issued_at: AssemblyIssuedAt | None


class AssemblyExecutorProof(AssemblyProofModel):
    original: AssemblyPrincipalProof
    current: AssemblyPrincipalProof

    @model_validator(mode="after")
    def same_call_identity(self):
        if self.original != self.current:
            raise ValueError("Original assembly caller changed during authorization")
        return self


class AssemblyProcessAnchor(AssemblyProofModel):
    organization_id: I32
    order_id: I32
    run_id: AssemblyUUID
    confirmed_revision_id: AssemblyUUID
    confirmed_revision: I32
    confirmed_digest: HexDigest
    prepared_revision_id: AssemblyUUID
    prepared_digest: HexDigest
    composition_digest: HexDigest
    policy_inputs_digest: HexDigest
    designated_first_output_key: CapacityCode


class AssemblyBindProof(AssemblyProofModel):
    protocol: AssemblyProtocol
    execution_id: AssemblyUUID
    anchor: AssemblyProcessAnchor
    executor: AssemblyExecutorProof


class AssemblyConsumeProof(AssemblyProofModel):
    protocol: AssemblyProtocol
    execution_id: AssemblyUUID
    bind_operation_id: AssemblyUUID
    anchor_digest: HexDigest
    output_key: CapacityCode
    executor: AssemblyExecutorProof


class AssemblyEffectiveLot(AssemblyProofModel):
    batch_id: I32
    sku_code: Code
    lot_no: Code
    mfg_date: AssemblyDate | None
    expiry_date: AssemblyDate | None
    manufacture_precision: Literal["day", "month", "year"] | None
    date_source: Text


class AssemblyOutput(Command):
    operation_id: AssemblyUUID
    kind: Literal["assembly_output"]
    execution_id: AssemblyUUID
    output_key: CapacityCode
    batch_id: I32
    location_id: I32
    confirmed_revision_id: AssemblyUUID
    policy_inputs_digest: HexDigest
    serial_number: RawScan | None
    mfg_date: AssemblyDate | None
    expiry_date: AssemblyDate | None
    evidence: CapacityText


class AssemblyQcObservation(AssemblyProofModel):
    kind: Literal["observation"]
    criterion_id: CapacityCode
    result: CapacityText
    evidence: CapacityText


class AssemblyQcMeasurement(AssemblyProofModel):
    kind: Literal["measurement"]
    criterion_id: CapacityCode
    value: Annotated[str, StringConstraints(strip_whitespace=False), Field(strict=True, min_length=1, max_length=100)]
    unit: CapacityCode
    evidence: CapacityText

    @model_validator(mode="after")
    def plain_finite_value(self):
        if re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", self.value) is None:
            raise ValueError("QC measurement requires a signed plain decimal string")
        value = Decimal(self.value)
        if not value.is_finite() or len(value.as_tuple().digits) > 20:
            raise ValueError("QC measurement exceeds maximum digits")
        return self


AssemblyQcCriterion = Annotated[AssemblyQcObservation | AssemblyQcMeasurement, Field(discriminator="kind")]


class AssemblyQc(Command):
    operation_id: AssemblyUUID
    kind: Literal["assembly_qc"]
    identity_id: AssemblyUUID
    unit_id: AssemblyUUID
    qc_birth_operation_id: AssemblyUUID
    confirmed_revision_id: AssemblyUUID
    criteria: list[AssemblyQcCriterion] = Field(min_length=1, max_length=200)
    evidence: CapacityText

    @model_validator(mode="after")
    def exact_criterion_ids(self):
        ids = [row.criterion_id for row in self.criteria]
        if len(ids) != len(set(ids)):
            raise ValueError("QC criterion IDs cannot repeat")
        return self


class AssemblyReturn(Command):
    operation_id: AssemblyUUID
    kind: Literal["assembly_return"]
    identity_id: AssemblyUUID
    ship_operation_id: AssemblyUUID
    pick_id: I32
    location_id: I32
    evidence: CapacityText


class AssemblyOutputProof(AssemblyProofModel):
    protocol: AssemblyProtocol
    identity_id: AssemblyUUID
    execution_id: AssemblyUUID
    output_key: CapacityCode
    anchor: AssemblyProcessAnchor
    genealogy_digest: HexDigest
    consume_operation_id: AssemblyUUID
    executor: AssemblyExecutorProof
    effective_lot: AssemblyEffectiveLot


class AssemblyQcProof(AssemblyProofModel):
    protocol: AssemblyProtocol
    command: AssemblyQc
    anchor_digest: HexDigest
    executor: AssemblyExecutorProof
    criteria_digest: HexDigest
    verdict: Literal["PASS", "FAIL"]
    released_unit_id: AssemblyUUID | None


class AssemblyReturnProof(AssemblyProofModel):
    protocol: AssemblyProtocol
    identity_id: AssemblyUUID
    ship_operation_id: AssemblyUUID
    ship_request_hash: HexDigest
    pick_id: I32
    shipped_unit_id: AssemblyUUID
    ship_ledger_id: I32
    pre_ship_frontier_digest: HexDigest
    returned_unit_id: AssemblyUUID
    executor: AssemblyExecutorProof


WarehouseCommand = Annotated[Receive | Quality | Maintenance | Move | OpenPackage | Reserve | Release | AdoptReservation | Pick | Unpick | Pack | PackV2 | Ship | DeliveryBind | TaskClaim | Replenish | TaskComplete | CountStart | CountRecord | CountFinishLocation | CountDecision | Cutover | AssemblyBind | AssemblyKitMove | AssemblyConsume | AssemblyUnusedReturn | AssemblyOutput | AssemblyQc | AssemblyReturn, Field(discriminator="kind")]


class WarehouseSetup(StrictModel):
    onec_ref: str | None = Field(min_length=1, max_length=128)


class LocationSetup(StrictModel):
    code: Code
    title: str = Field(default="", max_length=255)
    kind: Literal["receiving", "storage", "pick", "packing", "dispatch", "returns", "quarantine", "service"]
    capacity_sku: Code | None = None
    capacity_qty: PositiveQuantity | None = None
    load_evidence: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def capacity_scope(self):
        if (self.capacity_qty is None) != (self.capacity_sku is None):
            raise ValueError("Вместимость в базовых единицах задаётся для конкретного товара")
        return self


def strict_assembly_station_operation_id(value):
    if not isinstance(value, str):
        raise ValueError("Assembly station operation_id must be a UUID string")
    return value


AssemblyStationOperationId = Annotated[UUID, BeforeValidator(strict_assembly_station_operation_id)]


class AssemblyStationRegister(StrictModel):
    schema_version: int = Field(strict=True, ge=1, le=1)
    operation_id: AssemblyStationOperationId
    code: Annotated[str, Field(strict=True, min_length=1, max_length=64)]
    title: Annotated[str, Field(strict=True, max_length=255)] = ""
    confirmed_empty: StrictBool
    evidence: Annotated[str, Field(strict=True, min_length=1, max_length=1000)]
    reason: Annotated[str, Field(strict=True, min_length=1, max_length=1000)]

    @model_validator(mode="after")
    def require_confirmed_empty(self):
        if self.confirmed_empty is not True:
            raise ValueError("Assembly station must be explicitly confirmed empty")
        return self


def responsible_operation_uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Responsible operation_id must be a canonical UUID string")
    return value


ResponsibleOperationId = Annotated[UUID, BeforeValidator(responsible_operation_uuid)]
ResponsibleSubject = Annotated[
    str, StringConstraints(strip_whitespace=False),
    Field(strict=True, min_length=1, max_length=64, pattern=CAPACITY_TEXT_PATTERN),
]


class WarehouseResponsibleSet(StrictModel):
    schema_version: int = Field(strict=True, ge=1, le=1)
    operation_id: ResponsibleOperationId
    expected_revision: I32Zero
    active: StrictBool
    user_id: I32 | None = None
    evidence: CapacityText
    reason: CapacityText

    @model_validator(mode="after")
    def require_exact_candidate(self):
        if self.active != (self.user_id is not None):
            raise ValueError("Active assignment requires user_id; revoke requires null user_id")
        return self


class WarehouseResponsibleProof(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    setter_user_id: I32
    setter_subject: ResponsibleSubject
    assignee_subject: ResponsibleSubject
    request: WarehouseResponsibleSet


class WarehouseResponsibleReceipt(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    schema_version: int = Field(strict=True, ge=1, le=1)
    kind: Literal["warehouse_responsible_set"]
    organization_id: I32
    warehouse_id: I32
    operation_id: ResponsibleOperationId
    previous_revision: I32Zero
    revision: I32
    user_id: I32
    active: StrictBool
    evidence: CapacityText
    reason: CapacityText
    warehouse_responsible_v1: WarehouseResponsibleProof

    @model_validator(mode="after")
    def require_request_binding(self):
        request = self.warehouse_responsible_v1.request
        if (
            self.revision != self.previous_revision + 1
            or self.previous_revision != request.expected_revision
            or self.operation_id != request.operation_id
            or self.active != request.active
            or (self.active and self.user_id != request.user_id)
            or self.evidence != request.evidence or self.reason != request.reason
        ):
            raise ValueError("Responsible receipt does not match its immutable request")
        return self


class PolicySetup(StrictModel):
    sku_code: Code
    precision: int = Field(default=0, ge=0, le=6, strict=True)
    base_unit: str = Field(default="шт", min_length=1, max_length=16)
    expiry_required: bool = False
    manufacture_required: bool = False
    min_shelf_days: int = Field(default=0, ge=0, strict=True)
    max_age_days: int | None = Field(default=None, gt=0, strict=True)
    maintenance_days: int | None = Field(default=None, gt=0, strict=True)
    manufacturer_source: Text
    storage_rule: str = Field(default="", max_length=1000)
    warranty_rule: str = Field(default="", max_length=1000)
    count_class: Literal["A", "B", "C"] = "C"


class PackagingSetup(StrictModel):
    sku_code: Code
    barcode: Code
    title: str = Field(min_length=1, max_length=128)
    factor: PositiveQuantity
    sealed_allowed: bool = False
    evidence: Text


class BatchSetup(StrictModel):
    sku_code: Code
    lot_no: Code
    date_source: Text
    mfg_date: date | None = None
    expiry_date: date | None = None
    manufacture_precision: Literal["day", "month", "year"] | None = None
    supplier: str | None = Field(default=None, max_length=256)
    external_ref: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def dates(self):
        if self.mfg_date and self.expiry_date and self.mfg_date > self.expiry_date:
            raise ValueError("Дата производства позже окончания годности")
        if self.mfg_date and not self.manufacture_precision:
            raise ValueError("Укажите точность даты производства")
        return self


class PickRuleSetup(StrictModel):
    sku_code: Code
    location_id: int = Field(gt=0, strict=True)
    min_qty: Quantity
    max_qty: PositiveQuantity

    @model_validator(mode="after")
    def range(self):
        if self.max_qty <= self.min_qty:
            raise ValueError("Максимум должен превышать минимум")
        return self


class ReconciliationRow(StrictModel):
    sku_code: Code
    qty: Annotated[Decimal, BeforeValidator(exact_decimal), Field(max_digits=20, decimal_places=6, allow_inf_nan=False)]
    characteristic: str = Field(default="", max_length=128)
    purpose: str = Field(default="", max_length=128)


class ReconciliationInput(StrictModel):
    snapshot_ref: Text
    warehouse_ref: str = Field(min_length=1, max_length=128)
    complete: bool = Field(strict=True)
    scope: Literal["sku", "sku_characteristic_purpose"]
    rows: list[ReconciliationRow] = Field(max_length=100000)


CarrierPlanningCode = Literal[
    "carrier_membership_unknown", "profile_binding_stale",
    "packaging_profile_unknown", "packaging_geometry_unknown",
    "gross_limit_exceeded", "position_not_observed",
    "observed_position_overlap", "mass_measurement_unknown",
    "placement_evaluator_unavailable",
]
class CapacityOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class MeasuredContextViolation(CapacityOutput):
    code: Literal["measured_slots_unknown", "routes_unknown", "openings_unknown", "corridors_unknown", "supports_unknown", "carrier_relations_unknown", "support_top_load_unknown"]
    refs: list[CapacityCode]


class MeasuredContextFrameAck(CapacityOutput):
    frame_key: CapacityCode
    frame_id: UUID


class MeasuredContextAck(CapacityOutput):
    protocol: EquipmentProtocol
    operation_id: UUID
    kind: Literal["capacity_measurements_approve"]
    warehouse_id: I32
    revision: I32
    digest: HexDigest
    status: Literal["confirmed", "incomplete"]
    violations: list[MeasuredContextViolation]
    frames: list[MeasuredContextFrameAck]


class EquipmentAncestor(CapacityOutput):
    resource_id: UUID
    parent_id: UUID | None
    topology_revision: I32


class EquipmentResourceBinding(CapacityOutput):
    resource_id: UUID
    warehouse_id: I32
    kind: Literal["equipment"]
    parent_id: UUID
    topology_revision: I32
    ancestor_binding: list[EquipmentAncestor] = Field(min_length=2)


class EquipmentProfileBinding(CapacityOutput):
    profile_id: UUID
    revision: I32
    digest: HexDigest
    canonical_payload: str = Field(strict=True, min_length=1)
    bound_mapping_revision: I32
    ancestor_binding: list[EquipmentAncestor] = Field(min_length=2)


class EquipmentStateReceipt(CapacityOutput):
    protocol: EquipmentProtocol
    operation_id: UUID
    kind: Literal["capacity_equipment_state_observe"]
    organization_id: I32
    warehouse_id: I32
    equipment_resource_id: UUID
    state_revision: I32
    previous_operation_id: UUID | None
    actor_user_id: I32
    actor_sub: SourceRevision
    approved_at: EquipmentDate
    request_hash: HexDigest
    request: EquipmentStateObserve
    resource_binding: EquipmentResourceBinding
    profile_binding: EquipmentProfileBinding
    canonical_payload: str = Field(strict=True, min_length=1)
    digest: HexDigest


def equipment_receipt(value):
    """Strict full JSON decoder; digest proves selected bytes, not authority."""
    import json
    from datetime import timezone

    from modules.wms.physical_stock import canonical, digest

    if type(value) is not dict:
        raise ValueError("equipment_receipt_shape")
    parsed = EquipmentStateReceipt.model_validate(value)
    request = normalize_equipment_request(parsed.request)
    if value["request"] != request:
        raise ValueError("equipment_request_not_canonical")
    approved = parsed.approved_at.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if value["approved_at"] != approved:
        raise ValueError("equipment_approved_at_not_canonical")
    for key in ("operation_id", "equipment_resource_id"):
        if value[key] != str(getattr(parsed, key)):
            raise ValueError("equipment_uuid_not_canonical")
    if value["previous_operation_id"] != (None if parsed.previous_operation_id is None else str(parsed.previous_operation_id)):
        raise ValueError("equipment_previous_uuid_not_canonical")
    resource, profile = value["resource_binding"], value["profile_binding"]
    for binding, model in ((resource, parsed.resource_binding), (profile, parsed.profile_binding)):
        if binding != model.model_dump(mode="json"):
            raise ValueError("equipment_binding_not_canonical")
    chain = resource["ancestor_binding"]
    if len({item["resource_id"] for item in chain}) != len(chain):
        raise ValueError("equipment_ancestor_cycle")
    if any(item["parent_id"] != following["resource_id"] for item, following in zip(chain, chain[1:])) or chain[-1]["parent_id"] is not None:
        raise ValueError("equipment_ancestor_order")
    if chain[0] != {"resource_id": resource["resource_id"], "parent_id": resource["parent_id"], "topology_revision": resource["topology_revision"]}:
        raise ValueError("equipment_resource_chain_mismatch")
    if (value["operation_id"] != request["operation_id"] or value["warehouse_id"] != request["warehouse_id"]
        or value["equipment_resource_id"] != request["equipment_resource_id"]
        or resource["resource_id"] != request["equipment_resource_id"] or resource["warehouse_id"] != request["warehouse_id"]
        or resource["topology_revision"] != request["expected_topology_revision"]
        or profile["profile_id"] != request["equipment_profile_id"] or profile["revision"] != request["expected_profile_revision"]
        or profile["digest"] != request["expected_profile_digest"] or profile["bound_mapping_revision"] != resource["topology_revision"]
        or profile["ancestor_binding"] != chain or value["state_revision"] != request["expected_state_revision"] + 1
        or (value["state_revision"] == 1) != (value["previous_operation_id"] is None)):
        raise ValueError("equipment_receipt_correspondence")
    if not parsed.request.valid_from <= parsed.approved_at < parsed.request.valid_until:
        raise ValueError("equipment_observation_outside_validity")
    snapshot = json.loads(profile["canonical_payload"])
    if type(snapshot) is not dict or set(snapshot) != {"profile", "slots"} or canonical(snapshot) != profile["canonical_payload"]:
        raise ValueError("equipment_profile_payload_shape")
    signed = snapshot["profile"]
    if (type(signed) is not dict or signed.get("id") != profile["profile_id"] or signed.get("revision") != profile["revision"]
        or signed.get("organization_id") != value["organization_id"] or signed.get("warehouse_id") != value["warehouse_id"]
        or signed.get("resource_id") != value["equipment_resource_id"] or signed.get("packaging_id") is not None
        or signed.get("location_id") is not None or signed.get("bound_mapping_revision") != profile["bound_mapping_revision"]
        or signed.get("ancestor_binding") != chain or digest(snapshot) != profile["digest"]):
        raise ValueError("equipment_profile_payload_mismatch")
    if digest({"organization_id": value["organization_id"], "actor_user_id": value["actor_user_id"], "actor_sub": value["actor_sub"], "request": request}) != value["request_hash"]:
        raise ValueError("equipment_request_hash_mismatch")
    body = {key: item for key, item in value.items() if key not in {"canonical_payload", "digest"}}
    if value["canonical_payload"] != canonical(body) or value["digest"] != digest(body):
        raise ValueError("equipment_receipt_digest_mismatch")
    return value


class CarrierPlanningViolation(CapacityOutput):
    code: CarrierPlanningCode
    refs: list[str]

class CarrierPlanning(CapacityOutput):
    status: Literal["cannot_plan"]
    placement_authorized: Literal[False]
    violations: list[CarrierPlanningViolation]

class MeasurementConfirmation(CapacityOutput):
    requested: StrictBool
    accepted: StrictBool
    content_revision: I32 | None

class ObservationFrame(CapacityOutput):
    carrier: dict[str, Any] | None
    members: list[dict[str, Any]]
    positions: list[dict[str, Any]]
    location_units: list[dict[str, Any]]

class InvalidatedCarrier(CapacityOutput):
    before: dict[str, Any]
    reason: Literal["carrier_membership_unknown"]
    unit_ids: list[UUID]

class ResourceMappingResult(CapacityOutput):
    location_id: I32
    resource_mapping_revision: I32

class ResourceMetadataResult(CapacityOutput):
    resource_id: UUID
    topology_revision: I32
    location_mappings: list[ResourceMappingResult]

class ProfileMetadataResult(CapacityOutput):
    profile_id: UUID
    revision: I32
    digest: HexDigest
    snapshot: dict[str, Any]

class CarrierMetadataResult(CapacityOutput):
    carrier_id: UUID
    content_revision: I32
    content_digest: HexDigest
    confirmed_content_revision: I32 | None
    observed_empty: StrictBool
    reported: dict[str, Any]
    before: ObservationFrame
    after: ObservationFrame
    source_proofs: dict[str, list[dict[str, Any]]]
    unapplied_member_ids: list[UUID]
    invalidated_carriers: list[InvalidatedCarrier]
    measurement_confirmation: MeasurementConfirmation
    planning: CarrierPlanning


class AssemblyRunSpecRef(StrictModel):
    id: I32
    revision: I32
    digest: HexDigest


class AssemblyRunDecisionRef(StrictModel):
    id: I32
    revision: I32


class AssemblyRunBomRef(StrictModel):
    id: I32
    version: CapacityCode


class AssemblyRunSourceRefs(StrictModel):
    specification: AssemblyRunSpecRef
    nomination: AssemblyRunDecisionRef
    approval: AssemblyRunDecisionRef
    bom: AssemblyRunBomRef


class AssemblyRunSopInput(StrictModel):
    version: CapacityCode
    full_content: AssemblyDocumentText
    approval_reference: CapacityText
    evidence: CapacityText


class AssemblyRunProcessStep(StrictModel):
    step_id: CapacityCode
    content: AssemblyWorkText
    required_checks: list[CapacityText] = Field(min_length=1, max_length=50)


class AssemblyRunProcessInput(StrictModel):
    version: CapacityCode
    steps: list[AssemblyRunProcessStep] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_steps(self):
        unique_values([step.step_id for step in self.steps], "process step IDs")
        return self


class AssemblyRunSetupInput(StrictModel):
    version: CapacityCode
    content: AssemblyWorkText
    evidence: CapacityText


class AssemblyRunObservationCriterion(StrictModel):
    kind: Literal["observation"]
    criterion_id: CapacityCode
    description: CapacityText
    required_evidence: CapacityText
    expected_result: CapacityText


class AssemblyRunMeasurementCriterion(StrictModel):
    kind: Literal["measurement"]
    criterion_id: CapacityCode
    description: CapacityText
    required_evidence: CapacityText
    unit: CapacityCode
    precision: int = Field(strict=True, ge=0, le=6)
    lower_bound: CapacityText | None
    lower_inclusive: StrictBool
    upper_bound: CapacityText | None
    upper_inclusive: StrictBool

    @model_validator(mode="after")
    def validate_bounds(self):
        from decimal import Decimal, InvalidOperation

        if self.lower_bound is None and self.upper_bound is None:
            raise ValueError("Measurement criteria require at least one bound")
        values = []
        for bound in (self.lower_bound, self.upper_bound):
            if bound is None:
                values.append(None)
                continue
            if re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", bound) is None:
                raise ValueError("Measurement bounds must use plain decimal strings")
            try:
                value = Decimal(bound)
            except InvalidOperation as exc:
                raise ValueError("Measurement bounds must be exact finite decimal strings") from exc
            try:
                quantized = value.quantize(Decimal(1).scaleb(-self.precision))
            except InvalidOperation as exc:
                raise ValueError("Measurement bounds exceed declared precision") from exc
            if (not value.is_finite() or value != quantized
                    or len(quantized.as_tuple().digits) > 20):
                raise ValueError("Measurement bounds exceed declared precision")
            values.append(value)
        if values[0] is not None and values[1] is not None and values[0] > values[1]:
            raise ValueError("Measurement lower bound exceeds upper bound")
        return self


AssemblyRunCriterion = Annotated[
    AssemblyRunObservationCriterion | AssemblyRunMeasurementCriterion,
    Field(discriminator="kind"),
]


class AssemblyRunQcInput(StrictModel):
    policy_version: CapacityCode
    policy_document_or_authoritative_reference: CapacityText
    criteria: list[AssemblyRunCriterion] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def unique_criteria(self):
        unique_values([criterion.criterion_id for criterion in self.criteria], "QC criterion IDs")
        return self


class AssemblyRunResponsibilityInput(StrictModel):
    qc_responsible_sub: SourceRevision
    qc_responsible_user_id: I32
    appointment_reason: CapacityText
    appointment_evidence: CapacityText


class AssemblyRunIndependenceInput(StrictModel):
    policy_version: CapacityCode
    reference: CapacityText
    evidence: CapacityText
    rule: Literal["independent_required", "same_executor_allowed"]


class AssemblyRunPolicyReference(StrictModel):
    status: Literal["reference"]
    source: CapacityText
    revision: SourceRevision
    evidence: CapacityText


class AssemblyRunPolicyUnknown(StrictModel):
    status: Literal["unknown"]
    reason: CapacityText


AssemblyRunPolicy = Annotated[
    AssemblyRunPolicyReference | AssemblyRunPolicyUnknown,
    Field(discriminator="status"),
]


class AssemblyRunSerialPolicyRules(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)
    requirement: Literal["required", "not_applicable"]
    normalization: Literal["exact"] | None
    uniqueness_scope: Literal["organization", "sku"] | None

    @model_validator(mode="after")
    def consistent_requirement(self):
        if self.requirement == "required":
            if self.normalization != "exact" or self.uniqueness_scope is None:
                raise ValueError("Required serials need exact normalization and an explicit uniqueness scope")
        elif self.normalization is not None or self.uniqueness_scope is not None:
            raise ValueError("Non-applicable serials require null normalization and uniqueness scope")
        return self


class AssemblyRunDatePolicyRules(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)
    date_requirement: Literal["required", "not_applicable"]
    date_source: Literal["operator_report", "output_batch"] | None

    @model_validator(mode="after")
    def consistent_requirement(self):
        if (self.date_requirement == "required") != (self.date_source is not None):
            raise ValueError("Date requirements must explicitly select a source or declare null")
        return self


class AssemblyRunResolvedPolicyMetadata(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)
    status: Literal["resolved"]
    source: CapacityText
    revision: SourceRevision
    evidence: CapacityText


class AssemblyRunManufacturingPolicyResolved(AssemblyRunResolvedPolicyMetadata):
    policy_type: Literal["manufacturing"]
    rules: AssemblyRunDatePolicyRules


class AssemblyRunSerialPolicyResolved(AssemblyRunResolvedPolicyMetadata):
    policy_type: Literal["serial"]
    rules: AssemblyRunSerialPolicyRules


class AssemblyRunExpiryPolicyResolved(AssemblyRunResolvedPolicyMetadata):
    policy_type: Literal["expiry"]
    rules: AssemblyRunDatePolicyRules


AssemblyRunManufacturingPolicy = Annotated[
    AssemblyRunPolicyReference | AssemblyRunPolicyUnknown | AssemblyRunManufacturingPolicyResolved,
    Field(discriminator="status"),
]
AssemblyRunSerialPolicy = Annotated[
    AssemblyRunPolicyReference | AssemblyRunPolicyUnknown | AssemblyRunSerialPolicyResolved,
    Field(discriminator="status"),
]
AssemblyRunExpiryPolicy = Annotated[
    AssemblyRunPolicyReference | AssemblyRunPolicyUnknown | AssemblyRunExpiryPolicyResolved,
    Field(discriminator="status"),
]


class AssemblyRunPrepare(StrictModel):
    request_id: UUID
    expected_revision: I32Zero
    expected_source_refs: AssemblyRunSourceRefs
    expected_composition_schema_version: Literal[1]
    expected_composition_digest: HexDigest
    designated_first_output_key: CapacityCode
    sop: AssemblyRunSopInput
    process: AssemblyRunProcessInput
    setup: AssemblyRunSetupInput
    qc: AssemblyRunQcInput
    responsibility: AssemblyRunResponsibilityInput
    independence: AssemblyRunIndependenceInput
    manufacturing_policy: AssemblyRunManufacturingPolicy
    serial_policy: AssemblyRunSerialPolicy
    expiry_policy: AssemblyRunExpiryPolicy
    reason: CapacityText
    evidence: CapacityText


class AssemblyRunConfirm(StrictModel):
    request_id: UUID
    expected_revision: I32
    prepared_revision_id: UUID
    expected_prepared_digest: HexDigest
    expected_source_refs: AssemblyRunSourceRefs
    expected_composition_schema_version: Literal[1]
    expected_composition_digest: HexDigest
    evidence: CapacityText


H11Issue = Annotated[str, StringConstraints(strip_whitespace=False), Field(strict=True, max_length=128)]


class AssemblyRunH11SourceRefs(StrictModel):
    specification: AssemblyRunSpecRef | None
    nomination: AssemblyRunDecisionRef | None
    approval: AssemblyRunDecisionRef | None
    bom: AssemblyRunBomRef | None


class AssemblyRunH11Composition(StrictModel):
    schema_version: Literal[1]
    digest: HexDigest


class AssemblyRunH11Source(StrictModel):
    status: Literal[
        "validated", "missing_specification", "unapproved", "stale_approval",
        "source_changed", "invalid_source",
    ]
    refs: AssemblyRunH11SourceRefs
    composition: AssemblyRunH11Composition | None
    issues: list[H11Issue] = Field(max_length=20)


class AssemblyRunH11Actor(StrictModel):
    user_id: I32
    sub: SourceRevision


class AssemblyRunH11Assignment(StrictModel):
    revision: I32Zero
    state: Literal["unassigned", "assigned", "released"]
    warehouse_id: I32 | None
    own_active: StrictBool


class AssemblyRunH11Head(StrictModel):
    run_id: UUID
    id: UUID
    revision: I32
    kind: Literal["prepared", "confirmed"]
    content_digest: HexDigest
    prepared_revision_id: UUID
    prepared_digest: HexDigest


class AssemblyRunH11Sop(StrictModel):
    version: CapacityCode
    full_content: AssemblyDocumentText
    approval_reference: CapacityText
    server_digest: HexDigest


class AssemblyRunH11Setup(StrictModel):
    version: CapacityCode
    content: AssemblyWorkText


class AssemblyRunH11Independence(StrictModel):
    policy_version: CapacityCode
    reference: CapacityText
    rule: Literal["independent_required", "same_executor_allowed"]


class AssemblyRunH11PolicyReference(StrictModel):
    status: Literal["reference"]
    source: CapacityText
    revision: SourceRevision


AssemblyRunH11Policy = Annotated[
    AssemblyRunH11PolicyReference | AssemblyRunPolicyUnknown,
    Field(discriminator="status"),
]


class AssemblyRunH11ResolvedPolicyMetadata(StrictModel):
    status: Literal["resolved"]
    source: CapacityText
    revision: SourceRevision


class AssemblyRunH11ManufacturingPolicyResolved(AssemblyRunH11ResolvedPolicyMetadata):
    policy_type: Literal["manufacturing"]
    rules: AssemblyRunDatePolicyRules


class AssemblyRunH11SerialPolicyResolved(AssemblyRunH11ResolvedPolicyMetadata):
    policy_type: Literal["serial"]
    rules: AssemblyRunSerialPolicyRules


class AssemblyRunH11ExpiryPolicyResolved(AssemblyRunH11ResolvedPolicyMetadata):
    policy_type: Literal["expiry"]
    rules: AssemblyRunDatePolicyRules


AssemblyRunH11ManufacturingPolicy = Annotated[
    AssemblyRunH11PolicyReference | AssemblyRunPolicyUnknown | AssemblyRunH11ManufacturingPolicyResolved,
    Field(discriminator="status"),
]
AssemblyRunH11SerialPolicy = Annotated[
    AssemblyRunH11PolicyReference | AssemblyRunPolicyUnknown | AssemblyRunH11SerialPolicyResolved,
    Field(discriminator="status"),
]
AssemblyRunH11ExpiryPolicy = Annotated[
    AssemblyRunH11PolicyReference | AssemblyRunPolicyUnknown | AssemblyRunH11ExpiryPolicyResolved,
    Field(discriminator="status"),
]


class AssemblyRunH11Document(StrictModel):
    sop: AssemblyRunH11Sop
    process: AssemblyRunProcessInput
    setup: AssemblyRunH11Setup
    qc: AssemblyRunQcInput
    independence: AssemblyRunH11Independence
    manufacturing_policy: AssemblyRunH11ManufacturingPolicy
    serial_policy: AssemblyRunH11SerialPolicy
    expiry_policy: AssemblyRunH11ExpiryPolicy


class AssemblyRunH11Projection(StrictModel):
    schema_version: Literal[1]
    organization_id: I32
    warehouse_id: I32
    order_id: I32
    actor: AssemblyRunH11Actor
    read_scope: Literal["management", "nominee", "builder"]
    read_at: AwareDatetime
    assignment: AssemblyRunH11Assignment
    status: Literal["missing", "unconfirmed", "stale", "confirmed", "hidden"]
    source: AssemblyRunH11Source | None
    head: AssemblyRunH11Head | None
    document: AssemblyRunH11Document | None


class AssemblyOutputExecutionState(AssemblyProofModel):
    execution_id: AssemblyUUID
    configured: StrictBool
    confirmed_revision_id: AssemblyUUID | None
    policy_inputs_digest: HexDigest | None
    designated_first_output_key: CapacityCode | None
    first_qc: Literal["unborn", "pending", "PASS", "FAIL"] | None
    can_consume_other: StrictBool
    can_output_other: StrictBool


class AssemblyOutputCurrentUnit(AssemblyProofModel):
    id: AssemblyUUID
    location_id: I32
    quality: Literal["quarantine", "released", "blocked"]
    qty: Literal["1"]
    reserved: Annotated[str, Field(strict=True, pattern=r"^(?:0\.[0-9]{6}|1\.000000)$")]


class AssemblyOutputReadIdentity(AssemblyProofModel):
    identity_id: AssemblyUUID
    execution_id: AssemblyUUID
    output_key: CapacityCode
    confirmed_revision_id: AssemblyUUID
    effective_lot: AssemblyEffectiveLot
    current_unit: AssemblyOutputCurrentUnit | None
    qc_birth_operation_id: AssemblyUUID | None
    verdict: Literal["pending", "PASS", "FAIL"] | None
    can_qc: StrictBool
    policy_inputs_digest: HexDigest
    serial_number: RawScan | None
    qc: AssemblyRunQcInput | None


class AssemblyOutputReadLocation(AssemblyProofModel):
    location_id: I32
    code: Code
    kind: Literal["quarantine", "returns"]


class AssemblyOutputCurrentTransit(AssemblyProofModel):
    id: AssemblyUUID
    dispatch_operation_id: AssemblyUUID
    qty: Literal["1"]


class AssemblyOutputReadIdentityV2(AssemblyOutputReadIdentity):
    current_transit: AssemblyOutputCurrentTransit | None

    @model_validator(mode="after")
    def transit_has_no_address_or_qc(self):
        if (self.current_transit is not None and self.current_unit is not None) or (
            self.current_unit is None and (
                self.qc_birth_operation_id is not None or self.verdict is not None or self.can_qc
            )
        ):
            raise ValueError("Transit assembly output cannot expose an address or QC action")
        return self


class AssemblyOutputReturnCandidate(AssemblyProofModel):
    identity_id: AssemblyUUID
    ship_operation_id: AssemblyUUID
    pick_id: I32
    shipped_unit_id: AssemblyUUID
    warehouse_id: I32
    can_return: StrictBool


class AssemblyOutputReadProjection(AssemblyProofModel):
    schema_version: AssemblyProtocol
    organization_id: I32
    order_id: I32
    warehouse_id: I32
    status: Literal["visible", "blind_count_hidden", "unavailable"]
    execution_states: list[AssemblyOutputExecutionState] | None
    identities: list[AssemblyOutputReadIdentity] | None
    batches: list[AssemblyEffectiveLot] | None
    locations: list[AssemblyOutputReadLocation] | None
    return_candidates: list[AssemblyOutputReturnCandidate] | None
    actor: AssemblyRunH11Actor

    @model_validator(mode="after")
    def hide_all_physical_lists(self):
        lists = (self.execution_states, self.identities, self.batches, self.locations, self.return_candidates)
        if self.status == "visible":
            if any(value is None for value in lists):
                raise ValueError("Visible assembly outputs require all physical lists")
        elif any(value is not None for value in lists):
            raise ValueError("Hidden assembly outputs cannot expose physical lists")
        return self


class AssemblyOutputReadProjectionV2(AssemblyOutputReadProjection):
    schema_version: Annotated[int, Field(strict=True, ge=2, le=2)]
    identities: list[AssemblyOutputReadIdentityV2] | None

    @model_validator(mode="after")
    def transit_has_no_shipment_return(self):
        transit_ids = {row.identity_id for row in self.identities or [] if row.current_transit is not None}
        if any(row.identity_id in transit_ids for row in self.return_candidates or []):
            raise ValueError("Transit assembly output cannot be a shipment return candidate")
        return self

# ENSURE metadata is deliberately separate from quantity-command DTOs.
class ItemIdentityEnsureModel(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


def _ensure_nonblank_exact_text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Требуется непустая строка")
    return value


def _ensure_factor_text(value):
    if not isinstance(value, str):
        raise ValueError("Коэффициент должен быть десятичной строкой")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Недопустимый коэффициент упаковки") from exc
    if (
        not amount.is_finite()
        or amount <= 0
        or amount.as_tuple().exponent < -6
        or format(amount, "f") != value
    ):
        raise ValueError("Коэффициент должен быть конечным, положительным и точным")
    return value


def _ensure_schema_version_v1(value):
    if type(value) is not int or value != 1:
        raise ValueError("Поддерживается только schema_version=1")
    return value


ItemIdentityEnsureText = Annotated[
    str,
    StringConstraints(strip_whitespace=False),
    Field(strict=True, min_length=1, max_length=64),
    BeforeValidator(_ensure_nonblank_exact_text),
]
ItemIdentityEnsureUnit = Annotated[
    str,
    StringConstraints(strip_whitespace=False),
    Field(strict=True, min_length=1, max_length=16),
    BeforeValidator(_ensure_nonblank_exact_text),
]
ItemIdentityEnsureFactor = Annotated[
    str,
    StringConstraints(strip_whitespace=False),
    Field(strict=True, min_length=1, max_length=64),
    BeforeValidator(_ensure_factor_text),
]
ItemIdentityEnsureSchemaVersion = Annotated[
    int,
    BeforeValidator(_ensure_schema_version_v1),
    Field(strict=True, ge=1, le=1),
]
ItemIdentityEnsureSub = Annotated[
    str,
    StringConstraints(strip_whitespace=False),
    Field(strict=True, min_length=1, max_length=64),
    BeforeValidator(_ensure_nonblank_exact_text),
]


class ItemIdentityEnsureRequest(ItemIdentityEnsureModel):
    operation_id: UUID
    warehouse_id: I32
    sku_id: I32
    packaging_id: I32 | None


class ItemIdentityEnsureOwner(ItemIdentityEnsureModel):
    user_id: I32
    sub: ItemIdentityEnsureSub


class ItemIdentityEnsureSnapshot(ItemIdentityEnsureModel):
    sku_unit: ItemIdentityEnsureUnit
    base_unit: ItemIdentityEnsureUnit
    precision: Annotated[int, Field(strict=True, ge=0, le=6)]
    factor: ItemIdentityEnsureFactor
    sealed_allowed: StrictBool


class ItemIdentityEnsureOutcome(ItemIdentityEnsureModel):
    sku_id: I32
    sku_code: ItemIdentityEnsureText
    packaging_id: I32 | None
    internal_code: ItemIdentityEnsureText
    snapshot: ItemIdentityEnsureSnapshot


class ItemIdentityEnsureRejectedOutcome(ItemIdentityEnsureModel):
    status: Literal["metadata_rejected"]
    code: Literal["context_stale", "scan_mismatch", "source_unavailable", "origin_unproven"]


class ItemIdentityEnsureEnvelope(ItemIdentityEnsureModel):
    protocol: Literal["wms.item-label-metadata.v1"]
    schema_version: ItemIdentityEnsureSchemaVersion
    operation_id: UUID
    kind: Literal["item_identity_ensure"]
    organization_id: I32
    warehouse_id: I32
    owner: ItemIdentityEnsureOwner
    request: ItemIdentityEnsureRequest
    request_hash: HexDigest
    recorded_at: AwareDatetime
    outcome: ItemIdentityEnsureOutcome | ItemIdentityEnsureRejectedOutcome

    @model_validator(mode="after")
    def exact_references(self):
        if (
            self.request.operation_id != self.operation_id
            or self.request.warehouse_id != self.warehouse_id
        ):
            raise ValueError("Сохранённые ссылки операции не совпадают")
        if isinstance(self.outcome, ItemIdentityEnsureRejectedOutcome):
            return self
        if (
            self.outcome.sku_id != self.request.sku_id
            or self.outcome.packaging_id != self.request.packaging_id
        ):
            raise ValueError("Сохранённые ссылки операции не совпадают")
        expected_code = (
            f"SKU-{self.request.sku_id}"
            if self.request.packaging_id is None
            else f"ERP-P-{self.request.packaging_id}"
        )
        if self.outcome.internal_code != expected_code:
            raise ValueError("Внутренний код не совпадает с выбранной идентичностью")
        if self.outcome.snapshot.sku_unit != self.outcome.snapshot.base_unit:
            raise ValueError("Единицы номенклатуры и политики не совпадают")
        if self.request.packaging_id is None and (
            self.outcome.snapshot.factor != "1"
            or self.outcome.snapshot.sealed_allowed
        ):
            raise ValueError("Базовая идентичность должна иметь коэффициент 1")
        return self


ItemLabelMode = Literal["internal", "existing_factory"]


class ItemLabelPrepareRequest(ItemIdentityEnsureModel):
    operation_id: UUID
    warehouse_id: I32
    ensure_operation_id: UUID
    unit_id: UUID
    label_mode: ItemLabelMode


class ItemLabelVerifyRequest(ItemIdentityEnsureModel):
    operation_id: UUID
    warehouse_id: I32
    prepare_operation_id: UUID
    item_scan: RawScan
    batch_scan: RawScan
    location_scan: RawScan


class ItemLabelPrintPayload(ItemIdentityEnsureModel):
    schema_version: ItemIdentityEnsureSchemaVersion
    label_mode: ItemLabelMode
    sku_code: ItemIdentityEnsureText
    sku_title: Annotated[str, StringConstraints(strip_whitespace=False), Field(strict=True, max_length=255)]
    item_code: ItemIdentityEnsureText
    batch_code: ItemIdentityEnsureText
    lot_no: Annotated[str, StringConstraints(strip_whitespace=False), Field(strict=True, max_length=64)]
    location_code: ItemIdentityEnsureText
    location_title: Annotated[str, StringConstraints(strip_whitespace=False), Field(strict=True, max_length=64)]


class ItemLabelPrepareOutcome(ItemIdentityEnsureModel):
    status: Literal["label_prepared"]
    ensure_operation_id: UUID
    ensure_request_hash: HexDigest
    unit_id: UUID
    sku_id: I32
    sku_code: ItemIdentityEnsureText
    packaging_id: I32 | None
    snapshot: ItemIdentityEnsureSnapshot
    label_mode: ItemLabelMode
    batch_id: I32
    location_id: I32
    unit_digest: HexDigest
    context_digest: HexDigest
    proof_digest: HexDigest
    item_code: ItemIdentityEnsureText
    batch_code: ItemIdentityEnsureText
    location_code: ItemIdentityEnsureText
    print_payload: ItemLabelPrintPayload


class ItemLabelPrepareEnvelope(ItemIdentityEnsureModel):
    protocol: Literal["wms.item-label-metadata.v1"]
    schema_version: ItemIdentityEnsureSchemaVersion
    operation_id: UUID
    kind: Literal["item_label_prepare"]
    organization_id: I32
    warehouse_id: I32
    owner: ItemIdentityEnsureOwner
    request: ItemLabelPrepareRequest
    request_hash: HexDigest
    recorded_at: AwareDatetime
    outcome: ItemLabelPrepareOutcome | ItemIdentityEnsureRejectedOutcome

    @model_validator(mode="after")
    def exact_references(self):
        if (
            self.request.operation_id != self.operation_id
            or self.request.warehouse_id != self.warehouse_id
        ):
            raise ValueError("Сохранённые ссылки операции не совпадают")
        if isinstance(self.outcome, ItemIdentityEnsureRejectedOutcome):
            return self
        if (
            self.request.ensure_operation_id != self.outcome.ensure_operation_id
            or self.request.unit_id != self.outcome.unit_id
            or self.request.label_mode != self.outcome.label_mode
            or self.outcome.sku_id <= 0
            or self.outcome.snapshot.sku_unit != self.outcome.snapshot.base_unit
            or (
                self.outcome.packaging_id is None
                and (
                    self.outcome.snapshot.factor != "1"
                    or self.outcome.snapshot.sealed_allowed
                )
            )
            or self.outcome.print_payload.schema_version != 1
            or self.outcome.print_payload.label_mode != self.outcome.label_mode
            or self.outcome.print_payload.sku_code != self.outcome.sku_code
            or self.outcome.print_payload.item_code != self.outcome.item_code
            or self.outcome.print_payload.batch_code != self.outcome.batch_code
            or self.outcome.print_payload.location_code != self.outcome.location_code
            or self.outcome.batch_code
            != f"LOT-{self.outcome.batch_id}-{str(self.outcome.unit_id).upper()}"
            or self.outcome.location_code != f"LOC-{self.outcome.location_id}"
            or (
                self.outcome.label_mode == "internal"
                and self.outcome.item_code != (
                    f"SKU-{self.outcome.sku_id}"
                    if self.outcome.packaging_id is None
                    else f"ERP-P-{self.outcome.packaging_id}"
                )
            )
        ):
            raise ValueError("Сохранённые ссылки и печатный профиль не совпадают")
        return self


class ItemLabelVerifyOutcome(ItemIdentityEnsureModel):
    status: Literal["scan_verified"]
    prepare_operation_id: UUID
    prepare_request_hash: HexDigest
    ensure_operation_id: UUID
    unit_id: UUID
    sku_id: I32
    packaging_id: I32 | None
    label_mode: ItemLabelMode
    unit_digest: HexDigest
    context_digest: HexDigest
    proof_digest: HexDigest
    item_scan: RawScan
    batch_scan: RawScan
    location_scan: RawScan


class ItemLabelVerifyEnvelope(ItemIdentityEnsureModel):
    protocol: Literal["wms.item-label-metadata.v1"]
    schema_version: ItemIdentityEnsureSchemaVersion
    operation_id: UUID
    kind: Literal["item_label_verify"]
    organization_id: I32
    warehouse_id: I32
    owner: ItemIdentityEnsureOwner
    request: ItemLabelVerifyRequest
    request_hash: HexDigest
    recorded_at: AwareDatetime
    outcome: ItemLabelVerifyOutcome | ItemIdentityEnsureRejectedOutcome

    @model_validator(mode="after")
    def exact_references(self):
        if (
            self.request.operation_id != self.operation_id
            or self.request.warehouse_id != self.warehouse_id
        ):
            raise ValueError("Сохранённые ссылки операции не совпадают")
        if isinstance(self.outcome, ItemIdentityEnsureRejectedOutcome):
            return self
        if (
            self.request.prepare_operation_id != self.outcome.prepare_operation_id
            or self.request.item_scan != self.outcome.item_scan
            or self.request.batch_scan != self.outcome.batch_scan
            or self.request.location_scan != self.outcome.location_scan
        ):
            raise ValueError("Сохранённая ссылка подготовки не совпадает")
        return self
