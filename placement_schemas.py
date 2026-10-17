"""Separate placement wire grammar; existing physical command unions stay closed."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, StrictBool, model_validator

from modules.wms.physical_schemas import (
    AccessMethod, CapacityCode, CapacityText, I32, I32Zero, Measure,
    PositiveMeasure, SourceRevision, StrictModel, unique_values, HexDigest, Orientation,
)


class WorkRoute(StrictModel):
    source_location_id: I32
    destination_location_id: I32
    resource_ids: list[UUID] = Field(min_length=1, max_length=1000)
    access_slot_groups: list[list[CapacityCode]] = Field(min_length=1, max_length=1000)
    # Expected maximum destination slot-group top (y+h), in its measured frame.
    # None omits the assertion; the server still checks every group's height.
    placement_height_mm: Measure | None = None
    route_evidence: CapacityText

    @model_validator(mode="after")
    def check_route(self):
        if self.source_location_id == self.destination_location_id:
            raise ValueError("placement_route_same_location")
        unique_values(self.resource_ids, "ресурсы")
        groups = []
        for group in self.access_slot_groups:
            if not group or len(group) > 1000:
                raise ValueError("placement_slot_group_empty_or_over_limit")
            unique_values(group, "слоты")
            groups.append(tuple(sorted(group)))
        unique_values(groups, "группы слотов")
        return self


class PlacementWorkPolicyInput(StrictModel):
    scope_code: CapacityCode
    access_method: AccessMethod
    operator_subs: list[SourceRevision] = Field(min_length=1, max_length=1000)
    routes: list[WorkRoute] = Field(min_length=1, max_length=1000)
    equipment_resource_id: UUID | None
    equipment_available: StrictBool | None
    manual_gross_limit_kg: PositiveMeasure | None
    manual_height_limit_mm: PositiveMeasure | None
    equipment_gross_limit_kg: PositiveMeasure | None
    valid_from: AwareDatetime
    valid_until: AwareDatetime
    source_ref: CapacityText
    source_revision: SourceRevision
    evidence: CapacityText

    @model_validator(mode="after")
    def check_work(self):
        if self.valid_until <= self.valid_from:
            raise ValueError("placement_work_validity")
        unique_values(self.operator_subs, "операторы")
        unique_values([(r.source_location_id, r.destination_location_id) for r in self.routes], "маршруты")
        if self.access_method == "manual" and any(value is not None for value in (
            self.equipment_resource_id, self.equipment_available, self.equipment_gross_limit_kg,
        )):
            raise ValueError("placement_manual_equipment_contradiction")
        if self.access_method == "equipment" and any(value is not None for value in (
            self.manual_gross_limit_kg, self.manual_height_limit_mm,
        )):
            raise ValueError("placement_equipment_manual_contradiction")
        return self


class PlacementWorkApprove(StrictModel):
    protocol: Literal[2]
    operation_id: UUID
    warehouse_id: I32
    kind: Literal["capacity_work_approve"]
    policy_id: UUID
    expected_revision: I32Zero
    work: PlacementWorkPolicyInput


class PlacementChoice(StrictModel):
    task_id: UUID
    carrier_id: UUID
    destination_location_id: I32
    orientation: Orientation
    slot_keys: list[CapacityCode] = Field(min_length=1, max_length=1000)
    source_content_revision: I32
    source_content_digest: HexDigest
    destination_profile_id: UUID
    work_policy_id: UUID
    proposal_digest: HexDigest

    @model_validator(mode="after")
    def check_slots(self):
        unique_values(self.slot_keys, "слоты")
        return self


class PlacementAssign(StrictModel):
    protocol: Literal[2]
    operation_id: UUID
    warehouse_id: I32
    kind: Literal["capacity_placement_assign"]
    choices: list[PlacementChoice] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def check_choices(self):
        unique_values([row.task_id for row in self.choices], "задания")
        unique_values([row.carrier_id for row in self.choices], "носители")
        return self


class PlacementTaskAction(StrictModel):
    protocol: Literal[2]
    operation_id: UUID
    warehouse_id: I32
    kind: Literal["capacity_placement_begin", "capacity_placement_complete", "capacity_placement_cancel"]
    task_id: UUID
    assignment_digest: HexDigest
    evidence: CapacityText


# Separate name, not an extension of CapacityCommand or WarehouseCommand.
PlacementCommand = Annotated[PlacementWorkApprove | PlacementAssign | PlacementTaskAction,
                             Field(discriminator="kind")]


class PlacementProposalQuery(StrictModel):
    protocol: Literal[2]
    warehouse_id: I32
    carrier_ids: list[UUID] = Field(min_length=1, max_length=1000)
    candidate_location_ids: list[I32] = Field(min_length=1, max_length=1000)
    work_policy_id: UUID

    @model_validator(mode="after")
    def check_query(self):
        unique_values(self.carrier_ids, "носители")
        unique_values(self.candidate_location_ids, "ячейки")
        return self


def normalize_placement_request(data: PlacementWorkApprove | PlacementAssign | PlacementTaskAction) -> dict:
    """Own canonical envelope; never reinterpret existing command hashes."""
    def normalize(value):
        if isinstance(value, Decimal):
            return format(value, ".6f")
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        return value
    return normalize(data.model_dump(mode="python"))
