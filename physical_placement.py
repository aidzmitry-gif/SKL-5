"""Pure geometry and mass preview; no ORM, writes, receipts, or execution authority.

A future trusted loader must re-read current managed authority, eligible(),
native profile payloads/slots, mapping and topology bindings, all units/holds/
positions, count locks, and active claims under the warehouse lock. That loader
is not implemented here. Facts are an internal snapshot contract, not a wire
DTO or proof of caller authority. The result always denies execution.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

ZERO = Decimal("0.000000")
ACTIVE = {"assigned", "in_transit", "blocked", "needs_reconcile"}
ORIENTATIONS = ("LWH", "LHW", "WLH", "WHL", "HLW", "HWL")


class PlacementInputError(ValueError):
    """Malformed or contradictory facts; never coerce them."""


@dataclass(frozen=True, slots=True)
class Box:
    x: Decimal
    y: Decimal
    z: Decimal
    w: Decimal
    h: Decimal
    d: Decimal


@dataclass(frozen=True, slots=True)
class Resource:
    id: str
    parent_id: str | None
    kind: str
    topology_revision: int


@dataclass(frozen=True, slots=True)
class Slot:
    warehouse_id: int
    location_id: int
    profile_id: str
    key: str
    x: Decimal
    y: Decimal
    z: Decimal
    width: Decimal
    height: Decimal
    depth: Decimal


@dataclass(frozen=True, slots=True)
class NativeProfile:
    """Every physical_capacity.profile_payload column, excluding stored hash fields."""

    id: str
    organization_id: int
    warehouse_id: int
    revision: int
    packaging_id: int | None
    location_id: int | None
    resource_id: str | None
    length_mm: Decimal | None
    width_mm: Decimal | None
    height_mm: Decimal | None
    opening_width_mm: Decimal | None
    opening_height_mm: Decimal | None
    clearance_mm: Decimal | None
    gross_limit_kg: Decimal | None
    tare_kg: Decimal | None
    top_load_limit_kg: Decimal | None
    max_layers: int | None
    orientations: tuple[str, ...] | None
    load_type: str | None
    support_rule: str | None
    access_method: str | None
    equipment_ref: str | None
    restrictions: tuple[str, ...] | None
    source_ref: str
    source_revision: str
    evidence: str
    approved_by: str
    approved_at: datetime
    bound_mapping_revision: int | None
    ancestor_binding: tuple[tuple[str, str | None, int], ...] | None


@dataclass(frozen=True, slots=True)
class Profile:
    native: NativeProfile
    slots: tuple[Slot, ...]
    frame_id: str | None
    bound_resources: tuple[tuple[str, str | None, int], ...]
    canonical_payload: str
    digest: str
    sealed: bool


@dataclass(frozen=True, slots=True)
class Location:
    id: int
    code: str
    active: bool
    resource_id: str | None
    mapping_revision: int
    current_profile_id: str | None
    frame_id: str | None
    resource_path: tuple[tuple[str, str | None, int], ...]
    active_count_id: str | None


@dataclass(frozen=True, slots=True)
class Unit:
    id: str
    sku: str
    lot: str
    qty: Decimal
    reserved: Decimal
    eligibility: Literal["eligible", "ineligible", "unknown"]
    fefo_rank: int | None


@dataclass(frozen=True, slots=True)
class DomainUnit(Unit):
    """Current server facts; lot remains outside the FEFO comparison domain."""

    characteristic: str
    purpose: str

    def __post_init__(self) -> None:
        _domain_unit_shape(self)


@dataclass(frozen=True, slots=True)
class Position:
    carrier_id: str
    location_id: int
    profile_id: str
    slot_keys: tuple[str, ...]
    frame_id: str | None
    support_carrier_id: str | None


@dataclass(frozen=True, slots=True)
class Carrier:
    id: str
    warehouse_id: int
    location_id: int
    packaging_id: int | None
    packaging_profile_id: str | None
    content_revision: int
    confirmed_content_revision: int | None
    content_digest: str | None
    member_digest: str | None
    gross_kg: Decimal | None
    tare_kg: Decimal | None
    state: Literal["present", "departed", "unknown"]
    members: tuple[Unit, ...]
    positions: tuple[Position, ...]


@dataclass(frozen=True, slots=True)
class Claim:
    task_id: str
    carrier_id: str
    location_id: int
    profile_id: str
    slot_keys: tuple[str, ...]
    orientation: str
    frame_id: str | None
    support_carrier_id: str | None


@dataclass(frozen=True, slots=True)
class Hold:
    hold_id: int
    unit_id: str
    sku: str
    lot: str
    qty: Decimal


@dataclass(frozen=True, slots=True)
class Assignment:
    task_id: str
    organization_id: int
    warehouse_id: int
    task_kind: str
    task_status: str
    task_qty: Decimal | None
    task_unit_id: str | None
    task_hold_id: str | None
    task_destination_id: int | None
    assignee_sub: str
    state: str
    carrier_id: str
    source_location_id: int
    destination_location_id: int
    source_profile_id: str
    destination_profile_id: str
    packaging_profile_id: str
    work_policy_id: str
    source_revision: int
    source_digest: str
    gross_kg: Decimal
    tare_kg: Decimal
    source_path: tuple[str, ...]
    phase_paths: tuple[tuple[str, ...], ...]
    member_vector: tuple[tuple[str, str, str, Decimal], ...]
    current_member_vector: tuple[tuple[str, str, str, Decimal], ...]
    owned_holds: tuple[Hold, ...]
    current_owned_holds: tuple[Hold, ...]
    claims: tuple[Claim, ...]
    canonical_payload: str
    digest: str
    expected_digest: str
    action: Literal["begin", "complete"]


@dataclass(frozen=True, slots=True)
class Opening:
    width_axis: Literal["x", "y", "z"]
    height_axis: Literal["x", "y", "z"]
    width_mm: Decimal
    height_mm: Decimal
    clearance_mm: Decimal | None


@dataclass(frozen=True, slots=True)
class Corridor:
    box: Box
    frame_id: str | None
    older_fefo_rank: int


@dataclass(frozen=True, slots=True)
class CorridorDomain:
    domain: tuple[str, str, str]
    protected_unit_ids: tuple[str, ...]
    mode: Literal["fefo", "must_clear", "unknown"]
    threshold: int | None

    def __post_init__(self) -> None:
        _corridor_domain_shape(self)


@dataclass(frozen=True, slots=True)
class DomainCorridor:
    """Ephemeral compiled access facts, never the stored Corridor grammar."""

    kind: Literal["domain_v1"]
    box: Box
    frame_id: str
    domains: tuple[CorridorDomain, ...]

    def __post_init__(self) -> None:
        _domain_corridor_shape(self)


@dataclass(frozen=True, slots=True)
class Route:
    source_id: int
    destination_id: int
    source_profile_id: str
    source_digest: str
    destination_profile_id: str
    destination_digest: str
    source_mapping_revision: int
    destination_mapping_revision: int
    resource_ids: tuple[str, ...]
    phase_resource_paths: tuple[tuple[str, ...], ...]
    slot_groups: tuple[tuple[str, ...], ...]
    openings: tuple[Opening, ...]
    corridors: tuple[Corridor | DomainCorridor, ...]
    access_digest: str
    evidence: str


@dataclass(frozen=True, slots=True)
class LoadRule:
    resource_id: str
    state: Literal["required", "confirmed_non_load_bearing", "unknown"]
    gross_limit_kg: Decimal | None
    source_profile_id: str | None
    source_profile_digest: str | None


@dataclass(frozen=True, slots=True)
class Policy:
    id: str
    organization_id: int
    warehouse_id: int
    scope_code: str
    revision: int
    access_method: Literal["manual", "equipment"]
    operator_subs: tuple[str, ...]
    routes: tuple[Route, ...]
    equipment_resource_id: str | None
    equipment_profile_digest: str | None
    equipment_state: Literal["available", "unavailable", "unknown"]
    manual_gross_limit_kg: Decimal | None
    manual_height_limit_mm: Decimal | None
    equipment_gross_limit_kg: Decimal | None
    equipment_height_limit_mm: Decimal | None
    load_rules: tuple[LoadRule, ...]
    valid_from: datetime
    valid_until: datetime
    source_ref: str
    source_revision: str
    evidence: str
    approved_by: str
    approved_at: datetime
    approval_operation_id: str
    server_binding_digest: str
    canonical_payload: str
    digest: str


@dataclass(frozen=True, slots=True)
class Support:
    location_id: int
    profile_id: str
    profile_digest: str
    frame_id: str
    plane_y: Decimal
    x_min: Decimal
    x_max: Decimal
    z_min: Decimal
    z_max: Decimal
    bearing_kind: Literal["resource", "carrier"]
    bearing_id: str
    top_load_limit_kg: Decimal | None
    layer: int
    assignment_task_id: str | None


@dataclass(frozen=True, slots=True)
class UnknownOccupancy:
    location_id: int
    resource_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class Facts:
    organization_id: int
    warehouse_id: int
    warehouse_mode: str
    actor_sub: str | None
    current_member_subs: tuple[str, ...]
    policy: Policy | None
    locations: tuple[Location, ...]
    resources: tuple[Resource, ...]
    profiles: tuple[Profile, ...]
    supports: tuple[Support, ...]
    carriers: tuple[Carrier, ...]
    active_assignments: tuple[Assignment, ...]
    active_claims: tuple[Claim, ...]
    unknown_occupancies: tuple[UnknownOccupancy, ...]
    canonical_payload: str
    digest: str


@dataclass(frozen=True, slots=True)
class DomainFacts(Facts):
    """Current commitments; neither their presence nor digest grants authority."""

    decision_date: str
    current_snapshot_digest: str
    measured_context_digest: str

    def __post_init__(self) -> None:
        _domain_facts_shape(self)


@dataclass(frozen=True, slots=True)
class Query:
    protocol: Literal[2]
    warehouse_id: int
    carrier_ids: tuple[str, ...]
    candidate_location_ids: tuple[int, ...]
    work_policy_id: str


@dataclass(frozen=True, slots=True)
class UnitQuantity:
    unit_id: str
    sku: str
    lot: str
    qty: Decimal


@dataclass(frozen=True, slots=True)
class Choice:
    carrier_id: str
    destination_location_id: int
    orientation: str
    slot_keys: tuple[str, ...]
    source_content_revision: int
    source_content_digest: str
    destination_profile_id: str
    work_policy_id: str
    proposal_digest: str
    units: tuple[UnitQuantity, ...]
    gross_kg: Decimal
    tare_kg: Decimal
    depends_on_task_ids: tuple[str, ...]
    explanation: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Violation:
    code: str
    refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Result:
    protocol: Literal[2]
    status: Literal["proposed", "partial", "cannot_plan"]
    placement_authorized: Literal[False]
    facts_digest: str
    assignments: tuple[Choice, ...]
    remaining_carrier_ids: tuple[str, ...]
    violations: tuple[Violation, ...]


def _bad(code: str) -> None:
    raise PlacementInputError(code)


def _micro(v: Decimal, positive: bool = False) -> int:
    if type(v) is not Decimal or not v.is_finite():
        _bad("decimal_unknown_or_nonfinite")
    sign, digits, exponent = v.as_tuple()
    coefficient_text = "".join(str(digit) for digit in digits) or "0"
    coefficient = int(coefficient_text)
    if coefficient == 0:
        scaled = 0
    else:
        power = exponent + 6
        if power >= 0:
            if len(coefficient_text.lstrip("0")) + power > 20:
                _bad("decimal_scale_or_range")
            scaled = coefficient * (10**power)
        else:
            places = -power
            if places > len(coefficient_text):
                _bad("decimal_scale_or_range")
            divisor = 10**places
            if coefficient % divisor:
                _bad("decimal_scale_or_range")
            scaled = coefficient // divisor
        if scaled > 99999999999999999999:
            _bad("decimal_scale_or_range")
    scaled = -scaled if sign else scaled
    if positive and scaled <= 0:
        _bad("decimal_scale_or_range")
    return scaled


def _from_micro(value: int) -> Decimal:
    sign = int(value < 0)
    digits = tuple(int(ch) for ch in str(abs(value))) or (0,)
    return Decimal((sign, digits, -6))


def _d(v: Decimal, positive: bool = False) -> Decimal:
    return _from_micro(_micro(v, positive))


def _uuid(v: str) -> str:
    if type(v) is not str:
        _bad("uuid_invalid")
    try:
        if str(UUID(v)) != v:
            _bad("uuid_not_canonical")
    except ValueError:
        _bad("uuid_invalid")
    return v


def _hex(v: str) -> str:
    if type(v) is not str or len(v) != 64 or any(c not in "0123456789abcdef" for c in v):
        _bad("digest_invalid")
    return v


def _json(v: object) -> str:
    def encode(value: object) -> str:
        if type(value) is Decimal:
            return format(_d(value), ".6f")
        raise TypeError(f"unsupported canonical value: {type(value).__name__}")

    return json.dumps(
        v,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=encode,
    )


def _sha(v: str) -> str:
    return hashlib.sha256(v.encode("utf-8")).hexdigest()


def _plain(v: object) -> object:
    if type(v) is Decimal:
        return format(_d(v), ".6f")
    if isinstance(v, datetime):
        return _utc(v).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if is_dataclass(v):
        return {
            row.name: _plain(getattr(v, row.name))
            for row in fields(v)
            if row.name != "expected_digest"
        }
    if isinstance(v, tuple):
        return [_plain(x) for x in v]
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _plain(value) for k, value in v.items()}
    if v is None or type(v) in (str, int, bool):
        return v
    _bad("canonical_value_unsupported")


def _canonical_payload(value: object) -> str:
    if not is_dataclass(value):
        _bad("canonical_object_required")
    dynamic_assignment_fields = (
        {
            "task_status",
            "assignee_sub",
            "state",
            "current_member_vector",
            "current_owned_holds",
            "action",
        }
        if isinstance(value, Assignment)
        else set()
    )
    body = {
        row.name: _plain(getattr(value, row.name))
        for row in fields(value)
        if row.name not in {"canonical_payload", "digest", "expected_digest"}
        and row.name not in dynamic_assignment_fields
    }
    return _json(body)


def _utc(v: datetime) -> datetime:
    if not isinstance(v, datetime) or v.tzinfo is None or v.utcoffset() is None:
        _bad("time_not_aware")
    return v.astimezone(timezone.utc)


def _fixed(v: Decimal | None) -> str | None:
    return None if v is None else format(_d(v), ".6f")


def _profile_payload(p: NativeProfile, slots: tuple[Slot, ...]) -> dict[str, object]:
    return {
        "profile": {
            "id": p.id,
            "organization_id": p.organization_id,
            "warehouse_id": p.warehouse_id,
            "revision": p.revision,
            "packaging_id": p.packaging_id,
            "location_id": p.location_id,
            "resource_id": p.resource_id,
            "length_mm": _fixed(p.length_mm),
            "width_mm": _fixed(p.width_mm),
            "height_mm": _fixed(p.height_mm),
            "opening_width_mm": _fixed(p.opening_width_mm),
            "opening_height_mm": _fixed(p.opening_height_mm),
            "clearance_mm": _fixed(p.clearance_mm),
            "gross_limit_kg": _fixed(p.gross_limit_kg),
            "tare_kg": _fixed(p.tare_kg),
            "top_load_limit_kg": _fixed(p.top_load_limit_kg),
            "max_layers": p.max_layers,
            "orientations": None if p.orientations is None else list(p.orientations),
            "load_type": p.load_type,
            "support_rule": p.support_rule,
            "access_method": p.access_method,
            "equipment_ref": p.equipment_ref,
            "restrictions": None if p.restrictions is None else list(p.restrictions),
            "source_ref": p.source_ref,
            "source_revision": p.source_revision,
            "evidence": p.evidence,
            "approved_by": p.approved_by,
            "approved_at": _utc(p.approved_at)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
            "bound_mapping_revision": p.bound_mapping_revision,
            "ancestor_binding": None
            if p.ancestor_binding is None
            else [
                {"resource_id": rid, "parent_id": parent, "topology_revision": revision}
                for rid, parent, revision in p.ancestor_binding
            ],
        },
        "slots": [
            {
                "warehouse_id": s.warehouse_id,
                "location_id": s.location_id,
                "profile_id": s.profile_id,
                "slot_key": s.key,
                "x_mm": _fixed(s.x),
                "y_mm": _fixed(s.y),
                "z_mm": _fixed(s.z),
                "width_mm": _fixed(s.width),
                "height_mm": _fixed(s.height),
                "depth_mm": _fixed(s.depth),
            }
            for s in sorted(slots, key=lambda x: x.key)
        ],
    }


def _profile_ok(row: Profile, f: Facts) -> None:
    p = row.native
    _uuid(p.id)
    _hex(row.digest)
    if (
        type(p.organization_id) is not int
        or type(p.warehouse_id) is not int
        or (p.organization_id, p.warehouse_id) != (f.organization_id, f.warehouse_id)
    ):
        _bad("profile_scope_mismatch")
    if type(p.revision) is not int or not 1 <= p.revision <= 2147483647:
        _bad("profile_revision_invalid")
    if (
        not row.sealed
        or type(p.approved_by) is not str
        or not p.approved_by.strip()
        or not p.approved_at
    ):
        _bad("profile_not_approved")
    _utc(p.approved_at)
    targets = (p.packaging_id is not None, p.location_id is not None, p.resource_id is not None)
    if sum(targets) != 1:
        _bad("profile_target_not_exclusive")
    if p.packaging_id is not None:
        if (
            type(p.packaging_id) is not int
            or p.packaging_id <= 0
            or row.slots
            or p.bound_mapping_revision is not None
            or p.ancestor_binding is not None
            or row.bound_resources
        ):
            _bad("packaging_profile_target_invalid")
    elif p.location_id is not None:
        loc = next((x for x in f.locations if x.id == p.location_id), None)
        if (
            loc is None
            or p.resource_id is not None
            or type(p.bound_mapping_revision) is not int
            or p.bound_mapping_revision != loc.mapping_revision
            or p.ancestor_binding != loc.resource_path
            or row.bound_resources != loc.resource_path
        ):
            _bad("location_profile_topology_binding_missing")
        _path(loc, f.resources)
    else:
        resource = next((x for x in f.resources if x.id == p.resource_id), None)
        if (
            resource is None
            or p.location_id is not None
            or row.slots
            or type(p.bound_mapping_revision) is not int
            or p.bound_mapping_revision != resource.topology_revision
        ):
            _bad("resource_profile_target_invalid")
        path, binding = _resource_binding(resource.id, f.resources)
        if p.ancestor_binding != binding or row.bound_resources != binding:
            _bad("resource_profile_topology_binding_mismatch")
    if _sha(row.canonical_payload) != row.digest:
        _bad("profile_digest_mismatch")
    payload = json.loads(row.canonical_payload)
    if _json(payload) != row.canonical_payload or payload != _profile_payload(p, row.slots):
        _bad("profile_payload_mismatch")
    for v in (
        p.length_mm,
        p.width_mm,
        p.height_mm,
        p.opening_width_mm,
        p.opening_height_mm,
        p.gross_limit_kg,
    ):
        if v is not None:
            _d(v, True)
    for v in (p.clearance_mm, p.tare_kg, p.top_load_limit_kg):
        if v is not None and _micro(v) < 0:
            _bad("profile_measure_negative")
    # Missing geometry/openings remain CannotPlan facts; malformed present values
    # are rejected by the exact Decimal6 and sealed payload checks.
    if p.orientations is not None and (
        len(p.orientations) != len(set(p.orientations))
        or any(x not in ORIENTATIONS for x in p.orientations)
    ):
        _bad("profile_orientation_invalid")
    if p.access_method not in {None, "manual", "equipment"}:
        _bad("profile_access_unknown")
    if p.restrictions is not None and (
        type(p.restrictions) is not tuple
        or any(type(x) is not str or not x.strip() for x in p.restrictions)
        or len(set(p.restrictions)) != len(p.restrictions)
    ):
        _bad("profile_restrictions_invalid")
    if p.access_method == "equipment" and (
        type(p.equipment_ref) is not str or not p.equipment_ref.strip()
    ):
        _bad("profile_equipment_reference_missing")
    if len({s.key for s in row.slots}) != len(row.slots):
        _bad("duplicate_slot")
    for s in row.slots:
        if (s.warehouse_id, s.location_id, s.profile_id) != (p.warehouse_id, p.location_id, p.id):
            _bad("slot_scope_mismatch")
        if any(_micro(v) < 0 for v in (s.x, s.y, s.z)):
            _bad("slot_origin_negative")
        for v in (s.width, s.height, s.depth):
            _d(v, True)
        if p.location_id is not None and (
            p.length_mm is None
            or p.width_mm is None
            or p.height_mm is None
            or _micro(s.x) + _micro(s.width) > _micro(p.length_mm)
            or _micro(s.y) + _micro(s.height) > _micro(p.height_mm)
            or _micro(s.z) + _micro(s.depth) > _micro(p.width_mm)
        ):
            _bad("slot_outside_profile_envelope")


def _path(loc: Location, resources: tuple[Resource, ...]) -> tuple[str, ...]:
    ids, binding = _resource_binding(loc.resource_id, resources)
    if binding != loc.resource_path:
        _bad("topology_binding_mismatch")
    return ids


def _resource_binding(
    resource_id: str | None, resources: tuple[Resource, ...]
) -> tuple[tuple[str, ...], tuple[tuple[str, str | None, int], ...]]:
    by_id = {r.id: r for r in resources}
    if len(by_id) != len(resources):
        _bad("duplicate_resource")
    ids: list[str] = []
    seen: set[str] = set()
    current: str | None = resource_id
    while current is not None:
        if current in seen or current not in by_id:
            _bad("resource_cycle_or_missing")
        _uuid(current)
        seen.add(current)
        resource = by_id[current]
        if (
            type(resource.topology_revision) is not int
            or resource.topology_revision <= 0
            or not resource.kind.strip()
        ):
            _bad("resource_topology_fact_invalid")
        ids.append(current)
        if resource.parent_id is not None and resource.parent_id not in by_id:
            _bad("resource_parent_missing")
        current = resource.parent_id
    if (
        not ids
        or by_id[ids[-1]].parent_id is not None
        or by_id[ids[-1]].kind != "warehouse"
        or any(r.kind == "warehouse" and r.parent_id is not None for r in by_id.values())
    ):
        _bad("resource_root_invalid")
    binding = tuple((rid, by_id[rid].parent_id, by_id[rid].topology_revision) for rid in ids)
    return tuple(ids), binding


def _current_resource_profile(f: Facts, resource_id: str) -> Profile | None:
    rows = [p for p in f.profiles if p.native.resource_id == resource_id]
    if not rows:
        return None
    highest = max(p.native.revision for p in rows)
    current = [p for p in rows if p.native.revision == highest]
    if len(current) != 1:
        _bad("duplicate_current_resource_profile")
    return current[0]


def _current_packaging_profile(f: Facts, packaging_id: int | None) -> Profile | None:
    if packaging_id is None:
        return None
    rows = [p for p in f.profiles if p.native.packaging_id == packaging_id]
    if not rows:
        return None
    revision = max(p.native.revision for p in rows)
    current = [p for p in rows if p.native.revision == revision]
    if len(current) != 1:
        _bad("duplicate_current_packaging_profile")
    return current[0]


def _current_location_profile(f: Facts, location_id: int) -> Profile | None:
    rows = [p for p in f.profiles if p.native.location_id == location_id]
    if not rows:
        return None
    highest = max(p.native.revision for p in rows)
    current = [p for p in rows if p.native.revision == highest]
    if len(current) != 1:
        _bad("duplicate_current_location_profile")
    return current[0]


def _bound(p: Profile, loc: Location) -> bool:
    n = p.native
    try:
        ancestors = (
            None
            if n.ancestor_binding is None
            else _json(
                [
                    {"resource_id": rid, "parent_id": parent, "topology_revision": revision}
                    for rid, parent, revision in n.ancestor_binding
                ]
            )
        )
        expected = _json(
            [
                {"resource_id": rid, "parent_id": parent, "topology_revision": revision}
                for rid, parent, revision in loc.resource_path
            ]
        )
    except (TypeError, ValueError):
        return False
    return (
        n.packaging_id is None
        and n.resource_id is None
        and n.location_id == loc.id
        and n.bound_mapping_revision == loc.mapping_revision
        and p.frame_id is not None
        and p.frame_id == loc.frame_id
        and p.bound_resources == loc.resource_path
        and ancestors == expected
    )


def _box(p: Profile, keys: tuple[str, ...]) -> Box:
    by_key = {s.key: s for s in p.slots}
    if (
        not keys
        or len(keys) != len(set(keys))
        or any(type(k) is not str or k not in by_key for k in keys)
    ):
        _bad("slot_group_missing_or_duplicate")
    parts = [Box(s.x, s.y, s.z, s.width, s.height, s.depth) for s in (by_key[k] for k in keys)]
    if any(_overlap(a, b) for i, a in enumerate(parts) for b in parts[i + 1 :]):
        _bad("slot_group_overlap")
    x0 = min(_micro(b.x) for b in parts)
    y0 = min(_micro(b.y) for b in parts)
    z0 = min(_micro(b.z) for b in parts)
    x1 = max(_micro(b.x) + _micro(b.w) for b in parts)
    y1 = max(_micro(b.y) + _micro(b.h) for b in parts)
    z1 = max(_micro(b.z) + _micro(b.d) for b in parts)
    part_volume = sum((_micro(b.w) * _micro(b.h) * _micro(b.d) for b in parts), 0)
    box_volume = (x1 - x0) * (y1 - y0) * (z1 - z0)
    if part_volume != box_volume:
        _bad("slot_group_not_exact_cuboid")
    return Box(
        _from_micro(x0),
        _from_micro(y0),
        _from_micro(z0),
        _from_micro(x1 - x0),
        _from_micro(y1 - y0),
        _from_micro(z1 - z0),
    )


def _overlap(a: Box, b: Box) -> bool:
    ax, ay, az = (_micro(a.x), _micro(a.y), _micro(a.z))
    bx, by, bz = (_micro(b.x), _micro(b.y), _micro(b.z))
    return (
        ax < bx + _micro(b.w)
        and bx < ax + _micro(a.w)
        and ay < by + _micro(b.h)
        and by < ay + _micro(a.h)
        and az < bz + _micro(b.d)
        and bz < az + _micro(a.d)
    )


def _members(c: Carrier) -> tuple[tuple[str, str, str, Decimal], ...]:
    rows = []
    for u in c.members:
        _uuid(u.id)
        _d(u.qty)
        _d(u.reserved)
        if (
            type(u.sku) is not str
            or not u.sku.strip()
            or type(u.lot) is not str
            or not u.lot.strip()
            or u.qty < 0
            or u.reserved < 0
            or u.reserved > u.qty
            or (u.fefo_rank is not None and (type(u.fefo_rank) is not int or u.fefo_rank < 0))
            or u.eligibility not in {"eligible", "ineligible", "unknown"}
        ):
            _bad("member_vector_invalid")
        rows.append((u.id, u.sku, u.lot, _d(u.qty)))
    if len({r[0] for r in rows}) != len(rows):
        _bad("duplicate_member")
    return tuple(sorted(rows))


def _policy_error(f: Facts, q: Query, p: Policy, now: datetime) -> str | None:
    _uuid(p.id)
    _hex(p.digest)
    _hex(p.server_binding_digest)
    if _canonical_payload(p) != p.canonical_payload or _sha(p.canonical_payload) != p.digest:
        _bad("policy_digest_mismatch")
    if (p.organization_id, p.warehouse_id, p.id) != (
        f.organization_id,
        f.warehouse_id,
        q.work_policy_id,
    ):
        return "work_policy_binding_mismatch"
    if _utc(p.valid_from) > now or now >= _utc(p.valid_until):
        return "work_policy_not_current"
    if (
        f.actor_sub is None
        or f.actor_sub not in f.current_member_subs
        or f.actor_sub not in p.operator_subs
        or len(p.operator_subs) != len(set(p.operator_subs))
    ):
        return "current_operator_authority_unknown"
    if p.access_method == "manual":
        if p.manual_gross_limit_kg is None or p.manual_height_limit_mm is None:
            return "manual_access_limits_unknown"
        if any(x is not None for x in (p.equipment_resource_id, p.equipment_profile_digest)):
            return "manual_equipment_binding_contradiction"
    elif p.access_method == "equipment":
        if (
            p.equipment_resource_id is None
            or p.equipment_profile_digest is None
            or p.equipment_state != "available"
            or p.equipment_gross_limit_kg is None
            or p.equipment_height_limit_mm is None
        ):
            return "equipment_context_unknown"
        equipment = _current_resource_profile(f, p.equipment_resource_id)
        if (
            equipment is None
            or equipment.digest != p.equipment_profile_digest
            or equipment.native.resource_id != p.equipment_resource_id
        ):
            return "equipment_profile_binding_unknown"
    else:
        return "work_access_method_unknown"
    if len({r.resource_id for r in p.load_rules}) != len(p.load_rules):
        _bad("duplicate_load_rule")
    for v in (
        p.manual_gross_limit_kg,
        p.manual_height_limit_mm,
        p.equipment_gross_limit_kg,
        p.equipment_height_limit_mm,
    ):
        if v is not None:
            _d(v, True)
    for rule in p.load_rules:
        _uuid(rule.resource_id)
        if rule.state not in {"required", "confirmed_non_load_bearing", "unknown"}:
            _bad("load_rule_state_invalid")
        if rule.gross_limit_kg is not None:
            _d(rule.gross_limit_kg, True)
        if rule.source_profile_id is None or rule.source_profile_digest is None:
            return "load_source_binding_unknown"
        _uuid(rule.source_profile_id)
        _hex(rule.source_profile_digest)
    return None


def _native_access_error(facts: Facts, policy: Policy, profile: Profile) -> str | None:
    n = profile.native
    if n.restrictions is None:
        return "native_restrictions_unknown"
    if n.restrictions:
        return "native_restrictions_unverified"
    if n.access_method == "manual":
        if policy.access_method != "manual" or n.equipment_ref is not None:
            return "native_access_policy_mismatch"
        return None
    if n.access_method == "equipment":
        if (
            policy.access_method != "equipment"
            or n.equipment_ref != policy.equipment_resource_id
            or policy.equipment_state != "available"
        ):
            return "native_equipment_policy_mismatch"
        equipment = _current_resource_profile(facts, n.equipment_ref)
        if (
            equipment is None
            or equipment.digest != policy.equipment_profile_digest
            or equipment.native.resource_id != n.equipment_ref
        ):
            return "native_equipment_profile_stale"
        return None
    return "native_access_unknown"


def _hold_error(a: Assignment, carrier: Carrier) -> str | None:
    for hold in a.owned_holds + a.current_owned_holds:
        if (
            type(hold.hold_id) is not int
            or not 1 <= hold.hold_id <= 2147483647
        ):
            _bad("hold_id_invalid")
    if a.owned_holds != a.current_owned_holds:
        return "existing_assignment_holds_changed"
    members = {u.id: u for u in carrier.members}
    totals: dict[str, int] = {}
    hold_ids: set[int] = set()
    for hold in a.owned_holds:
        _uuid(hold.unit_id)
        quantity = _micro(hold.qty, True)
        unit = members.get(hold.unit_id)
        if unit is None or hold.hold_id in hold_ids or hold.sku != unit.sku or hold.lot != unit.lot:
            return "existing_assignment_hold_identity_mismatch"
        hold_ids.add(hold.hold_id)
        totals[hold.unit_id] = totals.get(hold.unit_id, 0) + quantity
    if set(totals) - set(members):
        return "existing_assignment_foreign_hold"
    for unit in carrier.members:
        qty, reserved = _micro(unit.qty), _micro(unit.reserved)
        held = totals.get(unit.id, 0)
        if qty == 0:
            if reserved != 0 or held != 0:
                return "zero_quantity_member_has_hold"
        elif reserved != qty or held != reserved:
            return "existing_assignment_hold_conservation_mismatch"
    return None


def _assignment_error(f: Facts, q: Query, a: Assignment, c: Carrier) -> str | None:
    if (
        a.task_kind != "cap_place"
        or a.task_qty is not None
        or a.task_unit_id is not None
        or a.task_hold_id is not None
        or a.task_destination_id != a.destination_location_id
    ):
        return "existing_task_protocol_mismatch"
    if (a.organization_id, a.warehouse_id) != (f.organization_id, f.warehouse_id):
        return "existing_assignment_scope_mismatch"
    if (
        a.assignee_sub != f.actor_sub
        or a.carrier_id != c.id
        or q.carrier_ids != (c.id,)
        or a.source_location_id != c.location_id
    ):
        return "existing_assignment_owner_or_carrier_mismatch"
    if a.action not in {"begin", "complete"}:
        return "existing_assignment_action_unknown"
    expected_state = ("assigned", "open") if a.action == "begin" else ("in_transit", "claimed")
    if (a.state, a.task_status) != expected_state:
        return "existing_assignment_state_mismatch"
    if a.work_policy_id != q.work_policy_id or a.expected_digest != a.digest:
        return "existing_assignment_digest_or_policy_mismatch"
    if (a.source_revision, a.source_digest) != (c.content_revision, c.content_digest):
        return "existing_assignment_source_changed"
    if (
        a.gross_kg != c.gross_kg
        or a.tare_kg != c.tare_kg
        or a.packaging_profile_id != c.packaging_profile_id
    ):
        return "existing_assignment_physical_frame_changed"
    if a.member_vector != a.current_member_vector or a.member_vector != _members(c):
        return "existing_assignment_member_vector_changed"
    if _canonical_payload(a) != a.canonical_payload or _sha(a.canonical_payload) != a.digest:
        return "existing_assignment_immutable_snapshot_changed"
    if (
        len(a.claims) != 1
        or len([x for x in f.active_claims if x.task_id == a.task_id]) != 1
        or a.claims[0] not in f.active_claims
    ):
        return "existing_assignment_claim_missing_or_ambiguous"
    claim = a.claims[0]
    if (
        claim.task_id != a.task_id
        or claim.carrier_id != c.id
        or claim.location_id != a.destination_location_id
        or claim.profile_id != a.destination_profile_id
        or claim.orientation not in ORIENTATIONS
        or not claim.slot_keys
        or len(claim.slot_keys) != len(set(claim.slot_keys))
    ):
        return "existing_assignment_claim_header_mismatch"
    if q.candidate_location_ids != (claim.location_id,):
        return "existing_assignment_destination_changed"
    if _hold_error(a, c):
        return _hold_error(a, c)
    return None


def _dims(p: NativeProfile, o: str) -> tuple[Decimal, Decimal, Decimal] | None:
    if None in (p.length_mm, p.width_mm, p.height_mm):
        return None
    v = {"L": _d(p.length_mm, True), "W": _d(p.width_mm, True), "H": _d(p.height_mm, True)}
    return v[o[0]], v[o[2]], v[o[1]]  # Names are ordered (X,Z,Y): LWH => X=L, Y=H, Z=W.


def _fits(pack: NativeProfile, dest: NativeProfile, route: Route, o: str, slot: Box) -> bool:
    if (
        pack.orientations is None
        or dest.orientations is None
        or o not in pack.orientations
        or o not in dest.orientations
    ):
        return False
    dims = _dims(pack, o)
    if (
        dims is None
        or pack.clearance_mm is None
        or dest.clearance_mm is None
        or pack.opening_width_mm is None
        or pack.opening_height_mm is None
        or dest.opening_width_mm is None
        or dest.opening_height_mm is None
    ):
        return False
    needed = tuple(d + 2 * max(_d(pack.clearance_mm), _d(dest.clearance_mm)) for d in dims)
    if needed[0] > slot.w or needed[1] > slot.h or needed[2] > slot.d:
        return False
    if (
        needed[2] > _d(pack.opening_width_mm, True)
        or needed[1] > _d(pack.opening_height_mm, True)
        or needed[2] > _d(dest.opening_width_mm, True)
        or needed[1] > _d(dest.opening_height_mm, True)
    ):
        return False
    axes = {"x": needed[0], "y": needed[1], "z": needed[2]}
    for opening in route.openings:
        if opening.clearance_mm is None:
            return False
        extra = 2 * _d(opening.clearance_mm)
        if axes[opening.width_axis] + extra > _d(opening.width_mm, True):
            return False
        if axes[opening.height_axis] + extra > _d(opening.height_mm, True):
            return False
    return bool(
        route.openings
    )  # Native opening values must be compiled into these bound route openings.


def _pending(f: Facts, own: Assignment | None) -> tuple[Claim, ...]:
    by_task = {a.task_id: a for a in f.active_assignments}
    grouped: dict[str, list[Claim]] = {}
    for claim in f.active_claims:
        grouped.setdefault(claim.task_id, []).append(claim)
    if (
        len(by_task) != len(f.active_assignments)
        or len({a.carrier_id for a in f.active_assignments}) != len(by_task)
        or set(grouped) != set(by_task)
        or len(set(f.active_claims)) != len(f.active_claims)
    ):
        _bad("orphan_or_missing_active_claim")
    locations = {x.id: x for x in f.locations}
    carriers = {x.id: x for x in f.carriers}
    valid_paths = {_path(loc, f.resources) for loc in f.locations}
    all_hold_ids: set[int] = set()
    for task, assignment in by_task.items():
        source = locations.get(assignment.source_location_id)
        destination = locations.get(assignment.destination_location_id)
        carrier = carriers.get(assignment.carrier_id)
        if source is None or destination is None or carrier is None:
            _bad("active_assignment_facts_missing")
        source_path, destination_path = _path(source, f.resources), _path(destination, f.resources)
        source_profile = _current_location_profile(f, source.id)
        destination_profile = _current_location_profile(f, destination.id)
        current_package = _current_packaging_profile(f, carrier.packaging_id)
        assignment_gross = _micro(assignment.gross_kg)
        assignment_tare = _micro(assignment.tare_kg)
        if assignment_gross < 0 or assignment_tare < 0 or assignment_gross < assignment_tare:
            _bad("pending_carrier_mass_inconsistent")
        claims = grouped[task]
        if (
            assignment.state not in ACTIVE
            or assignment.action not in {"begin", "complete"}
            or (assignment.state, assignment.task_status)
            != (("assigned", "open") if assignment.action == "begin" else ("in_transit", "claimed"))
            or len(claims) != 1
            or len(assignment.claims) != 1
            or set(claims) != set(assignment.claims)
            or assignment.source_path != source_path
            or source_path not in assignment.phase_paths
            or destination_path not in assignment.phase_paths
            or any(path not in valid_paths for path in assignment.phase_paths)
            or assignment.task_kind != "cap_place"
            or assignment.task_qty is not None
            or assignment.task_unit_id is not None
            or assignment.task_hold_id is not None
            or assignment.task_destination_id != assignment.destination_location_id
            or (assignment.warehouse_id, assignment.organization_id)
            != (f.warehouse_id, f.organization_id)
            or assignment.expected_digest != assignment.digest
            or _canonical_payload(assignment) != assignment.canonical_payload
            or _sha(assignment.canonical_payload) != assignment.digest
            or assignment.member_vector != assignment.current_member_vector
            or assignment.member_vector != _members(carrier)
            or carrier.member_digest is None
            or _sha(_json(_members(carrier))) != carrier.member_digest
            or carrier.state != "present"
            or carrier.confirmed_content_revision != carrier.content_revision
            or carrier.location_id != source.id
            or carrier.content_revision != assignment.source_revision
            or carrier.content_digest != assignment.source_digest
            or carrier.gross_kg != assignment.gross_kg
            or carrier.tare_kg != assignment.tare_kg
            or carrier.packaging_profile_id != assignment.packaging_profile_id
            or source_profile is None
            or source.current_profile_id != source_profile.native.id
            or assignment.source_profile_id != source_profile.native.id
            or destination_profile is None
            or destination.current_profile_id != destination_profile.native.id
            or assignment.destination_profile_id != destination_profile.native.id
            or current_package is None
            or assignment.packaging_profile_id != current_package.native.id
        ):
            _bad("active_claim_snapshot_mismatch")
        claim = claims[0]
        if (
            claim.task_id != task
            or claim.carrier_id != assignment.carrier_id
            or claim.location_id != assignment.destination_location_id
            or claim.profile_id != assignment.destination_profile_id
            or claim.frame_id != destination_profile.frame_id
            or claim.orientation not in ORIENTATIONS
            or not claim.slot_keys
            or len(claim.slot_keys) != len(set(claim.slot_keys))
        ):
            _bad("active_claim_header_mismatch")
        hold_issue = _hold_error(assignment, carrier)
        if hold_issue:
            _bad(hold_issue)
        for hold in assignment.owned_holds:
            if hold.hold_id in all_hold_ids:
                _bad("duplicate_active_hold_id")
            all_hold_ids.add(hold.hold_id)
    return tuple(claim for claim in f.active_claims if own is None or claim.task_id != own.task_id)


def _load(
    f: Facts,
    policy: Policy,
    path: tuple[str, ...],
    carrier: Carrier,
    source_path: tuple[str, ...],
    own: Assignment | None,
    extra: dict[str, Decimal],
) -> tuple[str | None, dict[str, Decimal]]:
    actual: dict[str, int] = {}
    pending: dict[str, int] = {}
    rules = {rule.resource_id: rule for rule in policy.load_rules}
    locations = {loc.id: loc for loc in f.locations}
    for current in f.carriers:
        loc = locations.get(current.location_id)
        if current.state in {"present", "unknown"} and loc is None:
            return "actual_location_unknown", {}
        if loc is None:
            continue
        overlap = set(_path(loc, f.resources)) & set(path)
        if not overlap:
            continue
        if current.state == "unknown":
            return "actual_carrier_state_unknown", {}
        if current.state != "present":
            continue
        if current.gross_kg is None or current.tare_kg is None:
            return "actual_gross_unknown", {}
        if (
            current.confirmed_content_revision != current.content_revision
            or current.content_digest is None
            or current.member_digest is None
            or not current.positions
        ):
            return "actual_carrier_confirmation_unknown", {}
        if _sha(_json(_members(current))) != current.member_digest:
            _bad("actual_carrier_member_digest_mismatch")
        current_profile = _current_location_profile(f, loc.id)
        profile_rows = {row.native.id: row for row in f.profiles}
        for position in current.positions:
            position_profile = profile_rows.get(position.profile_id)
            if (
                current_profile is None
                or position_profile is None
                or position.frame_id != current_profile.frame_id
                or position_profile.frame_id != current_profile.frame_id
                or not _bound(position_profile, loc)
            ):
                return "actual_coordinate_binding_unknown", {}
            try:
                _box(position_profile, position.slot_keys)
            except PlacementInputError:
                return "actual_footprint_ambiguous", {}
        gross, tare = _micro(current.gross_kg), _micro(current.tare_kg)
        if gross < tare:
            _bad("actual_carrier_mass_inconsistent")
        for rid in overlap:
            actual[rid] = actual.get(rid, 0) + gross
    for assignment in f.active_assignments:
        gross, tare = _micro(assignment.gross_kg), _micro(assignment.tare_kg)
        if gross < tare:
            _bad("pending_carrier_mass_inconsistent")
        moved_resources = set().union(*assignment.phase_paths) - set(assignment.source_path)
        for rid in moved_resources & set(path):
            pending[rid] = pending.get(rid, 0) + gross
    delta = (
        {}
        if own is not None
        else {rid: _d(carrier.gross_kg) for rid in set(path) - set(source_path)}
    )
    for rid in path:
        rule = rules.get(rid)
        if rule is None or rule.state == "unknown":
            return "required_load_limit_unknown", {}
        native = _current_resource_profile(f, rid)
        if (
            native is None
            or rule.source_profile_id != native.native.id
            or rule.source_profile_digest != native.digest
        ):
            return "load_source_binding_unknown", {}
        if native.native.resource_id != rid:
            _bad("load_profile_target_mismatch")
        access_issue = _native_access_error(f, policy, native)
        if access_issue:
            return access_issue, {}
        native_limit = (
            None
            if native.native.gross_limit_kg is None
            else _micro(native.native.gross_limit_kg, True)
        )
        if rule.state == "confirmed_non_load_bearing":
            if rule.gross_limit_kg is not None:
                _bad("non_load_bearing_limit_contradiction")
            policy_limit = None
        else:
            if rule.gross_limit_kg is None:
                return "required_load_limit_unknown", {}
            policy_limit = _micro(rule.gross_limit_kg, True)
        limits = [v for v in (native_limit, policy_limit) if v is not None]
        if not limits:
            if rule.state == "required":
                return "required_load_limit_unknown", {}
            continue
        limit = min(limits)
        total = (
            actual.get(rid, 0)
            + pending.get(rid, 0)
            + _micro(extra.get(rid, ZERO))
            + _micro(delta.get(rid, ZERO))
        )
        if total > limit:
            return "gross_limit_exceeded", {}
    return None, delta


def _support(
    f: Facts,
    dest: Profile,
    slot: Box,
    gross: Decimal,
    positions: tuple[Position, ...],
    claims: tuple[Claim, ...],
    carriers: dict[str, Carrier],
    profiles: dict[str, Profile],
    support_extra: dict[str, int],
) -> tuple[str | None, tuple[str, ...], str | None]:
    n = dest.native
    if (
        n.support_rule not in {"full_base", "layered"}
        or n.load_type != "distributed"
        or type(n.max_layers) is not int
        or n.max_layers < 1
    ):
        return None, (), "support_model_unknown"
    loc = next((x for x in f.locations if x.id == n.location_id), None)
    if loc is None:
        return None, (), "support_location_unknown"
    location_path = set(_path(loc, f.resources))
    slot_x, slot_y, slot_z = (_micro(slot.x), _micro(slot.y), _micro(slot.z))
    slot_x1, _slot_y1, slot_z1 = (
        slot_x + _micro(slot.w),
        slot_y + _micro(slot.h),
        slot_z + _micro(slot.d),
    )
    for support in f.supports:
        if (support.location_id, support.profile_id, support.profile_digest, support.frame_id) != (
            n.location_id,
            n.id,
            dest.digest,
            dest.frame_id,
        ):
            continue
        plane = _micro(support.plane_y)
        sx0, sx1, sz0, sz1 = map(
            _micro, (support.x_min, support.x_max, support.z_min, support.z_max)
        )
        if (
            plane != slot_y
            or sx0 > slot_x
            or sx1 < slot_x1
            or sz0 > slot_z
            or sz1 < slot_z1
            or type(support.layer) is not int
            or support.layer < 0
            or support.layer >= n.max_layers
        ):
            continue
        if (
            n.support_rule == "full_base"
            and support.layer != 0
            or support.layer > 0
            and support.bearing_kind != "carrier"
        ):
            continue
        if support.bearing_kind == "resource":
            if (
                support.layer != 0
                or support.assignment_task_id is not None
                or support.bearing_id not in location_path
            ):
                continue
            return support.bearing_id, (), None
        if support.bearing_kind != "carrier":
            _bad("support_bearing_kind_invalid")
        lower = carriers.get(support.bearing_id)
        if lower is None or lower.state != "present":
            continue
        if lower.id in {assignment.carrier_id for assignment in f.active_assignments}:
            return None, (), "pending_support_dependency_unproven"
        if lower.location_id != loc.id:
            continue
        lower_positions = [p for p in positions if p.carrier_id == lower.id]
        if not lower_positions:
            return None, (), "support_carrier_position_unknown"
        if any(p.support_carrier_id is not None for p in lower_positions):
            return None, (), "deeper_support_chain_unproven"
        if (
            support.assignment_task_id is not None
            or lower.gross_kg is None
            or lower.tare_kg is None
            or _micro(lower.gross_kg) < _micro(lower.tare_kg)
        ):
            return None, (), "support_carrier_mass_unknown"
        lower_package = profiles.get(lower.packaging_profile_id or "")
        current_package = _current_packaging_profile(f, lower.packaging_id)
        if (
            current_package is None
            or lower_package is None
            or current_package.native.id != lower_package.native.id
        ):
            return None, (), "support_packaging_profile_not_current"
        access_issue = _native_access_error(f, f.policy, lower_package)
        if access_issue:
            return None, (), access_issue
        if (
            lower_package.native.packaging_id != lower.packaging_id
            or lower_package.native.top_load_limit_kg is None
        ):
            return None, (), "support_top_load_unknown"
        lower_limit = _micro(lower_package.native.top_load_limit_kg)
        if support.top_load_limit_kg is None:
            return None, (), "support_top_load_unknown"
        limit = min(lower_limit, _micro(support.top_load_limit_kg))
        covered = False
        lower_boxes = []
        for lower_position in lower_positions:
            lower_profile = profiles.get(lower_position.profile_id)
            if (
                lower_position.frame_id != dest.frame_id
                or lower_profile is None
                or not _bound(lower_profile, loc)
            ):
                return None, (), "support_coordinate_binding_unknown"
            try:
                lower_box = _box(lower_profile, lower_position.slot_keys)
            except PlacementInputError:
                return None, (), "support_footprint_unknown"
            lower_boxes.append(lower_box)
            lx, ly, lz = (_micro(lower_box.x), _micro(lower_box.y), _micro(lower_box.z))
            if (
                ly + _micro(lower_box.h) == slot_y
                and lx <= slot_x
                and lx + _micro(lower_box.w) >= slot_x1
                and lz <= slot_z
                and lz + _micro(lower_box.d) >= slot_z1
            ):
                covered = True
        if not covered:
            continue
        existing: dict[str, int] = {}
        for position in positions:
            if position.support_carrier_id != lower.id:
                continue
            upper = carriers.get(position.carrier_id)
            if (
                upper is None
                or upper.state != "present"
                or upper.location_id != loc.id
                or upper.gross_kg is None
                or upper.tare_kg is None
                or _micro(upper.gross_kg) < _micro(upper.tare_kg)
            ):
                return None, (), "supported_carrier_mass_unknown"
            if position.frame_id != dest.frame_id:
                return None, (), "support_coordinate_binding_unknown"
            existing[upper.id] = max(existing.get(upper.id, 0), _micro(upper.gross_kg))
        for claim in claims:
            if claim.location_id != loc.id:
                if claim.support_carrier_id == lower.id:
                    return None, (), "pending_support_binding_unknown"
                continue
            claim_profile = profiles.get(claim.profile_id)
            if (
                claim_profile is None
                or claim.frame_id != dest.frame_id
                or claim_profile.frame_id != dest.frame_id
                or not _bound(claim_profile, loc)
            ):
                return None, (), "pending_support_binding_unknown"
            try:
                claim_box = _box(claim_profile, claim.slot_keys)
            except PlacementInputError:
                return None, (), "pending_support_binding_unknown"
            cx, cy, cz = map(_micro, (claim_box.x, claim_box.y, claim_box.z))
            cx1, cz1 = cx + _micro(claim_box.w), cz + _micro(claim_box.d)
            overlaps_bearing = False
            borne = False
            for lower_box in lower_boxes:
                lx, ly, lz = map(_micro, (lower_box.x, lower_box.y, lower_box.z))
                lx1, lz1 = lx + _micro(lower_box.w), lz + _micro(lower_box.d)
                if (
                    ly + _micro(lower_box.h) == cy
                    and lx < cx1
                    and cx < lx1
                    and lz < cz1
                    and cz < lz1
                ):
                    overlaps_bearing = True
                    borne = borne or (lx <= cx and cx1 <= lx1 and lz <= cz and cz1 <= lz1)
            if not overlaps_bearing and claim.support_carrier_id != lower.id:
                continue
            if not borne or claim.support_carrier_id != lower.id:
                return None, (), "pending_support_binding_unknown"
            upper = carriers.get(claim.carrier_id)
            if upper is None or upper.gross_kg is None:
                return None, (), "supported_carrier_mass_unknown"
            existing[upper.id] = max(existing.get(upper.id, 0), _micro(upper.gross_kg))
        proposed = support_extra.get(lower.id, 0)
        if sum(existing.values()) + proposed + _micro(gross) > limit:
            return None, (), "support_top_load_exceeded"
        return lower.id, (), None
    return None, (), "support_surface_unknown"


def _domain_unit_shape(unit: DomainUnit) -> None:
    if type(unit.sku) is not str or not unit.sku.strip():
        _bad("domain_sku_invalid")
    if any(type(x) is not str or len(x) > 128 for x in (unit.characteristic, unit.purpose)):
        _bad("domain_metadata_invalid")


def _corridor_domain_shape(group: CorridorDomain) -> None:
    if (
        type(group.domain) is not tuple
        or len(group.domain) != 3
        or type(group.domain[0]) is not str
        or not group.domain[0].strip()
        or any(type(x) is not str or len(x) > 128 for x in group.domain[1:])
    ):
        _bad("corridor_domain_invalid")
    if type(group.protected_unit_ids) is not tuple or not group.protected_unit_ids:
        _bad("corridor_protected_ids_invalid")
    ids = tuple(_uuid(x) for x in group.protected_unit_ids)
    if ids != tuple(sorted(set(ids))):
        _bad("corridor_protected_ids_invalid")
    if type(group.mode) is not str or group.mode not in {"fefo", "must_clear", "unknown"}:
        _bad("corridor_domain_mode_invalid")
    if group.mode == "fefo":
        if type(group.threshold) is not int or group.threshold < 0:
            _bad("corridor_domain_threshold_invalid")
    elif group.threshold is not None:
        _bad("corridor_domain_threshold_invalid")


def _domain_corridor_shape(corridor: DomainCorridor) -> None:
    if type(corridor.kind) is not str or corridor.kind != "domain_v1":
        _bad("domain_corridor_kind_invalid")
    _uuid(corridor.frame_id)
    if type(corridor.box) is not Box:
        _bad("domain_corridor_box_invalid")
    if any(_micro(x) < 0 for x in (corridor.box.x, corridor.box.y, corridor.box.z)):
        _bad("domain_corridor_box_invalid")
    if any(_micro(x) <= 0 for x in (corridor.box.w, corridor.box.h, corridor.box.d)):
        _bad("domain_corridor_box_invalid")
    if type(corridor.domains) is not tuple or any(
        type(x) is not CorridorDomain for x in corridor.domains
    ):
        _bad("domain_corridor_groups_invalid")
    for group in corridor.domains:
        _corridor_domain_shape(group)
    domains = tuple(x.domain for x in corridor.domains)
    ids = tuple(i for x in corridor.domains for i in x.protected_unit_ids)
    if domains != tuple(sorted(set(domains))) or len(ids) != len(set(ids)):
        _bad("domain_corridor_partition_invalid")


def _domain_facts_shape(facts: DomainFacts) -> None:
    value = facts.decision_date
    if type(value) is not str:
        _bad("domain_decision_date_invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        _bad("domain_decision_date_invalid")
    if parsed.isoformat() != value:
        _bad("domain_decision_date_invalid")
    _hex(facts.current_snapshot_digest)
    _hex(facts.measured_context_digest)


def _domain_family(facts: Facts, now: datetime) -> bool:
    current = type(facts) is DomainFacts
    if current:
        _domain_facts_shape(facts)
        if facts.decision_date != now.astimezone(ZoneInfo("Europe/Minsk")).date().isoformat():
            _bad("domain_decision_date_stale")
    expected_unit = DomainUnit if current else Unit
    expected_corridor = DomainCorridor if current else Corridor
    if any(type(u) is not expected_unit for c in facts.carriers for u in c.members):
        _bad("mixed_domain_unit_family")
    if facts.policy is not None:
        for route in facts.policy.routes:
            if current and type(route.corridors) is not tuple:
                _bad("domain_corridor_family_invalid")
            if any(type(x) is not expected_corridor for x in route.corridors):
                _bad("mixed_domain_corridor_family")
            if current:
                for corridor in route.corridors:
                    _domain_corridor_shape(corridor)
    if current:
        for carrier in facts.carriers:
            for unit in carrier.members:
                _domain_unit_shape(unit)
    return current


def _domain_key(unit: DomainUnit) -> tuple[str, str, str]:
    return unit.sku, unit.characteristic, unit.purpose


def _domain_bindings(facts: DomainFacts) -> tuple[dict[str, DomainUnit], Violation | None]:
    """Resolve every group, including corridors missed by all candidate boxes."""
    units = {u.id: u for c in facts.carriers for u in c.members}
    owners = {u.id: c for c in facts.carriers for u in c.members}
    confirmed_owners: set[str] = set()
    for route in facts.policy.routes:
        for corridor in route.corridors:
            for group in corridor.domains:
                protected = []
                for unit_id in group.protected_unit_ids:
                    unit = units.get(unit_id)
                    owner = owners.get(unit_id)
                    if unit is None or owner is None:
                        return units, Violation("domain_protected_member_missing", (unit_id,))
                    if owner.id not in confirmed_owners:
                        if (
                            owner.state != "present"
                            or owner.confirmed_content_revision != owner.content_revision
                            or owner.content_digest is None
                            or owner.member_digest != _sha(_json(_members(owner)))
                        ):
                            return units, Violation("domain_protected_member_stale", (unit_id,))
                        confirmed_owners.add(owner.id)
                    if _domain_key(unit) != group.domain:
                        _bad("domain_protected_domain_mismatch")
                    protected.append(unit)
                if group.mode == "fefo" and all(
                    u.qty > ZERO
                    and u.reserved == ZERO
                    and u.eligibility == "eligible"
                    and u.fefo_rank is not None
                    for u in protected
                ):
                    if group.threshold != min(u.fefo_rank for u in protected):
                        _bad("domain_fefo_threshold_mismatch")
    return units, None


def _domain_access_error(
    slot: Box, route: Route, incoming: Carrier, units: dict[str, DomainUnit]
) -> str | None:
    positive = tuple(u for u in incoming.members if u.qty > ZERO)
    for corridor in route.corridors:
        if not _overlap(slot, corridor.box):
            continue
        if not corridor.domains:
            return "access_corridor_must_remain_clear"
        for group in corridor.domains:
            protected = tuple(units[i] for i in group.protected_unit_ids)
            if group.mode == "must_clear" or any(
                u.qty == ZERO or u.reserved > ZERO or u.eligibility == "ineligible"
                for u in protected
            ):
                return "domain_access_must_remain_clear"
            if group.mode == "unknown" or any(u.eligibility == "unknown" for u in protected):
                return "domain_access_unknown"
            if any(u.fefo_rank is None for u in protected) or any(
                u.fefo_rank is None for u in positive
            ):
                return "domain_fefo_rank_unknown"
            if not positive:
                return "domain_incoming_empty"
            if any(_domain_key(u) != group.domain for u in positive):
                return "domain_fefo_domain_blocked"
            threshold = min(u.fefo_rank for u in protected)
            if any(u.fefo_rank > threshold for u in positive):
                return "domain_fefo_access_blocked"
    return None


def evaluate_current(
    facts: DomainFacts,
    query: Query,
    now_utc: datetime,
    existing_assignment: Assignment | None = None,
) -> Result:
    """Current-family pure preview entry; full trusted loader remains required."""
    if type(facts) is not DomainFacts:
        _bad("domain_facts_required")
    return evaluate(facts, query, now_utc, existing_assignment)


def evaluate(
    facts: Facts, query: Query, now_utc: datetime, existing_assignment: Assignment | None = None
) -> Result:
    """Pure deterministic preview; the returned flag is never true.

    All arithmetic runs with a fixed precision sufficient for 1,000 six-decimal
    cuboids/masses. Slot coverage itself is exact integer-microunit arithmetic.
    """
    with localcontext() as context:
        context.prec = 80
        return _evaluate(facts, query, now_utc, existing_assignment)


def _evaluate(
    facts: Facts, query: Query, now_utc: datetime, existing_assignment: Assignment | None = None
) -> Result:
    now = _utc(now_utc)
    current_family = _domain_family(facts, now)
    _hex(facts.digest)
    _uuid(query.work_policy_id)
    if (
        _canonical_payload(facts) != facts.canonical_payload
        or _sha(facts.canonical_payload) != facts.digest
    ):
        _bad("facts_digest_mismatch")
    if (
        type(query.protocol) is not int
        or query.protocol != 2
        or type(query.warehouse_id) is not int
        or query.warehouse_id != facts.warehouse_id
        or type(facts.organization_id) is not int
        or type(facts.warehouse_id) is not int
    ):
        _bad("request_scope_mismatch")
    ids = tuple(_uuid(x) for x in query.carrier_ids)
    if not ids or len(ids) > 1000 or len(ids) != len(set(ids)):
        _bad("carrier_ids_empty_duplicate_or_over_limit")
    if (
        not query.candidate_location_ids
        or len(query.candidate_location_ids) > 1000
        or len(query.candidate_location_ids) != len(set(query.candidate_location_ids))
        or any(type(x) is not int or x <= 0 for x in query.candidate_location_ids)
    ):
        _bad("destination_ids_empty_or_duplicate")

    def result(
        status: str,
        choices: tuple[Choice, ...],
        left: tuple[str, ...],
        errors: tuple[Violation, ...],
    ) -> Result:
        return Result(
            2,
            status,
            False,
            facts.digest,
            choices,
            left,
            tuple(sorted(errors, key=lambda x: (x.code, x.refs))),
        )

    if facts.warehouse_mode != "erp" or facts.policy is None:
        return result("cannot_plan", (), ids, (Violation("warehouse_or_work_context_unknown", ()),))
    policy = facts.policy
    issue = _policy_error(facts, query, policy, now)
    if issue:
        return result("cannot_plan", (), ids, (Violation(issue, ()),))
    locations = {x.id: x for x in facts.locations}
    carriers = {x.id: x for x in facts.carriers}
    profiles = {x.native.id: x for x in facts.profiles}
    resources = {x.id: x for x in facts.resources}
    if (
        len(locations) != len(facts.locations)
        or len(carriers) != len(facts.carriers)
        or len(profiles) != len(facts.profiles)
        or len(resources) != len(facts.resources)
    ):
        _bad("duplicate_fact_identity")
    for p in facts.profiles:
        _profile_ok(p, facts)
    for loc in facts.locations:
        _path(loc, facts.resources)
        current = _current_location_profile(facts, loc.id)
        if current is not None and loc.current_profile_id != current.native.id:
            _bad("location_current_profile_stale")
    global_units: dict[str, str] = {}
    for carrier in facts.carriers:
        _uuid(carrier.id)
        if carrier.state not in {"present", "departed", "unknown"}:
            _bad("carrier_state_invalid")
        if (
            type(carrier.warehouse_id) is not int
            or carrier.warehouse_id != facts.warehouse_id
            or type(carrier.location_id) is not int
            or carrier.location_id <= 0
            or type(carrier.content_revision) is not int
            or not 1 <= carrier.content_revision <= 2147483647
            or (
                carrier.confirmed_content_revision is not None
                and (
                    type(carrier.confirmed_content_revision) is not int
                    or not 1 <= carrier.confirmed_content_revision <= 2147483647
                )
            )
            or (
                carrier.packaging_id is not None
                and (type(carrier.packaging_id) is not int or carrier.packaging_id <= 0)
            )
        ):
            _bad("carrier_identity_or_revision_invalid")
        if carrier.packaging_profile_id is not None:
            _uuid(carrier.packaging_profile_id)
        for value in (carrier.content_digest, carrier.member_digest):
            if value is not None:
                _hex(value)
        vector = _members(carrier)
        for unit_id, _, _, _ in vector:
            previous = global_units.setdefault(unit_id, carrier.id)
            if previous != carrier.id:
                _bad("unit_owned_by_multiple_carriers")
        if carrier.state in {"present", "unknown"}:
            if carrier.gross_kg is not None:
                gross = _micro(carrier.gross_kg)
                if gross < 0:
                    _bad("negative_actual_gross")
            if carrier.tare_kg is not None:
                tare = _micro(carrier.tare_kg)
                if tare < 0:
                    _bad("negative_actual_tare")
            if carrier.gross_kg is not None and carrier.tare_kg is not None:
                if _micro(carrier.gross_kg) < _micro(carrier.tare_kg):
                    _bad("actual_carrier_mass_inconsistent")
    domain_units: dict[str, DomainUnit] = {}
    if current_family:
        domain_units, binding_error = _domain_bindings(facts)
        if binding_error is not None:
            return result("cannot_plan", (), ids, (binding_error,))
    own = existing_assignment
    if own is not None:
        candidate = carriers.get(own.carrier_id)
        if candidate is None:
            return result(
                "cannot_plan", (), ids, (Violation("existing_carrier_unknown", (own.task_id,)),)
            )
        issue = _assignment_error(facts, query, own, candidate)
        if issue:
            return result("cannot_plan", (), ids, (Violation(issue, (own.task_id,)),))
        if own not in facts.active_assignments:
            _bad("existing_assignment_not_active")
    claims = _pending(facts, own)
    targets = set(query.candidate_location_ids)
    occupied: list[tuple[int, str, Box, str]] = []
    positions = tuple(p for c in facts.carriers if c.state == "present" for p in c.positions)
    for carrier in facts.carriers:
        if carrier.state == "present" and carrier.location_id in targets and not carrier.positions:
            return result(
                "cannot_plan", (), ids, (Violation("actual_position_unknown", (carrier.id,)),)
            )
        if any(
            p.carrier_id != carrier.id or p.location_id != carrier.location_id
            for p in carrier.positions
        ):
            _bad("carrier_position_identity_mismatch")
    for position in positions:
        if position.location_id not in targets:
            continue
        profile = profiles.get(position.profile_id)
        loc = locations.get(position.location_id)
        current = _current_location_profile(facts, position.location_id)
        if (
            profile is None
            or loc is None
            or current is None
            or position.frame_id is None
            or position.frame_id != current.frame_id
            or profile.frame_id != current.frame_id
            or not _bound(profile, loc)
        ):
            return result(
                "cannot_plan",
                (),
                ids,
                (Violation("actual_coordinate_binding_unknown", (position.carrier_id,)),),
            )
        try:
            occupied.append(
                (
                    position.location_id,
                    position.frame_id,
                    _box(profile, position.slot_keys),
                    position.carrier_id,
                )
            )
        except PlacementInputError:
            return result(
                "cannot_plan",
                (),
                ids,
                (Violation("actual_footprint_ambiguous", (position.carrier_id,)),),
            )
    for claim in claims:
        if claim.location_id not in targets:
            continue
        profile = profiles.get(claim.profile_id)
        current = _current_location_profile(facts, claim.location_id)
        if (
            profile is None
            or current is None
            or claim.frame_id is None
            or claim.frame_id != current.frame_id
            or profile.frame_id != current.frame_id
        ):
            return result(
                "cannot_plan",
                (),
                ids,
                (Violation("pending_coordinate_binding_unknown", (claim.task_id,)),),
            )
        try:
            occupied.append(
                (claim.location_id, claim.frame_id, _box(profile, claim.slot_keys), claim.task_id)
            )
        except PlacementInputError:
            return result(
                "cannot_plan",
                (),
                ids,
                (Violation("pending_footprint_ambiguous", (claim.task_id,)),),
            )

    extra: dict[str, Decimal] = {}
    location_extra: dict[int, int] = {}
    support_extra: dict[str, int] = {}
    choices: list[Choice] = []
    remaining: list[str] = []
    violations: list[Violation] = []
    for carrier_id in ids:
        carrier = carriers.get(carrier_id)
        if carrier is None or carrier.warehouse_id != facts.warehouse_id:
            _bad("candidate_carrier_missing_or_scope")
        active_for_carrier = [a for a in facts.active_assignments if a.carrier_id == carrier_id]
        if active_for_carrier and (
            own is None
            or len(active_for_carrier) != 1
            or active_for_carrier[0].task_id != own.task_id
        ):
            remaining.append(carrier_id)
            violations.append(Violation("carrier_already_has_active_assignment", (carrier_id,)))
            continue
        package = profiles.get(carrier.packaging_profile_id or "")
        source = locations.get(carrier.location_id)
        current_package = _current_packaging_profile(facts, carrier.packaging_id)
        if current_package is not None:
            if package is None or current_package.native.id != package.native.id:
                remaining.append(carrier_id)
                violations.append(Violation("packaging_profile_not_current", (carrier_id,)))
                continue
        if (
            carrier.state != "present"
            or carrier.confirmed_content_revision != carrier.content_revision
            or carrier.content_revision <= 0
            or carrier.content_digest is None
            or carrier.member_digest is None
            or package is None
            or package.native.packaging_id != carrier.packaging_id
            or carrier.gross_kg is None
            or carrier.tare_kg is None
            or source is None
        ):
            remaining.append(carrier_id)
            violations.append(Violation("carrier_or_source_fact_unknown", (carrier_id,)))
            continue
        _hex(carrier.content_digest)
        _hex(carrier.member_digest)
        gross, tare = _d(carrier.gross_kg), _d(carrier.tare_kg)
        if gross < tare or (carrier.members and gross <= 0):
            _bad("carrier_mass_inconsistent")
        members = _members(carrier)
        if _sha(_json(members)) != carrier.member_digest:
            _bad("carrier_member_digest_mismatch")
        if (
            package.native.tare_kg is None
            or _d(package.native.tare_kg) != tare
            or package.native.gross_limit_kg is None
            or gross > _d(package.native.gross_limit_kg, True)
        ):
            remaining.append(carrier_id)
            violations.append(Violation("packaging_mass_unknown_or_exceeded", (carrier_id,)))
            continue
        package_access = _native_access_error(facts, policy, package)
        if package_access:
            remaining.append(carrier_id)
            violations.append(Violation(package_access, (carrier_id,)))
            continue
        if any(u.eligibility != "eligible" for u in carrier.members):
            remaining.append(carrier_id)
            violations.append(Violation("carrier_member_ineligible", (carrier_id,)))
            continue
        if own is None and any(u.reserved != 0 for u in carrier.members):
            remaining.append(carrier_id)
            violations.append(Violation("carrier_member_or_hold_ineligible", (carrier_id,)))
            continue
        if not carrier.positions or not source.active or source.active_count_id is not None:
            remaining.append(carrier_id)
            violations.append(Violation("source_position_or_count_lock_unknown", (carrier_id,)))
            continue
        source_profile = profiles.get(source.current_profile_id or "")
        source_current = _current_location_profile(facts, source.id)
        if (
            source_profile is None
            or source_current is None
            or source_current.native.id != source.current_profile_id
            or _native_access_error(facts, policy, source_profile)
        ):
            remaining.append(carrier_id)
            violations.append(Violation("source_profile_or_access_unknown", (carrier_id,)))
            continue
        if any(
            p.location_id != source.id
            or p.profile_id not in profiles
            or p.frame_id != profiles[p.profile_id].frame_id
            or not _bound(profiles[p.profile_id], source)
            for p in carrier.positions
        ):
            remaining.append(carrier_id)
            violations.append(Violation("source_coordinate_binding_unknown", (carrier_id,)))
            continue
        source_path = _path(source, facts.resources)
        candidates = [locations[x] for x in query.candidate_location_ids if x in locations]
        if own is not None and own.carrier_id == carrier_id:
            own_claim = own.claims[0]
            candidates = [loc for loc in candidates if loc.id == own_claim.location_id]
        route_order = {
            r.destination_id: i for i, r in enumerate(policy.routes) if r.source_id == source.id
        }
        candidates.sort(key=lambda x: (route_order.get(x.id, 10**9), x.code, x.id))
        chosen = None
        reason = "no_compatible_slot"
        for dest in candidates:
            if dest.id == source.id or not dest.active or dest.active_count_id is not None:
                continue
            route = next(
                (
                    r
                    for r in policy.routes
                    if r.source_id == source.id and r.destination_id == dest.id
                ),
                None,
            )
            dst = profiles.get(dest.current_profile_id or "")
            if (
                route is None
                or dst is None
                or _current_location_profile(facts, dest.id) != dst
                or not _bound(dst, dest)
                or not _bound(source_profile, source)
                or (route.source_profile_id, route.source_digest)
                != (source_profile.native.id, source_profile.digest)
                or (route.destination_profile_id, route.destination_digest)
                != (dst.native.id, dst.digest)
                or route.source_mapping_revision != source.mapping_revision
                or route.destination_mapping_revision != dest.mapping_revision
                or route.resource_ids != _path(dest, facts.resources)
                or not route.access_digest
                or not route.evidence.strip()
            ):
                reason = "route_or_profile_binding_unknown"
                continue
            if own is not None and own.carrier_id == carrier_id:
                claim = own.claims[0]
                if (
                    (
                        own.source_profile_id,
                        own.destination_profile_id,
                        own.packaging_profile_id,
                        own.phase_paths,
                    )
                    != (
                        route.source_profile_id,
                        route.destination_profile_id,
                        carrier.packaging_profile_id,
                        route.phase_resource_paths,
                    )
                    or claim.location_id != dest.id
                    or claim.profile_id != dst.native.id
                    or claim.frame_id != dst.frame_id
                    or claim.orientation not in ORIENTATIONS
                    or claim.slot_keys not in route.slot_groups
                ):
                    reason = "existing_assignment_route_or_claim_changed"
                    continue
            dst_path = _path(dest, facts.resources)
            phase_resources = {rid for phase in route.phase_resource_paths for rid in phase}
            valid_paths = {_path(loc, facts.resources) for loc in facts.locations}
            if (
                not route.phase_resource_paths
                or source_path not in route.phase_resource_paths
                or dst_path not in route.phase_resource_paths
                or any(path not in valid_paths for path in route.phase_resource_paths)
                or any(rid not in resources for rid in phase_resources)
            ):
                reason = "work_route_phase_binding_unknown"
                continue
            affected_resources = tuple(sorted(set(source_path) | phase_resources))
            if any(
                u.location_id == dest.id or set(u.resource_ids) & set(affected_resources)
                for u in facts.unknown_occupancies
            ):
                reason = "physical_footprint_unknown"
                continue
            access_error = _native_access_error(facts, policy, dst)
            if access_error:
                reason = access_error
                continue
            load_error, delta = _load(
                facts,
                policy,
                affected_resources,
                carrier,
                source_path,
                own if own and own.carrier_id == carrier_id else None,
                extra,
            )
            if load_error:
                reason = load_error
                continue
            max_gross, max_height = (
                (policy.manual_gross_limit_kg, policy.manual_height_limit_mm)
                if policy.access_method == "manual"
                else (policy.equipment_gross_limit_kg, policy.equipment_height_limit_mm)
            )
            if max_gross is None or max_height is None:
                reason = "work_access_limit_unknown"
                continue
            native_gross = dst.native.gross_limit_kg
            if native_gross is None:
                reason = "native_gross_limit_unknown"
                continue
            effective_gross = min(_d(max_gross, True), _d(native_gross, True))
            location_base = sum(
                _micro(row.gross_kg)
                for row in facts.carriers
                if row.state == "present"
                and row.location_id == dest.id
                and row.gross_kg is not None
            )
            location_pending = sum(
                _micro(a.gross_kg)
                for a in facts.active_assignments
                if a.destination_location_id == dest.id
            )
            own_already_charged = own is not None and own.carrier_id == carrier_id
            proposed_location = 0 if own_already_charged else _micro(gross)
            if location_base + location_pending + location_extra.get(
                dest.id, 0
            ) + proposed_location > _micro(native_gross, True):
                reason = "native_location_gross_limit_exceeded"
                continue
            if gross > _d(max_gross, True):
                reason = "work_access_limit_exceeded"
                continue
            for keys in sorted(route.slot_groups):
                if len(keys) > 1000:
                    reason = "slot_group_over_limit"
                    continue
                if (
                    own is not None
                    and own.carrier_id == carrier_id
                    and keys != own.claims[0].slot_keys
                ):
                    continue
                try:
                    slot = _box(dst, keys)
                except PlacementInputError:
                    reason = "destination_slot_group_invalid"
                    continue
                if any(loc == dest.id and frame != dst.frame_id for loc, frame, _, _ in occupied):
                    reason = "coordinate_binding_unknown"
                    continue
                if any(loc == dest.id and _overlap(slot, box) for loc, _, box, _ in occupied):
                    continue
                if gross > effective_gross or _micro(slot.y) + _micro(slot.h) > _micro(
                    max_height, True
                ):
                    reason = "work_or_native_access_limit_exceeded"
                    continue
                if any(x.frame_id != dst.frame_id for x in route.corridors):
                    reason = "access_coordinate_binding_unknown"
                    continue
                if current_family:
                    domain_error = _domain_access_error(slot, route, carrier, domain_units)
                    if domain_error:
                        reason = domain_error
                        continue
                elif any(
                    _overlap(slot, x.box)
                    and any(
                        u.fefo_rank is None or u.fefo_rank > x.older_fefo_rank
                        for u in carrier.members
                    )
                    for x in route.corridors
                ):
                    reason = "fefo_access_blocked"
                    continue
                orientations = (
                    (own.claims[0].orientation,)
                    if own is not None and own.carrier_id == carrier_id
                    else ORIENTATIONS
                )
                for orientation in orientations:
                    if not _fits(package.native, dst.native, route, orientation, slot):
                        continue
                    support, deps, support_error = _support(
                        facts,
                        dst,
                        slot,
                        gross,
                        positions,
                        claims,
                        carriers,
                        profiles,
                        support_extra,
                    )
                    if support_error:
                        reason = support_error
                        continue
                    if support:
                        chosen = (dest, dst, keys, orientation, deps, delta, support)
                        break
                if chosen:
                    break
            if chosen:
                break
        if not chosen:
            remaining.append(carrier_id)
            violations.append(Violation(reason, (carrier_id,)))
            continue
        dest, dst, keys, orientation, deps, delta, bearing_id = chosen
        proposal = _json(
            {
                "facts": facts.digest,
                "carrier": carrier_id,
                "destination": dest.id,
                "profile": dst.digest,
                "policy": policy.digest,
                "slots": list(keys),
                "orientation": orientation,
                "source_revision": carrier.content_revision,
                "source_digest": carrier.content_digest,
            }
        )
        choices.append(
            Choice(
                carrier_id,
                dest.id,
                orientation,
                keys,
                carrier.content_revision,
                carrier.content_digest,
                dst.native.id,
                policy.id,
                _sha(proposal),
                tuple(UnitQuantity(u.id, u.sku, u.lot, _d(u.qty)) for u in carrier.members),
                gross,
                tare,
                deps,
                ("approved_geometry", "current_load_and_access"),
            )
        )
        occupied.append((dest.id, dst.frame_id or "", _box(dst, keys), carrier_id))
        if own is None:
            location_extra[dest.id] = location_extra.get(dest.id, 0) + _micro(gross)
        if bearing_id in carriers:
            support_extra[bearing_id] = support_extra.get(bearing_id, 0) + _micro(gross)
        for resource_id, mass in delta.items():
            extra[resource_id] = extra.get(resource_id, ZERO) + mass
    if set(ids) != {x.carrier_id for x in choices} | set(remaining) or {
        x.carrier_id for x in choices
    } & set(remaining):
        _bad("result_partition_corrupt")
    if own is not None and not choices:
        return result(
            "cannot_plan",
            (),
            ids,
            tuple(violations) or (Violation("own_assignment_not_revalidated", (own.task_id,)),),
        )
    status = "proposed" if not remaining else ("partial" if choices else "cannot_plan")
    return result(status, tuple(choices), tuple(remaining), tuple(violations))


# ponytail: direct collision scans; consider a spatial index only if approved routes exceed 500 groups.
