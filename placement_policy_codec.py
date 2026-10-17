"""Internal pure decoder for a stored placement work-policy column mapping.

The binding JSON is opaque and untrusted. Its digest proves byte selection only;
this codec makes no approval, receipt, authority, provenance, or execution claim.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .physical_placement import (
    Box,
    Corridor,
    LoadRule,
    Opening,
    PlacementInputError,
    Policy,
    Route,
    _canonical_payload,
    _d,
    _hex,
    _json,
    _sha,
    _utc,
    _uuid,
)

_ROW_KEYS = {
    "id",
    "organization_id",
    "warehouse_id",
    "scope_code",
    "revision",
    "access_method",
    "valid_from",
    "valid_until",
    "policy",
    "server_binding",
    "canonical_payload",
    "digest",
    "source_ref",
    "source_revision",
    "evidence",
    "approved_by",
    "approved_at",
    "approval_operation_id",
}
_POLICY_KEYS = {
    "operator_subs",
    "routes",
    "equipment_resource_id",
    "equipment_profile_digest",
    "equipment_state",
    "manual_gross_limit_kg",
    "manual_height_limit_mm",
    "equipment_gross_limit_kg",
    "equipment_height_limit_mm",
    "load_rules",
}
_ROUTE_KEYS = {
    "source_id",
    "destination_id",
    "source_profile_id",
    "source_digest",
    "destination_profile_id",
    "destination_digest",
    "source_mapping_revision",
    "destination_mapping_revision",
    "resource_ids",
    "phase_resource_paths",
    "slot_groups",
    "openings",
    "corridors",
    "access_digest",
    "evidence",
}
_OPENING_KEYS = {
    "width_axis",
    "height_axis",
    "width_mm",
    "height_mm",
    "clearance_mm",
}
_CORRIDOR_KEYS = {"box", "frame_id", "older_fefo_rank"}
_BOX_KEYS = {"x", "y", "z", "w", "h", "d"}
_LOAD_RULE_KEYS = {
    "resource_id",
    "state",
    "gross_limit_kg",
    "source_profile_id",
    "source_profile_digest",
}


def _reject(code: str) -> None:
    raise PlacementInputError(f"policy_codec_{code}")


def _object(value: object, keys: set[str], field: str) -> dict[str, Any]:
    if type(value) is not dict or value.keys() != keys:
        _reject(f"{field}_shape")
    return value


def _array(value: object, field: str) -> list[Any]:
    if type(value) is not list:
        _reject(f"{field}_shape")
    return value


def _text(value: object, field: str, maximum: int | None = None) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or (maximum is not None and len(value) > maximum)
    ):
        _reject(f"{field}_text")
    return value


def _int32(value: object, field: str) -> int:
    if type(value) is not int or not 1 <= value <= 2147483647:
        _reject(f"{field}_integer")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        _reject(f"{field}_integer")
    return value


def _decimal(
    value: object,
    field: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if type(value) is not str or not value or len(value) > 22:
        _reject(f"{field}_decimal")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        _reject(f"{field}_decimal")
    normalized = _d(parsed, positive)
    if format(normalized, ".6f") != value:
        _reject(f"{field}_decimal_canonical")
    if nonnegative and normalized < 0:
        _reject(f"{field}_decimal_range")
    return normalized


def _datetime(value: object, field: str) -> datetime:
    if type(value) is not datetime:
        _reject(f"{field}_datetime")
    return _utc(value)


def _unique(values: tuple[Any, ...], field: str) -> tuple[Any, ...]:
    if len(values) != len(set(values)):
        _reject(f"{field}_duplicate")
    return values


def _text_vector(
    value: object, field: str, *, maximum: int | None = None
) -> tuple[str, ...]:
    rows = tuple(_text(item, field, maximum) for item in _array(value, field))
    return _unique(rows, field)


def _uuid_vector(value: object, field: str) -> tuple[str, ...]:
    rows = tuple(_uuid(item) for item in _array(value, field))
    return _unique(rows, field)


def _json_tree(value: object, field: str) -> None:
    """Accept only values that the JSON column can round-trip without float loss."""
    if value is None or type(value) in (str, int, bool):
        return
    if type(value) is list:
        for item in value:
            _json_tree(item, field)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                _reject(f"{field}_key")
            _json_tree(item, field)
        return
    _reject(f"{field}_value")


def _box(value: object) -> Box:
    row = _object(value, _BOX_KEYS, "box")
    return Box(
        x=_decimal(row["x"], "box_x"),
        y=_decimal(row["y"], "box_y"),
        z=_decimal(row["z"], "box_z"),
        w=_decimal(row["w"], "box_w", positive=True),
        h=_decimal(row["h"], "box_h", positive=True),
        d=_decimal(row["d"], "box_d", positive=True),
    )


def _opening(value: object) -> Opening:
    row = _object(value, _OPENING_KEYS, "opening")
    width_axis = row["width_axis"]
    height_axis = row["height_axis"]
    if type(width_axis) is not str or width_axis not in {"x", "y", "z"}:
        _reject("opening_width_axis")
    if type(height_axis) is not str or height_axis not in {"x", "y", "z"}:
        _reject("opening_height_axis")
    clearance = (
        None
        if row["clearance_mm"] is None
        else _decimal(row["clearance_mm"], "opening_clearance", nonnegative=True)
    )
    return Opening(
        width_axis=width_axis,
        height_axis=height_axis,
        width_mm=_decimal(row["width_mm"], "opening_width", positive=True),
        height_mm=_decimal(row["height_mm"], "opening_height", positive=True),
        clearance_mm=clearance,
    )


def _corridor(value: object) -> Corridor:
    row = _object(value, _CORRIDOR_KEYS, "corridor")
    return Corridor(
        box=_box(row["box"]),
        frame_id=(
            None if row["frame_id"] is None else _text(row["frame_id"], "corridor_frame")
        ),
        older_fefo_rank=_nonnegative_int(row["older_fefo_rank"], "corridor_fefo_rank"),
    )


def _route(value: object) -> Route:
    row = _object(value, _ROUTE_KEYS, "route")
    source_id = _int32(row["source_id"], "route_source_id")
    destination_id = _int32(row["destination_id"], "route_destination_id")
    if source_id == destination_id:
        _reject("route_same_location")
    paths = tuple(
        _uuid_vector(path, "route_phase_resource_path")
        for path in _array(row["phase_resource_paths"], "route_phase_paths")
    )
    _unique(paths, "route_phase_paths")
    slot_groups = tuple(
        _text_vector(group, "route_slot_group", maximum=64)
        for group in _array(row["slot_groups"], "route_slot_groups")
    )
    _unique(slot_groups, "route_slot_groups")
    return Route(
        source_id=source_id,
        destination_id=destination_id,
        source_profile_id=_uuid(row["source_profile_id"]),
        source_digest=_hex(row["source_digest"]),
        destination_profile_id=_uuid(row["destination_profile_id"]),
        destination_digest=_hex(row["destination_digest"]),
        source_mapping_revision=_int32(
            row["source_mapping_revision"], "route_source_mapping_revision"
        ),
        destination_mapping_revision=_int32(
            row["destination_mapping_revision"], "route_destination_mapping_revision"
        ),
        resource_ids=_uuid_vector(row["resource_ids"], "route_resource_ids"),
        phase_resource_paths=paths,
        slot_groups=slot_groups,
        openings=tuple(_opening(item) for item in _array(row["openings"], "openings")),
        corridors=tuple(_corridor(item) for item in _array(row["corridors"], "corridors")),
        access_digest=_hex(row["access_digest"]),
        evidence=_text(row["evidence"], "route_evidence"),
    )


def _load_rule(value: object) -> LoadRule:
    row = _object(value, _LOAD_RULE_KEYS, "load_rule")
    state = row["state"]
    if type(state) is not str or state not in {
        "required",
        "confirmed_non_load_bearing",
        "unknown",
    }:
        _reject("load_rule_state")
    gross_limit = (
        None
        if row["gross_limit_kg"] is None
        else _decimal(row["gross_limit_kg"], "load_rule_gross_limit", positive=True)
    )
    profile_id = (
        None
        if row["source_profile_id"] is None
        else _uuid(row["source_profile_id"])
    )
    profile_digest = (
        None
        if row["source_profile_digest"] is None
        else _hex(row["source_profile_digest"])
    )
    return LoadRule(
        resource_id=_uuid(row["resource_id"]),
        state=state,
        gross_limit_kg=gross_limit,
        source_profile_id=profile_id,
        source_profile_digest=profile_digest,
    )


def decode_placement_work_policy(row: object) -> Policy:
    """Decode one exact PlacementWorkPolicy column mapping into the pure Policy type.

    This checks shape, canonical encodings, row/body agreement, and digest
    consistency. It does not establish that the binding is true, that an approval
    receipt exists or is trusted, or that this row is authorized for use.
    """
    stored = _object(row, _ROW_KEYS, "row")
    body = _object(stored["policy"], _POLICY_KEYS, "policy")
    binding = stored["server_binding"]
    if type(binding) is not dict:
        _reject("server_binding_shape")
    _json_tree(binding, "server_binding")
    binding_digest = _sha(_json(binding))

    access_method = stored["access_method"]
    if type(access_method) is not str or access_method not in {"manual", "equipment"}:
        _reject("access_method")
    equipment_state = body["equipment_state"]
    if type(equipment_state) is not str or equipment_state not in {
        "available",
        "unavailable",
        "unknown",
    }:
        _reject("equipment_state")

    operator_subs = _text_vector(body["operator_subs"], "operator_sub")
    routes = tuple(_route(item) for item in _array(body["routes"], "routes"))
    _unique(tuple((route.source_id, route.destination_id) for route in routes), "routes")
    load_rules = tuple(
        _load_rule(item) for item in _array(body["load_rules"], "load_rules")
    )
    _unique(tuple(rule.resource_id for rule in load_rules), "load_rules")

    equipment_resource_id = (
        None
        if body["equipment_resource_id"] is None
        else _uuid(body["equipment_resource_id"])
    )
    equipment_profile_digest = (
        None
        if body["equipment_profile_digest"] is None
        else _hex(body["equipment_profile_digest"])
    )
    limits = {
        name: (
            None
            if body[name] is None
            else _decimal(body[name], name, positive=True)
        )
        for name in (
            "manual_gross_limit_kg",
            "manual_height_limit_mm",
            "equipment_gross_limit_kg",
            "equipment_height_limit_mm",
        )
    }

    valid_from = _datetime(stored["valid_from"], "valid_from")
    valid_until = _datetime(stored["valid_until"], "valid_until")
    if valid_until <= valid_from:
        _reject("validity_interval")

    canonical_payload = stored["canonical_payload"]
    if type(canonical_payload) is not str or not canonical_payload:
        _reject("canonical_payload")
    digest = _hex(stored["digest"])
    policy = Policy(
        id=_uuid(stored["id"]),
        organization_id=_int32(stored["organization_id"], "organization_id"),
        warehouse_id=_int32(stored["warehouse_id"], "warehouse_id"),
        scope_code=_text(stored["scope_code"], "scope_code", 64),
        revision=_int32(stored["revision"], "revision"),
        access_method=access_method,
        operator_subs=operator_subs,
        routes=routes,
        equipment_resource_id=equipment_resource_id,
        equipment_profile_digest=equipment_profile_digest,
        equipment_state=equipment_state,
        manual_gross_limit_kg=limits["manual_gross_limit_kg"],
        manual_height_limit_mm=limits["manual_height_limit_mm"],
        equipment_gross_limit_kg=limits["equipment_gross_limit_kg"],
        equipment_height_limit_mm=limits["equipment_height_limit_mm"],
        load_rules=load_rules,
        valid_from=valid_from,
        valid_until=valid_until,
        source_ref=_text(stored["source_ref"], "source_ref", 1000),
        source_revision=_text(stored["source_revision"], "source_revision", 200),
        evidence=_text(stored["evidence"], "evidence", 1000),
        approved_by=_text(stored["approved_by"], "approved_by", 200),
        approved_at=_datetime(stored["approved_at"], "approved_at"),
        approval_operation_id=_uuid(stored["approval_operation_id"]),
        server_binding_digest=binding_digest,
        canonical_payload=canonical_payload,
        digest=digest,
    )
    if _canonical_payload(policy) != canonical_payload:
        _reject("canonical_payload_mismatch")
    if _sha(canonical_payload) != digest:
        _reject("digest_mismatch")
    return policy

# Closed measured-context decoder. Server-native vectors are deliberately unbounded.
def _measured_time(value, field):
    from modules.wms.physical_schemas import equipment_date
    parsed = equipment_date(value)
    from datetime import timezone
    if parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z") != value:
        _reject(f"{field}_not_canonical")
    return parsed


def _measured_native_frame(frame, warehouse_id):
    from modules.wms.physical_models import (
        STOCK_UNIT_MEASUREMENT_V1_COLUMNS,
        CarrierPosition,
        PhysicalCarrier,
    )
    from modules.wms.physical_stock import _origin_dimension_snapshot_valid

    _object(frame, {"carrier", "members", "positions", "location_units"}, "measurement_observation")
    carrier = _object(frame["carrier"], {c.name for c in PhysicalCarrier.__table__.columns}, "measurement_carrier")
    _uuid(carrier["id"])
    for key in ("warehouse_id", "location_id", "content_revision"):
        _int32(carrier[key], key)
    if carrier["warehouse_id"] != warehouse_id or carrier["state"] != "present":
        _reject("measurement_carrier_scope_state")
    if (carrier["packaging_id"] is None) != (carrier["packaging_profile_id"] is None):
        _reject("measurement_carrier_packaging_pair")
    if carrier["packaging_id"] is not None:
        _int32(carrier["packaging_id"], "packaging_id")
        _uuid(carrier["packaging_profile_id"])
    if carrier["confirmed_content_revision"] is not None:
        _int32(carrier["confirmed_content_revision"], "confirmed_content_revision")
        if carrier["confirmed_content_revision"] != carrier["content_revision"]:
            _reject("measurement_carrier_confirmed_revision")
    for key in ("gross_kg", "tare_kg"):
        if carrier[key] is not None:
            _decimal(carrier[key], key, nonnegative=True)
    if carrier["gross_kg"] is not None and carrier["tare_kg"] is not None and Decimal(carrier["gross_kg"]) < Decimal(carrier["tare_kg"]):
        _reject("measurement_carrier_mass")
    _text(carrier["measurement_evidence"], "measurement_evidence", 1000)
    _text(carrier["observed_by"], "observed_by", 200)
    _measured_time(carrier["observed_at"], "observed_at")
    for name in ("members", "location_units"):
        rows = _array(frame[name], name)
        ids = []
        for row in rows:
            _object(row, set(STOCK_UNIT_MEASUREMENT_V1_COLUMNS), "measurement_unit")
            ids.append(_uuid(row["id"]))
            if row["assembly_output_id"] is not None:
                _uuid(row["assembly_output_id"])
            if row["carrier_id"] is not None:
                _uuid(row["carrier_id"])
            qty = _decimal(row["qty"], "qty", nonnegative=True)
            reserved = _decimal(row["reserved"], "reserved", nonnegative=True)
            for key in ("warehouse_id", "location_id", "batch_id"):
                _int32(row[key], key)
            if row["packaging_id"] is not None:
                _int32(row["packaging_id"], "unit_packaging_id")
            if reserved > qty or row["warehouse_id"] != warehouse_id:
                _reject("measurement_unit_quantity_scope")
            if row["assembly_output_id"] is not None and (qty not in {Decimal(0),Decimal(1)} or reserved not in {Decimal(0),Decimal(1)}):
                _reject("measurement_assembly_whole_identity")
            dimensions = {key: row[key] for key in (
                "warehouse_id", "location_id", "sku_code", "batch_id", "characteristic", "purpose", "quality",
                "packaging_id", "package_state", "container", "last_checked", "next_service", "inspection_ref",
                "service_result", "arrived_at", "arrival_source")}
            if row["arrived_at"] is None:
                _reject("measurement_unit_arrival_missing")
            if not _origin_dimension_snapshot_valid(dimensions):
                _reject("measurement_unit_dimensions")
            _uuid_vector(row["external_operations"], "external_operations")
            if name == "members" and (row["carrier_id"] != carrier["id"] or row["location_id"] != carrier["location_id"]):
                _reject("measurement_member_scope")
            if name == "location_units" and row["location_id"] != carrier["location_id"]:
                _reject("measurement_location_unit_scope")
        if ids != sorted(set(ids)):
            _reject("measurement_unit_vector_order_or_duplicate")
    location_rows = {row["id"]: row for row in frame["location_units"]}
    if any(location_rows.get(row["id"]) != row for row in frame["members"]):
        _reject("measurement_member_location_correspondence")
    if [row for row in frame["location_units"] if row["carrier_id"] == carrier["id"]] != frame["members"]:
        _reject("measurement_member_vector_incomplete")
    positions = _array(frame["positions"], "positions")
    for index, row in enumerate(positions):
        _object(row, {c.name for c in CarrierPosition.__table__.columns}, "measurement_position")
        _int32(row["warehouse_id"], "position_warehouse")
        _int32(row["location_id"], "position_location")
        if type(row["ordinal"]) is not int or row["ordinal"] != index:
            _reject("measurement_position_ordinal")
        if row["carrier_id"] != carrier["id"] or row["warehouse_id"] != warehouse_id or row["location_id"] != carrier["location_id"]:
            _reject("measurement_position_scope")
        _uuid(row["location_profile_id"])
        _text(row["slot_key"], "slot_key", 64)
    _unique(tuple((row["location_profile_id"], row["slot_key"]) for row in positions), "measurement_positions")
    return frame


def measured_context_proof(value):
    """Validate immutable closed bytes. No database/current-state authorization here."""
    from modules.wms.physical_models import (
        PlacementProfile, _CAP_PROFILE_OMIT, _cap_signed_payload, _cap_validate_chain_shape,
    )
    from modules.wms.physical_schemas import (
        CapacityMeasurementsApprove, normalize_measurements_request,
    )
    from modules.wms.physical_stock import canonical, digest

    _json_tree(value, "measurement_proof")
    keys = {"protocol", "organization_id", "warehouse_id", "operation_id", "kind", "revision", "previous_operation_id",
        "request_hash", "request", "approver", "resolved_frames", "profile_bindings", "topology", "carrier_observations",
        "canonical_payload", "digest"}
    _object(value, keys, "measurement_proof")
    if type(value["protocol"]) is not int or value["protocol"] != 1 or value["kind"] != "capacity_measurements_approve":
        _reject("measurement_protocol_kind")
    org, wh = _int32(value["organization_id"], "organization_id"), _int32(value["warehouse_id"], "warehouse_id")
    _uuid(value["operation_id"])
    _int32(value["revision"], "revision")
    if value["previous_operation_id"] is not None:
        _uuid(value["previous_operation_id"])
    _hex(value["request_hash"])
    request = value["request"]
    parsed = CapacityMeasurementsApprove.model_validate(request)
    if canonical(normalize_measurements_request(parsed)) != canonical(request):
        _reject("measurement_request_not_normalized")
    if (request["kind"] != value["kind"] or request["operation_id"] != value["operation_id"] or request["warehouse_id"] != wh
        or request["expected_revision"] + 1 != value["revision"] or request["previous_operation_id"] != value["previous_operation_id"]):
        _reject("measurement_request_proof_correspondence")
    approver = _object(value["approver"], {"user_id", "sub", "issued_at", "roles", "approved_at"}, "measurement_approver")
    _int32(approver["user_id"], "approver_user_id")
    _text(approver["sub"], "approver_sub", 200)
    if approver["issued_at"] is not None:
        _nonnegative_int(approver["issued_at"], "issued_at")
    _text_vector(approver["roles"], "approver_roles", maximum=1000)
    _measured_time(approver["approved_at"], "approved_at")
    if value["request_hash"] != digest({"organization_id": org, "actor_user_id": approver["user_id"], "actor_sub": approver["sub"], "request": request}):
        _reject("measurement_request_hash")
    frames = _array(value["resolved_frames"], "resolved_frames")
    if len(frames) != len(request["frames"]):
        _reject("measurement_resolved_frame_count")
    for row, declaration in zip(frames, request["frames"]):
        _object(row, {"frame_key", "frame_id", "anchor_location_id", "datum_mark", "axes", "source", "resolution"}, "measurement_resolved_frame")
        _uuid(row["frame_id"])
        _int32(row["anchor_location_id"], "frame_anchor")
        if any(canonical(row[key]) != canonical(declaration[key]) for key in ("frame_key", "anchor_location_id", "datum_mark", "axes", "source")):
            _reject("measurement_frame_declaration_correspondence")
        resolution = row["resolution"]
        if declaration["kind"] == "new":
            _object(resolution, {"kind"}, "measurement_new_resolution")
            if resolution["kind"] != "new":
                _reject("measurement_new_resolution_kind")
        else:
            _object(resolution, {"kind", "approval_operation_id", "approval_digest", "frame_id"}, "measurement_reuse_resolution")
            _hex(resolution["approval_digest"])
            if (resolution["kind"] != "reuse" or resolution["approval_operation_id"] != declaration["approval_operation_id"]
                or resolution["frame_id"] != declaration["frame_id"] or row["frame_id"] != declaration["frame_id"]):
                _reject("measurement_reuse_resolution_correspondence")
    _unique(tuple(row["frame_key"] for row in frames), "measurement_frame_keys")
    frame_ids = {}
    for row in frames:
        signature = (row["anchor_location_id"], row["datum_mark"], row["axes"])
        if row["frame_id"] in frame_ids and frame_ids[row["frame_id"]] != signature:
            _reject("measurement_conflicting_frame_identity")
        frame_ids[row["frame_id"]] = signature
    frame_by_key = {row["frame_key"]: row for row in frames}
    bindings = _array(value["profile_bindings"], "profile_bindings")
    if len(bindings) != len(request["profile_frames"]):
        _reject("measurement_profile_binding_count")
    for binding, declaration in zip(bindings, request["profile_frames"]):
        _object(binding, {"profile_id", "revision", "digest", "sealed", "canonical_payload", "snapshot", "frame_key", "frame_id"}, "measurement_profile_binding")
        _uuid(binding["profile_id"])
        _uuid(binding["frame_id"])
        _int32(binding["revision"], "profile_binding_revision")
        _hex(binding["digest"])
        if binding["sealed"] is not True or binding["profile_id"] != declaration["profile_id"] or binding["digest"] != declaration["expected_digest"] or binding["frame_key"] != declaration["frame_key"]:
            _reject("measurement_profile_binding_correspondence")
        if binding["frame_key"] not in frame_by_key or binding["frame_id"] != frame_by_key[binding["frame_key"]]["frame_id"]:
            _reject("measurement_profile_frame_missing")
        snapshot = _object(binding["snapshot"], {"profile", "slots"}, "measurement_profile_snapshot")
        signed = _object(snapshot["profile"], {c.name for c in PlacementProfile.__table__.columns if c.name not in _CAP_PROFILE_OMIT}, "measurement_native_profile")
        _uuid(signed["id"])
        for key in ("organization_id", "warehouse_id", "revision", "location_id", "bound_mapping_revision"):
            _int32(signed[key], key)
        if (signed["id"] != binding["profile_id"] or signed["revision"] != binding["revision"] or signed["organization_id"] != org
            or signed["warehouse_id"] != wh or signed["packaging_id"] is not None or signed["resource_id"] is not None):
            _reject("measurement_profile_scope_target")
        _cap_validate_chain_shape(signed["ancestor_binding"])
        from modules.wms.physical_schemas import ProfileApprove
        ProfileApprove.model_validate({"protocol":2,"kind":"capacity_profile_approve","operation_id":value["operation_id"],
            "warehouse_id":wh,"profile_id":signed["id"],"expected_revision":signed["revision"]-1,"expected_mapping_revision":signed["bound_mapping_revision"],
            **{key:signed[key] for key in ("packaging_id","location_id","resource_id","length_mm","width_mm","height_mm",
                "opening_width_mm","opening_height_mm","clearance_mm","gross_limit_kg","tare_kg","top_load_limit_kg",
                "max_layers","orientations","load_type","support_rule","access_method","equipment_ref","restrictions",
                "source_ref","source_revision","evidence")},
            "slots":[{key:row[key] for key in ("slot_key","x_mm","y_mm","z_mm","width_mm","height_mm","depth_mm")} for row in snapshot["slots"]]})
        _text(signed["approved_by"], "profile_approved_by", 200)
        native = {**signed, "sealed": True, "canonical_payload": binding["canonical_payload"], "digest": binding["digest"]}
        native["approved_at"] = _measured_time(signed["approved_at"], "native_approved_at")
        if canonical(_cap_signed_payload(native)) != canonical(snapshot):
            _reject("measurement_native_profile_snapshot")
    topology = _object(value["topology"], {"resources", "locations"}, "measurement_topology")
    resources = _array(topology["resources"], "measurement_resources")
    for row in resources:
        _object(row, {"id", "warehouse_id", "parent_id", "kind", "topology_revision", "source_ref", "source_revision", "evidence", "approved_by", "approved_at"}, "measurement_resource")
        _uuid(row["id"])
        if row["parent_id"] is not None:
            _uuid(row["parent_id"])
        _int32(row["topology_revision"], "topology_revision")
        _int32(row["warehouse_id"], "resource_warehouse")
        if row["warehouse_id"] != wh or row["kind"] not in {"warehouse", "storage_area", "floor", "rack", "shelf", "cell", "staging", "equipment"} or (row["kind"] == "warehouse") != (row["parent_id"] is None):
            _reject("measurement_resource_scope_kind")
        for key, maximum in (("source_ref",1000),("source_revision",200),("evidence",1000),("approved_by",200)):
            _text(row[key], key, maximum)
        _measured_time(row["approved_at"], "resource_approved_at")
    if [r["id"] for r in resources] != sorted({r["id"] for r in resources}):
        _reject("measurement_resource_order_duplicate")
    resource_by_id = {r["id"]: r for r in resources}
    locations = _array(topology["locations"], "measurement_locations")
    for row in locations:
        _object(row, {"warehouse_id", "location_id", "resource_id", "resource_mapping_revision", "ancestor_binding"}, "measurement_location_binding")
        _int32(row["location_id"], "location_id")
        _int32(row["warehouse_id"], "location_warehouse")
        _int32(row["resource_mapping_revision"], "resource_mapping_revision")
        _uuid(row["resource_id"])
        if row["warehouse_id"] != wh:
            _reject("measurement_location_scope")
        _cap_validate_chain_shape(row["ancestor_binding"])
        if row["ancestor_binding"][0]["resource_id"] != row["resource_id"]:
            _reject("measurement_location_chain_start")
        for item in row["ancestor_binding"]:
            actual = resource_by_id.get(item["resource_id"])
            if actual is None or any(item[k] != actual[{"resource_id":"id"}.get(k,k)] for k in item):
                _reject("measurement_topology_chain_correspondence")
    if [r["location_id"] for r in locations] != sorted({r["location_id"] for r in locations}):
        _reject("measurement_location_order_duplicate")
    location_ids = {r["location_id"] for r in locations}
    if any(frame["anchor_location_id"] not in location_ids for frame in frames):
        _reject("measurement_anchor_missing")
    observations = _array(value["carrier_observations"], "measurement_carrier_observations")
    for row in observations:
        _object(row, {"carrier_id", "observation_operation_id", "observation_request_hash", "content_revision", "content_digest", "after"}, "measurement_carrier_observation")
        _uuid(row["carrier_id"])
        _uuid(row["observation_operation_id"])
        _hex(row["observation_request_hash"])
        _hex(row["content_digest"])
        _int32(row["content_revision"], "content_revision")
        _measured_native_frame(row["after"], wh)
        if row["after"]["carrier"]["id"] != row["carrier_id"] or row["after"]["carrier"]["content_revision"] != row["content_revision"] or digest(row["after"]) != row["content_digest"]:
            _reject("measurement_observation_correspondence")
    if [r["carrier_id"] for r in observations] != sorted({r["carrier_id"] for r in observations}):
        _reject("measurement_observation_order_duplicate")
    body = {k:v for k,v in value.items() if k not in {"canonical_payload", "digest"}}
    if value["canonical_payload"] != canonical(body) or value["digest"] != digest(body):
        _reject("measurement_proof_canonical_digest")
    # Reuse the Source wire decoder, never accept a second opaque source schema.
    return value
