"""Trusted placement collection and compilation in a route-owned transaction.

No client Facts are accepted. The current snapshot is separate from immutable
measurement and WorkPolicy history. Helpers never begin/commit a transaction.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, fields, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import TypeAdapter
from sqlalchemy import select

from core.domain.models import Sku
from modules.wms.models import Location as LocationRow
from modules.wms.physical_capacity import (
    _capacity_transit_origin, _observation_frame_values, _observation_row, capacity_metadata_rows,
    measurement_current_binding, measurement_receipt,
)
from modules.wms.physical_models import (
    STOCK_UNIT_MEASUREMENT_V1_COLUMNS, CarrierPosition, CountLocationLock, PhysicalCarrier, PhysicalCount, PhysicalLocation,
    PhysicalCountLine, PhysicalHold, PhysicalOperation, PhysicalTask, Packaging,
    PickedAllocation, PlacementProfile, StockUnit, StoragePolicy, _cap_chain,
    _cap_current_binding, _cap_full_snapshot, require_current_on_hand,
)
from modules.wms.physical_placement import (
    ACTIVE, Box, Carrier, CorridorDomain, DomainCorridor, DomainFacts, DomainUnit,
    Location, NativeProfile, Policy, Position, Profile, Query, Resource,
    Support, UnknownOccupancy, _canonical_payload, _json, _members, _sha, _utc,
    evaluate_current,
)
from modules.wms.physical_stock import (
    canonical, control, decimal_text, digest, eligible, physical_unit_frontier, reject, today,
)
from modules.wms.placement_models import (
    PlacementAssignment, PlacementClaim, PlacementWorkPolicy,
)
from modules.wms.placement_policy_codec import measured_context_proof
from modules.wms.placement_schemas import PlacementCommand, normalize_placement_request

_PLACEMENT_COMMAND_ADAPTER = TypeAdapter(PlacementCommand)


@dataclass(frozen=True, slots=True)
class PlacementSnapshot:
    """Deep-owned full facts; only immutable bytes cross the loader boundary."""

    canonical_payload: str
    digest: str

    def data(self) -> dict:
        # Each caller gets its own tree; mutating it cannot mutate this snapshot.
        return json.loads(self.canonical_payload)


def freeze_snapshot(value: dict) -> PlacementSnapshot:
    text = canonical(value)
    return PlacementSnapshot(text, digest(value))


def _decision_day(now_utc: datetime) -> date:
    return _utc(now_utc).astimezone(ZoneInfo("Europe/Minsk")).date()


def _check_day(day: date) -> None:
    if today() != day:
        reject("placement_decision_day_changed")


async def _locked_rows(session, statement):
    return list((await session.scalars(statement.with_for_update()
                .execution_options(populate_existing=True))).all())


async def collect_placement_snapshot(session, stock, organization_id, warehouse_id,
                                     actor, now_utc: datetime) -> PlacementSnapshot:
    """Exact full warehouse collector. Organization authority is acquired first."""
    if not session.in_transaction() or session.new or session.dirty or session.deleted:
        reject("placement_clean_route_transaction_required")
    day = _decision_day(now_utc)
    _check_day(day)
    with session.no_autoflush:
        from modules.wms.physical_stock import _require_physical_causal_snapshot
        await _require_physical_causal_snapshot(session)
        await stock._fresh_capacity_authority(session, organization_id, actor,
                                               ("wms.operate",))
        warehouse = await control(session, warehouse_id, organization_id)
        await stock._fresh_capacity_authority(session, organization_id, actor,
                                               ("wms.operate",))
        if warehouse.mode != "erp":
            reject("placement_warehouse_not_erp")
        resources, locations, profiles, slots = await capacity_metadata_rows(session, warehouse_id)
        packaging_ids = select(PhysicalCarrier.packaging_id).where(PhysicalCarrier.warehouse_id == warehouse_id)
        packaging = await _locked_rows(session, select(Packaging).where(Packaging.organization_id == organization_id,
            Packaging.id.in_(packaging_ids) | Packaging.id.in_([row.packaging_id for row in profiles if row.packaging_id is not None])).order_by(Packaging.id))
        native_locations = await _locked_rows(session, select(LocationRow).where(
            LocationRow.id.in_([row.location_id for row in locations])).order_by(LocationRow.id))
        carriers = await _locked_rows(session, select(PhysicalCarrier).where(
            PhysicalCarrier.warehouse_id == warehouse_id).order_by(PhysicalCarrier.id))
        positions = await _locked_rows(session, select(CarrierPosition).where(
            CarrierPosition.warehouse_id == warehouse_id).order_by(CarrierPosition.carrier_id, CarrierPosition.ordinal))
        counts = await _locked_rows(session, select(PhysicalCount).where(
            PhysicalCount.warehouse_id == warehouse_id).order_by(PhysicalCount.id))
        count_locks = await _locked_rows(session, select(CountLocationLock).join(
            PhysicalLocation, CountLocationLock.location_id == PhysicalLocation.location_id
        ).where(PhysicalLocation.warehouse_id == warehouse_id).order_by(CountLocationLock.location_id))
        count_lines = await _locked_rows(session, select(PhysicalCountLine).join(
            PhysicalCount, PhysicalCount.id == PhysicalCountLine.count_id).where(
            PhysicalCount.warehouse_id == warehouse_id).order_by(PhysicalCountLine.count_id, PhysicalCountLine.id))
        tasks = await _locked_rows(session, select(PhysicalTask).where(
            PhysicalTask.warehouse_id == warehouse_id).order_by(PhysicalTask.id))
        assignments = await _locked_rows(session, select(PlacementAssignment).where(
            PlacementAssignment.organization_id == organization_id,
            PlacementAssignment.warehouse_id == warehouse_id).order_by(PlacementAssignment.task_id))
        claims = await _locked_rows(session, select(PlacementClaim).where(
            PlacementClaim.warehouse_id == warehouse_id).order_by(PlacementClaim.task_id, PlacementClaim.ordinal))
        holds = await _locked_rows(session, select(PhysicalHold).where(
            PhysicalHold.warehouse_id == warehouse_id).order_by(PhysicalHold.id))
        picks = await _locked_rows(session, select(PickedAllocation).join(
            PhysicalHold, PhysicalHold.id == PickedAllocation.hold_id).where(
            PhysicalHold.warehouse_id == warehouse_id).order_by(PickedAllocation.id))
        work = await _locked_rows(session, select(PlacementWorkPolicy).where(
            PlacementWorkPolicy.organization_id == organization_id,
            PlacementWorkPolicy.warehouse_id == warehouse_id).order_by(PlacementWorkPolicy.id))
        storage = await _locked_rows(session, select(StoragePolicy).where(
            StoragePolicy.warehouse_id == warehouse_id).order_by(StoragePolicy.sku_code))
        # All units are last, with no qty/carrier filter, truncation or late task lock.
        units = await _locked_rows(session, select(StockUnit).where(
            StockUnit.warehouse_id == warehouse_id).order_by(StockUnit.id))
        skus = list((await session.scalars(select(Sku).where(Sku.code.in_(
            sorted({row.sku_code for row in units}))).order_by(Sku.code)
            .execution_options(populate_existing=True))).all())
        connection = await session.connection()
        profile_values = [_observation_row(row) for row in profiles]
        native_profile_values = [{column.name: getattr(row, column.name)
                                  for column in PlacementProfile.__table__.columns} for row in profiles]
        chains = {}
        for row in locations:
            if row.resource_id is not None:
                chains[str(row.location_id)] = await connection.run_sync(
                    lambda conn, row=row: _cap_chain(conn, warehouse_id, row.resource_id))
        current_profiles = {}
        for row in native_profile_values:
            target = (row["packaging_id"], row["location_id"], row["resource_id"])
            previous = current_profiles.get(target)
            if previous is None or row["revision"] > previous["revision"]:
                current_profiles[target] = row
            elif row["revision"] == previous["revision"]:
                reject("placement_profile_revision_duplicate")
        for row in current_profiles.values():
            if row["sealed"] is not True:
                reject("placement_current_profile_unsealed")
            await connection.run_sync(lambda conn, row=row: _cap_full_snapshot(conn, row))
            await connection.run_sync(lambda conn, row=row: _cap_current_binding(conn, row))
        from modules.wms.item_identity import (
            _OriginUnproven, _origin_frontier_tokens, _prove_unit_boundary,
        )
        unit_evidence = []
        present_units = []
        for unit in units:
            _check_day(day)
            origin, frontier, records, reason, lot = None, None, [], None, None
            if unit.stock_state == "in_transit":
                origin, frontier, records = await _capacity_transit_origin(session, organization_id, unit)
                reason = "placement_in_transit"
            else:
                require_current_on_hand(unit)
                present_units.append(unit)
                try:
                    reason, lot = await eligible(session, stock.reference, warehouse, unit, 0)
                    frontier, records = await physical_unit_frontier(session, organization_id, unit.id)
                    origin = await _prove_unit_boundary(session, organization_id, unit.id,
                        _origin_frontier_tokens(frontier), current_unit=unit, allow_zero=unit.qty == 0)
                except _OriginUnproven:
                    reason = "placement_origin_unproven"
                except HTTPException as exc:
                    if exc.status_code != 409:
                        raise
                    reason = "placement_eligibility_or_origin_unproven"
            _check_day(day)
            projected_origin = None if origin is None else {
                **origin, "qty": decimal_text(origin["qty"]),
                "anchor_prior": sorted(origin["anchor_prior"]),
            }
            unit_evidence.append({"unit_id": unit.id, "reason": reason, "effective_lot": lot,
                                  "origin": projected_origin, "frontier": frontier, "history": records})
        operations = list((await session.scalars(select(PhysicalOperation).where(
            PhysicalOperation.organization_id == organization_id,
            PhysicalOperation.warehouse_id == warehouse_id).order_by(PhysicalOperation.id)
            .execution_options(populate_existing=True))).all())
        await stock._fresh_capacity_authority(session, organization_id, actor, ("wms.operate",))
        _check_day(day)
        seed = stock._caller_seed
        return freeze_snapshot({
            "protocol": 1, "organization_id": organization_id, "warehouse_id": warehouse_id,
            "decision_date": day.isoformat(), "warehouse": _observation_row(warehouse),
            "authority": {"user_id": seed[7], "sub": seed[2], "issued_at": seed[6], "roles": list(seed[1])},
            "resources": [_observation_row(row) for row in resources],
            "locations": [_observation_row(row) for row in locations],
            "native_locations": [_observation_row(row) for row in native_locations],
            "profiles": profile_values, "slots": [_observation_row(row) for row in slots],
            "packaging": [_observation_row(row) for row in packaging],
            "chains": chains, "carriers": [_observation_row(row) for row in carriers],
            "positions": [_observation_row(row) for row in positions],
            "counts": [_observation_row(row) for row in counts],
            "count_locks": [_observation_row(row) for row in count_locks],
            "count_lines": [_observation_row(row) for row in count_lines],
            "tasks": [_observation_row(row) for row in tasks],
            "assignments": [_observation_row(row) for row in assignments],
            "claims": [_observation_row(row) for row in claims], "holds": [_observation_row(row) for row in holds],
            "picks": [_observation_row(row) for row in picks], "work_policies": [_observation_row(row) for row in work],
            "storage_policies": [_observation_row(row) for row in storage],
            "skus": [_observation_row(row) for row in skus],
            "units": [_observation_row(row) for row in present_units], "unit_evidence": unit_evidence,
            "operations": [_observation_row(row) for row in operations],
        })


def _latest_profiles(data):
    result = {}
    for row in data["profiles"]:
        key = (row["packaging_id"], row["location_id"], row["resource_id"])
        if key not in result or row["revision"] > result[key]["revision"]:
            result[key] = row
    return result


def _decimal(value):
    return None if value is None else Decimal(value)


def _timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _compiled_profiles(data, proof):
    frames = {row["profile_id"]: row["frame_id"] for row in proof["profile_bindings"]}
    result = []
    from modules.wms.physical_placement import Slot
    decimal_names = {"length_mm", "width_mm", "height_mm", "opening_width_mm", "opening_height_mm",
                     "clearance_mm", "gross_limit_kg", "tare_kg", "top_load_limit_kg"}
    for row in _latest_profiles(data).values():
        values = {field.name: row[field.name] for field in fields(NativeProfile)}
        for key in decimal_names:
            values[key] = _decimal(values[key])
        for key in ("orientations", "restrictions"):
            values[key] = None if values[key] is None else tuple(values[key])
        values["approved_at"] = _timestamp(values["approved_at"])
        chain = values["ancestor_binding"]
        values["ancestor_binding"] = None if chain is None else tuple(
            (item["resource_id"], item["parent_id"], item["topology_revision"]) for item in chain)
        children = tuple(Slot(child["warehouse_id"], child["location_id"], child["profile_id"], child["slot_key"],
                              *(_decimal(child[key]) for key in ("x_mm", "y_mm", "z_mm", "width_mm", "height_mm", "depth_mm")))
                         for child in data["slots"] if child["profile_id"] == row["id"])
        result.append(Profile(NativeProfile(**values), children, frames.get(row["id"]),
                              values["ancestor_binding"] or (), row["canonical_payload"], row["digest"], row["sealed"]))
    return tuple(sorted(result, key=lambda row: row.native.id))


def _committed_unit_ids(data):
    held = {row["unit_id"] for row in data["holds"] if Decimal(row["qty"]) >
            Decimal(row["shipped"]) + Decimal(row["consumed"]) + Decimal(row["released"])}
    held.update(row["unit_id"] for row in data["picks"] if Decimal(row["qty"]) >
                Decimal(row["shipped"]) + Decimal(row["canceled"]))
    held.update(row["unit_id"] for row in data["tasks"] if row["status"] in ("open", "claimed") and row["unit_id"] is not None)
    return held


def _empty_loose_native_unit(row, evidence, committed):
    """Only a complete native zero proof can remove unbound occupancy."""
    from modules.wms.item_identity import (
        _OriginUnproven, _origin_digest_text, _origin_dimensions,
        _origin_frontier_tokens, _origin_qty, _origin_uuid_text,
    )
    required = {"id", "carrier_id", "packaging_id", "package_state", "container", "qty", "reserved"}
    if (not required.issubset(row) or row["carrier_id"] is not None
            or row["packaging_id"] is not None or row["package_state"] != "loose"
            or row["container"] != "" or row["id"] in committed
            or not isinstance(evidence, dict) or evidence.get("unit_id") != row["id"]):
        return False
    origin = evidence.get("origin")
    history = evidence.get("history")
    keys = {"qty", "dimensions", "basis", "root_operation_id", "tail_operation_id", "anchor_prior", "proof_digest"}
    if (not isinstance(origin, dict) or not keys.issubset(origin)
            or origin["basis"] not in {"received", "clone", "clone_suffix", "approved_count", "assembly_output", "assembly_return"}
            or not _origin_uuid_text(origin["root_operation_id"])
            or not _origin_uuid_text(origin["tail_operation_id"])
            or not _origin_digest_text(origin["proof_digest"])
            or not isinstance(origin["anchor_prior"], list)
            or not isinstance(history, dict) or not {"ledgers", "operations", "count_operations"}.issubset(history)
            or not isinstance(history["ledgers"], list) or not isinstance(history["operations"], dict)
            or not isinstance(history["count_operations"], list)
            or ("qc_birth_operation_id" in origin and not _origin_uuid_text(origin["qc_birth_operation_id"]))):
        return False
    try:
        dimensions = _origin_dimensions(origin["dimensions"])
        tokens = _origin_frontier_tokens(evidence["frontier"])
        return (_origin_qty(row["qty"]) == _origin_qty(row["reserved"]) == _origin_qty(origin["qty"]) == 0
                and all(key in row and row[key] == value for key, value in dimensions.items())
                and all(isinstance(token, str) for token in origin["anchor_prior"])
                and set(origin["anchor_prior"]).issubset(tokens))
    except (_OriginUnproven, KeyError, TypeError, ValueError):
        return False


def _current_units(data, own_hold_ids=()):
    evidence = {row["unit_id"]: row for row in data["unit_evidence"]}
    held = _committed_unit_ids({**data, "holds": [row for row in data["holds"]
                                  if row["id"] not in own_hold_ids]})
    domains = defaultdict(list)
    for row in data["units"]:
        item = evidence[row["id"]]
        lot = item["effective_lot"]
        if lot is not None and item["origin"] is not None:
            domains[(row["sku_code"], row["characteristic"], row["purpose"])].append(
                ((lot.get("expiry_date") or "9999-12-31", row["arrived_at"], row["id"]), row["id"]))
    ranks = {unit_id: rank for group in domains.values() for rank, (_, unit_id) in enumerate(sorted(group))}
    units = {}
    for row in data["units"]:
        item = evidence[row["id"]]
        reason = item["reason"]
        status = "unknown" if item["origin"] is None or item["effective_lot"] is None else (
            "ineligible" if reason is not None or row["id"] in held else "eligible")
        lot = str(row["batch_id"]) if item["effective_lot"] is None else item["effective_lot"]["lot_no"]
        units[row["id"]] = DomainUnit(row["id"], row["sku_code"], lot, Decimal(row["qty"]),
                                     Decimal(row["reserved"]), status, ranks.get(row["id"]),
                                     row["characteristic"], row["purpose"])
    return units


def compile_measured_corridors(data, proof, measured_route, units):
    declaration = measured_route["corridors"]
    if declaration["state"] != "confirmed":
        reject("placement_measured_corridors_unknown")
    frames = {row["frame_key"]: row["frame_id"] for row in proof["resolved_frames"]}
    native = {row["id"]: row for row in data["units"]}
    corridors = []
    for row in declaration["value"]:
        grouped = defaultdict(list)
        for unit_id in row["protected_unit_ids"]:
            current, source = units.get(unit_id), native.get(unit_id)
            if current is None or source is None or source["location_id"] != row["location_id"]:
                reject("placement_protected_unit_current_binding_missing")
            grouped[(current.sku, current.characteristic, current.purpose)].append(current)
        groups = []
        for domain, members in sorted(grouped.items()):
            if any(unit.eligibility == "unknown" or unit.fefo_rank is None for unit in members):
                mode, threshold = "unknown", None
            elif any(unit.qty == 0 or unit.reserved != 0 or unit.eligibility == "ineligible" for unit in members):
                mode, threshold = "must_clear", None
            else:
                mode, threshold = "fefo", min(unit.fefo_rank for unit in members)
            groups.append(CorridorDomain(domain, tuple(sorted(unit.id for unit in members)), mode, threshold))
        box = row["box"]
        corridors.append(DomainCorridor("domain_v1", Box(*(_decimal(box[key]) for key in
            ("x_mm", "y_mm", "z_mm", "length_mm", "height_mm", "width_mm"))), frames[row["frame_key"]], tuple(groups)))
    return tuple(corridors)


def compile_placement_facts(snapshot: PlacementSnapshot, policy: Policy, proof,
                            current_member_subs: tuple[str, ...], *, action=None,
                            own_task_id=None) -> DomainFacts:
    """Only caller-verified original policy/receipt/current binding may enter."""
    data = snapshot.data()
    proof = measured_context_proof(proof)
    if (proof["organization_id"], proof["warehouse_id"]) != (data["organization_id"], data["warehouse_id"]):
        reject("placement_measurement_scope_mismatch")
    declarations = proof["request"]["routes"]
    if declarations["state"] != "confirmed" or proof["request"]["supports"]["state"] != "confirmed":
        reject("placement_measured_geometry_unknown")
    route_map = {}
    for route in declarations["value"]:
        key = (route["source_location_id"], route["destination_location_id"])
        if key in route_map:
            reject("placement_duplicate_measured_route_pair")
        route_map[key] = route
    assignments, claims = decode_active_assignments(data, action=action, own_task_id=own_task_id)
    own_hold_ids = tuple(hold.hold_id for row in assignments if row.task_id == own_task_id
                         for hold in row.owned_holds)
    units = _current_units(data, own_hold_ids)
    compiled_routes = []
    for route in policy.routes:
        measured = route_map.get((route.source_id, route.destination_id))
        if measured is None or route.corridors:
            reject("placement_original_route_binding_contradiction")
        compiled_routes.append(replace(route, corridors=compile_measured_corridors(data, proof, measured, units)))
    compiled_policy = replace(policy, routes=tuple(compiled_routes), canonical_payload="", digest="")
    text = _canonical_payload(compiled_policy)
    compiled_policy = replace(compiled_policy, canonical_payload=text, digest=_sha(text))
    profiles = _compiled_profiles(data, proof)
    profile_by_location = {row.native.location_id: row for row in profiles if row.native.location_id is not None}
    frames = {row["frame_key"]: row["frame_id"] for row in proof["resolved_frames"]}
    resource_rows = tuple(Resource(row["id"], row["parent_id"], row["kind"], row["topology_revision"]) for row in data["resources"])
    native_locations = {row["id"]: row for row in data["native_locations"]}
    count_locks = {row["location_id"]: row["count_id"] for row in data["count_locks"]}
    locations = []
    for row in data["locations"]:
        if row["resource_id"] is None:
            continue
        profile = profile_by_location.get(row["location_id"])
        native_location = native_locations.get(row["location_id"])
        if native_location is None:
            reject("placement_native_location_missing")
        chain = tuple((item["resource_id"], item["parent_id"], item["topology_revision"])
                      for item in data["chains"].get(str(row["location_id"]), []))
        locations.append(Location(row["location_id"], row["code"], native_location["is_active"], row["resource_id"],
                                  row["resource_mapping_revision"], None if profile is None else profile.native.id,
                                  None if profile is None else profile.frame_id, chain, count_locks.get(row["location_id"])))
    supports = tuple(Support(row["location_id"], row["profile_id"], row["profile_digest"], frames[row["frame_key"]],
                             *(_decimal(row[key]) for key in ("plane_y_mm", "x_min_mm", "x_max_mm", "z_min_mm", "z_max_mm")),
                             row["bearing"]["kind"], row["bearing"].get("resource_id", row["bearing"].get("carrier_id")),
                             _decimal(row["top_load_limit_kg"]), row["layer"], None)
                     for row in proof["request"]["supports"]["value"])
    relations = proof["request"]["carrier_support_relations"]
    if relations["state"] != "confirmed":
        reject("placement_carrier_support_relations_unknown")
    lower = {row["carrier_id"]: row["support_carrier_id"] for row in relations["value"]}
    observations = {row["carrier_id"]: row for row in proof["carrier_observations"]}
    carriers = []
    for row in data["carriers"]:
        observation = observations.get(row["id"])
        actual = _observation_frame_values(row, data["positions"], data["units"], row["location_id"])
        positions = defaultdict(list)
        for position in actual["positions"]:
            positions[position["location_profile_id"]].append(position["slot_key"])
        compiled_positions = tuple(Position(row["id"], row["location_id"], profile_id, tuple(keys),
                                            next((p.frame_id for p in profiles if p.native.id == profile_id), None), lower.get(row["id"]))
                                   for profile_id, keys in sorted(positions.items()))
        carrier = Carrier(row["id"], row["warehouse_id"], row["location_id"], row["packaging_id"],
                          row["packaging_profile_id"], row["content_revision"], row["confirmed_content_revision"],
                          None if observation is None else placement_content_digest(actual), None,
                          _decimal(row["gross_kg"]), _decimal(row["tare_kg"]), row["state"],
                          tuple(units[member["id"]] for member in actual["members"]), compiled_positions)
        carrier = replace(carrier, member_digest=_sha(_json(_members(carrier))))
        carriers.append(carrier)
    warehouse_roots = tuple(sorted(row["id"] for row in data["resources"]
                                   if row["parent_id"] is None and row["kind"] == "warehouse"))
    unknown = []
    unit_evidence = {row["unit_id"]: row for row in data["unit_evidence"]}
    committed = _committed_unit_ids(data)
    for row in data["units"]:
        if row["carrier_id"] is not None:
            continue
        if _empty_loose_native_unit(row, unit_evidence.get(row["id"]), committed):
            continue
        influence = tuple(item["resource_id"] for item in data["chains"].get(str(row["location_id"]), [])) or warehouse_roots
        if not influence:
            reject("placement_unbound_resource_influence_unknown")
        unknown.append(UnknownOccupancy(row["location_id"], influence, "unbound_native_unit_footprint"))
    facts = DomainFacts(data["organization_id"], data["warehouse_id"], data["warehouse"]["mode"],
                        data["authority"]["sub"], current_member_subs, compiled_policy, tuple(locations), resource_rows,
                        profiles, supports, tuple(carriers), assignments, claims, tuple(unknown), "", "", data["decision_date"], snapshot.digest, proof["digest"])
    text = _canonical_payload(facts)
    return replace(facts, canonical_payload=text, digest=_sha(text))


WORK_KIND = "capacity_work_approve"
WORK_PRIVATE = "_placement_work_policy_v1"
WORK_ROW_KEYS = frozenset(column.name for column in PlacementWorkPolicy.__table__.columns)
OP_COLUMNS = ("id", "organization_id", "warehouse_id", "operation_id", "kind", "request_hash", "actor", "result")


def _closed(value, keys, code):
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError(code)
    return value


def _policy_row(row):
    from modules.wms.physical_models import _cap_timestamp_text
    result = dict(row)
    for key in ("approved_at", "valid_from", "valid_until"):
        if isinstance(result[key], datetime):
            result[key] = _cap_timestamp_text(result[key])
    return _closed(json.loads(canonical(result)), WORK_ROW_KEYS, "work_full_row_shape")


def _measurement_binding(row):
    binding = _closed(row["server_binding"], {"protocol", "measured_context"}, "work_server_binding_shape")
    if type(binding["protocol"]) is not int or binding["protocol"] != 1:
        raise ValueError("work_server_binding_protocol")
    return measured_context_proof(binding["measured_context"])


def _route_map(proof):
    request = proof["request"]
    for key in ("routes", "supports", "carrier_support_relations"):
        if request[key]["state"] != "confirmed":
            raise ValueError("work_measured_geometry_unknown")
    result = {}
    for route in request["routes"]["value"]:
        key = (route["source_location_id"], route["destination_location_id"])
        if key in result:
            raise ValueError("work_duplicate_measured_route_pair")
        if route["openings"]["state"] != "confirmed" or route["corridors"]["state"] != "confirmed":
            raise ValueError("work_route_measurements_unknown")
        result[key] = route
    return result


def derive_work_record(data, request, measured_proof, approver, *, revision, equipment=None):
    """Derive selected facts only; caller must prove current rows and authority."""
    from modules.wms.physical_placement import LoadRule, Route, Opening, _box
    from modules.wms.placement_schemas import PlacementWorkApprove, normalize_placement_request
    from modules.wms.placement_policy_codec import decode_placement_work_policy
    command = PlacementWorkApprove.model_validate(request)
    if normalize_placement_request(command) != request:
        raise ValueError("work_request_not_canonical")
    proof = measured_context_proof(measured_proof)
    org, wh = data["organization_id"], data["warehouse_id"]
    if (proof["organization_id"], proof["warehouse_id"], request["warehouse_id"]) != (org, wh, wh):
        raise ValueError("work_measurement_scope")
    work = request["work"]
    routes = _route_map(proof)
    bindings = {row["profile_id"]: row for row in proof["profile_bindings"]}
    declarations = {row["profile_id"]: row for row in proof["request"]["profile_frames"]}
    places = {row["location_id"]: row for row in proof["topology"]["locations"]}
    compiled_profiles = {row.native.id: row for row in _compiled_profiles(data, proof)}
    selected = []
    for selection in work["routes"]:
        key = (selection["source_location_id"], selection["destination_location_id"])
        measured = routes.get(key)
        if measured is None:
            raise ValueError("work_exact_measured_route_missing")
        src = bindings.get(measured["source_profile_id"])
        dst = bindings.get(measured["destination_profile_id"])
        if src is None or dst is None:
            raise ValueError("work_route_profile_binding_missing")
        for binding in (src, dst):
            if declarations[binding["profile_id"]]["measured_slots"]["state"] != "confirmed":
                raise ValueError("work_measured_slots_unknown")
        profile = compiled_profiles.get(dst["profile_id"])
        if profile is None or profile.frame_id != dst["frame_id"] or not measured["slot_groups"]:
            raise ValueError("work_destination_measured_profile_or_groups_missing")
        if sorted(tuple(sorted(group)) for group in selection["access_slot_groups"]) != sorted(tuple(sorted(group)) for group in measured["slot_groups"]):
            raise ValueError("work_slot_group_assertion_mismatch")
        heights = tuple(_box(profile, tuple(group)).y + _box(profile, tuple(group)).h for group in measured["slot_groups"])
        maximum = max(heights)
        if selection["placement_height_mm"] is not None and Decimal(selection["placement_height_mm"]) != maximum:
            raise ValueError("work_destination_height_assertion_mismatch")
        paths = tuple(tuple(item["resource_id"] for item in places[location_id]["ancestor_binding"])
                      for location_id in measured["phase_location_ids"])
        if len(set(paths)) != len(paths):
            # The pure grammar requires unique paths; preserve measured phases by
            # consolidating identical resource charges without inventing a path.
            paths = tuple(dict.fromkeys(paths))
        resources = tuple(sorted({resource for path in paths for resource in path}))
        destination_path = tuple(item["resource_id"] for item in places[key[1]]["ancestor_binding"])
        if set(selection["resource_ids"]) != set(resources):
            raise ValueError("work_resource_assertion_mismatch")
        openings = tuple(Opening(row["width_axis"], row["height_axis"], Decimal(row["width_mm"]),
                                 Decimal(row["height_mm"]), _decimal(row["clearance_mm"]))
                         for row in measured["openings"]["value"])
        if work["access_method"] == "manual":
            height_limit = _decimal(work["manual_height_limit_mm"])
        else:
            height_limit = None if equipment is None else _decimal(equipment["request"]["operating_height_limit_mm"])
        if height_limit is None or any(height > height_limit for height in heights):
            raise ValueError("work_server_destination_height_unknown_or_exceeded")
        selected.append(Route(key[0], key[1], src["profile_id"], src["digest"], dst["profile_id"], dst["digest"],
            places[key[0]]["resource_mapping_revision"], places[key[1]]["resource_mapping_revision"], destination_path,
            paths, tuple(tuple(group) for group in measured["slot_groups"]), openings, (), proof["digest"], selection["route_evidence"]))
    resource_profiles = {row["resource_id"]: row for row in _latest_profiles(data).values() if row["resource_id"] is not None}
    required_resources = sorted({resource for route in selected for path in route.phase_resource_paths for resource in path})
    rules = []
    for resource_id in required_resources:
        profile = resource_profiles.get(resource_id)
        if profile is None or profile["gross_limit_kg"] is None:
            raise ValueError("work_required_resource_limit_unknown")
        rules.append(LoadRule(resource_id, "required", Decimal(profile["gross_limit_kg"]), profile["id"], profile["digest"]))
    if work["access_method"] == "manual":
        if work["manual_gross_limit_kg"] is None:
            raise ValueError("work_manual_gross_limit_unknown")
        equipment_id = equipment_digest = equipment_gross = equipment_height = None
        equipment_state = "unknown"
    else:
        if equipment is None or equipment["equipment_resource_id"] != work["equipment_resource_id"]:
            raise ValueError("work_equipment_receipt_missing")
        observed = equipment["request"]
        equipment_state = observed["state"]
        if equipment_state != "available" or not (_timestamp(observed["valid_from"]) <= _timestamp(work["valid_from"]) and _timestamp(work["valid_until"]) <= _timestamp(observed["valid_until"])):
            raise ValueError("work_equipment_not_available_for_window")
        if work["equipment_available"] is not True:
            raise ValueError("work_equipment_expected_assertion_mismatch")
        equipment_id, equipment_digest = equipment["equipment_resource_id"], equipment["profile_binding"]["digest"]
        equipment_gross, equipment_height = _decimal(observed["operating_gross_limit_kg"]), _decimal(observed["operating_height_limit_mm"])
        if equipment_gross is None or equipment_height is None or work["equipment_gross_limit_kg"] is None or Decimal(work["equipment_gross_limit_kg"]) != equipment_gross:
            raise ValueError("work_equipment_measured_limits_unknown_or_mismatch")
    binding = {"protocol": 1, "measured_context": proof}
    policy = Policy(request["policy_id"], org, wh, work["scope_code"], revision, work["access_method"],
        tuple(work["operator_subs"]), tuple(selected), equipment_id, equipment_digest, equipment_state,
        _decimal(work["manual_gross_limit_kg"]), _decimal(work["manual_height_limit_mm"]), equipment_gross, equipment_height,
        tuple(rules), _timestamp(work["valid_from"]), _timestamp(work["valid_until"]), work["source_ref"],
        work["source_revision"], work["evidence"], approver["sub"], _timestamp(approver["approved_at"]), request["operation_id"], digest(binding), "", "")
    text = _canonical_payload(policy)
    policy = replace(policy, canonical_payload=text, digest=_sha(text))
    body = json.loads(text)
    row = {"id": policy.id, "organization_id": org, "warehouse_id": wh, "scope_code": policy.scope_code,
           "revision": revision, "access_method": policy.access_method, "valid_from": work["valid_from"], "valid_until": work["valid_until"],
           "policy": {key: body[key] for key in ("operator_subs", "routes", "equipment_resource_id", "equipment_profile_digest", "equipment_state", "manual_gross_limit_kg", "manual_height_limit_mm", "equipment_gross_limit_kg", "equipment_height_limit_mm", "load_rules")},
           "server_binding": binding, "canonical_payload": text, "digest": policy.digest,
           "source_ref": policy.source_ref, "source_revision": policy.source_revision, "evidence": policy.evidence,
           "approved_by": policy.approved_by, "approved_at": approver["approved_at"], "approval_operation_id": policy.approval_operation_id}
    decode_placement_work_policy({**row, **{key: _timestamp(row[key])
        for key in ("valid_from", "valid_until", "approved_at")}})
    return row


def work_ack(row):
    return {"protocol": 2, "operation_id": row["approval_operation_id"], "kind": WORK_KIND,
            "warehouse_id": row["warehouse_id"], "policy_id": row["id"], "revision": row["revision"], "digest": row["digest"], "status": "approved"}


def _work_capture_models():
    # Native rows only. Neither raw warehouse operations nor recursive Work
    # receipts/policies are included in this closed capture.
    return {"locations": PhysicalLocation, "native_locations": LocationRow,
            "holds": PhysicalHold, "picks": PickedAllocation, "tasks": PhysicalTask,
            "assignments": PlacementAssignment, "claims": PlacementClaim,
            "count_locks": CountLocationLock, "counts": PhysicalCount,
            "count_lines": PhysicalCountLine}


def _capture_location_frames(data):
    places = sorted(data["locations"], key=lambda row: row["location_id"])
    ids = {row["location_id"] for row in places}
    if any(row["location_id"] not in ids for row in (*data["units"], *data["carriers"])):
        raise ValueError("work_capture_location_coverage_unknown")
    return [{"location_id": place["location_id"],
             "units": sorted((row for row in data["units"] if row["location_id"] == place["location_id"]), key=lambda row: row["id"]),
             "carriers": sorted((row for row in data["carriers"] if row["location_id"] == place["location_id"]), key=lambda row: row["id"]),
             "positions": sorted((row for row in data["positions"] if row["location_id"] == place["location_id"]), key=lambda row: (row["carrier_id"], row["ordinal"]))}
            for place in places]


def _work_capture_statement(name, model, warehouse_id):
    # Reuse the collector's actual FK ownership joins for rows that have no
    # warehouse_id column; never infer warehouse by a name or missing field.
    table = model.__table__
    if name == "native_locations":
        native = PhysicalLocation.__table__
        return select(table).join(native, native.c.location_id == table.c.id).where(native.c.warehouse_id == warehouse_id)
    if name == "picks":
        parent = PhysicalHold.__table__
        return select(table).join(parent, parent.c.id == table.c.hold_id).where(parent.c.warehouse_id == warehouse_id)
    if name == "count_locks":
        parent = PhysicalLocation.__table__
        return select(table).join(parent, parent.c.location_id == table.c.location_id).where(parent.c.warehouse_id == warehouse_id)
    if name == "count_lines":
        parent = PhysicalCount.__table__
        return select(table).join(parent, parent.c.id == table.c.count_id).where(parent.c.warehouse_id == warehouse_id)
    return select(table).where(table.c.warehouse_id == warehouse_id)


def make_work_location_capture(data, row, approver, predecessor_pk):
    # Enumerate the entire locked native warehouse, a finite superset of every
    # selected route/phase/load/corridor. Explicit empty differs from absent.
    captured = {name: data[name] for name in _work_capture_models()}
    return json.loads(canonical({"protocol": 1, "organization_id": row["organization_id"],
        "warehouse_id": row["warehouse_id"], "operation_id": row["approval_operation_id"],
        "policy_id": row["id"], "policy_digest": row["digest"],
        "measurement_operation_id": (measured := _measurement_binding(row))["operation_id"],
        "measurement_digest": measured["digest"],
        "predecessor_pk": predecessor_pk, "actor": {"user_id": approver["user_id"], "sub": approver["sub"]},
        "location_frames": _capture_location_frames(data), "unit_evidence": data["unit_evidence"], **captured}))


def _work_capture_shape(capture, row, approver):
    return _work_validation("capture", row, None, capture, approver)


def _work_validation(entry, row, receipt, capture, approver):
    if entry not in ("capture", "envelope"):
        raise ValueError("work_validation_entry")
    if entry == "envelope":
        from modules.wms.placement_policy_codec import decode_placement_work_policy
        from modules.wms.placement_schemas import PlacementWorkApprove, normalize_placement_request
        from modules.wms.physical_placement import _uuid
        row = _policy_row(row)
        _closed(receipt, {"organization_id", "warehouse_id", "operation_id", "kind", "request_hash", "actor", "result"}, "work_receipt_columns_shape")
        if (receipt["organization_id"], receipt["warehouse_id"]) != (row["organization_id"], row["warehouse_id"]):
            raise ValueError("work_row_receipt_scope_mismatch")
        result = receipt["result"]
        _closed(result, set(work_ack(row)) | {WORK_PRIVATE}, "work_result_shape")
        if canonical({key: value for key, value in result.items() if key != WORK_PRIVATE}) != canonical(work_ack(row)):
            raise ValueError("work_ack_correspondence")
        version = result[WORK_PRIVATE].get("protocol") if type(result[WORK_PRIVATE]) is dict else None
        if type(version) is not int or version not in (1, 2):
            raise ValueError("work_proof_canonical_or_digest")
        proof = _closed(result[WORK_PRIVATE], {"protocol", "organization_id", "warehouse_id", "operation_id", "kind", "request_hash", "request", "approver", "operators", "policy_row", "equipment_receipt", "canonical_payload", "digest"} | ({"location_capture"} if version == 2 else set()), "work_proof_shape")
        unsigned = {key: value for key, value in proof.items() if key not in {"canonical_payload", "digest"}}
        if proof["canonical_payload"] != canonical(unsigned) or proof["digest"] != digest(unsigned):
            raise ValueError("work_proof_canonical_or_digest")
        if canonical(proof["policy_row"]) != canonical(row):
            raise ValueError("work_proof_original_row_mismatch")
        approver = _closed(proof["approver"], {"user_id", "sub", "issued_at", "roles", "approved_at"}, "work_approver_shape")
        if type(approver["user_id"]) is not int or not 0 < approver["user_id"] <= 2147483647 or not isinstance(approver["sub"], str) or not approver["sub"].strip():
            raise ValueError("work_original_numeric_identity")
        if approver["issued_at"] is not None and (type(approver["issued_at"]) is not int or approver["issued_at"] < 0):
            raise ValueError("work_original_issued_at")
        if type(approver["roles"]) is not list or any(type(role) is not str for role in approver["roles"]):
            raise ValueError("work_original_roles")
        command = PlacementWorkApprove.model_validate(proof["request"])
        request = normalize_placement_request(command)
        if request != proof["request"] or command.expected_revision + 1 != row["revision"] or str(command.policy_id) != row["id"]:
            raise ValueError("work_request_original_row_mismatch")
        _uuid(row["id"])
        policy = decode_placement_work_policy({**row, **{key: _timestamp(row[key])
            for key in ("valid_from", "valid_until", "approved_at")}})
        measured = _measurement_binding(row)
        if type(proof["operators"]) is not list or len(proof["operators"]) != len(policy.operator_subs):
            raise ValueError("work_operator_proof_missing")
        seen = set()
        for operator in proof["operators"]:
            _closed(operator, {"user_id", "sub", "role"}, "work_operator_proof_shape")
            if type(operator["user_id"]) is not int or not 0 < operator["user_id"] <= 2147483647 or type(operator["sub"]) is not str or type(operator["role"]) is not str or operator["sub"] in seen:
                raise ValueError("work_operator_proof_identity")
            seen.add(operator["sub"])
        if seen != set(policy.operator_subs):
            raise ValueError("work_operator_proof_correspondence")
        expected_hash = digest({"organization_id": row["organization_id"], "actor_user_id": approver["user_id"], "actor_sub": approver["sub"], "request": request})
        if any(receipt[key] != proof[key] for key in ("organization_id", "warehouse_id", "operation_id", "kind", "request_hash")) or receipt["actor"] != approver["sub"] or receipt["request_hash"] != expected_hash:
            raise ValueError("work_receipt_identity_hash_correspondence")
        if (receipt["organization_id"], receipt["warehouse_id"], receipt["operation_id"], receipt["kind"], approver["sub"], approver["approved_at"]) != (row["organization_id"], row["warehouse_id"], row["approval_operation_id"], WORK_KIND, row["approved_by"], row["approved_at"]):
            raise ValueError("work_row_receipt_correspondence")
        if version == 1:
            return proof
        capture = proof["location_capture"]
    from modules.wms.placement_policy_codec import _measured_native_frame
    keys = {"protocol", "organization_id", "warehouse_id", "operation_id", "policy_id", "policy_digest",
            "measurement_operation_id", "measurement_digest", "predecessor_pk", "actor", "location_frames", "unit_evidence"}
    models = _work_capture_models()
    _closed(capture, keys | set(models), "work_capture_shape")
    if (type(capture["protocol"]) is not int or capture["protocol"] != 1
            or type(capture["predecessor_pk"]) is not int or capture["predecessor_pk"] <= 0
            or any(capture[key] != row[source] for key, source in (("organization_id", "organization_id"),
                ("warehouse_id", "warehouse_id"), ("operation_id", "approval_operation_id"),
                ("policy_id", "id"), ("policy_digest", "digest")))
            or capture["actor"] != {"user_id": approver["user_id"], "sub": approver["sub"]}
            or capture["measurement_operation_id"] != (measured := (measured if entry == "envelope" else _measurement_binding(row)))["operation_id"]
            or capture["measurement_digest"] != measured["digest"]):
        raise ValueError("work_capture_scope_or_identity")
    wh = row["warehouse_id"]
    native_locations = {item["location_id"] for item in capture["locations"]}
    hold_ids = {item["id"] for item in capture["holds"]}
    count_ids = {item["id"] for item in capture["counts"]}
    for name, model in models.items():
        values = capture[name]
        if type(values) is not list:
            raise ValueError("work_capture_native_rows_shape")
        primary = tuple(column.name for column in model.__table__.primary_key)
        seen = set()
        for value in values:
            _closed(value, {column.name for column in model.__table__.columns}, "work_capture_native_row_columns")
            if name == "native_locations":
                scoped = value["id"] in native_locations
            elif name == "picks":
                scoped = value["hold_id"] in hold_ids
            elif name in ("count_locks", "count_lines"):
                scoped = value["count_id"] in count_ids and value["location_id"] in native_locations
            else:
                scoped = type(value["warehouse_id"]) is int and value["warehouse_id"] == wh
            if not scoped or ("organization_id" in value and value["organization_id"] != row["organization_id"]):
                raise ValueError("work_capture_native_row_scope")
            identity = tuple(value[key] for key in primary)
            if identity in seen:
                raise ValueError("work_capture_native_row_duplicate")
            seen.add(identity)
    if {place["id"] for place in capture["native_locations"]} != native_locations:
        raise ValueError("work_capture_location_coverage")
    frames = capture["location_frames"]
    if type(frames) is not list or [frame.get("location_id") for frame in frames] != sorted(native_locations):
        raise ValueError("work_capture_location_coverage")
    units, carriers, positions = [], [], []
    for frame in frames:
        _closed(frame, {"location_id", "units", "carriers", "positions"}, "work_capture_location_frame_shape")
        for name, model in (("units", StockUnit), ("carriers", PhysicalCarrier), ("positions", CarrierPosition)):
            if type(frame[name]) is not list:
                raise ValueError("work_capture_frame_rows_shape")
            for item in frame[name]:
                columns = (STOCK_UNIT_MEASUREMENT_V1_COLUMNS if model is StockUnit
                    else tuple(column.name for column in model.__table__.columns))
                _closed(item, set(columns), "work_capture_frame_row_columns")
                if item["warehouse_id"] != wh or item["location_id"] != frame["location_id"]:
                    raise ValueError("work_capture_frame_row_scope")
        if [item["id"] for item in frame["units"]] != sorted({item["id"] for item in frame["units"]}):
            raise ValueError("work_capture_unit_order_or_duplicate")
        if [item["id"] for item in frame["carriers"]] != sorted({item["id"] for item in frame["carriers"]}):
            raise ValueError("work_capture_carrier_order_or_duplicate")
        for carrier in frame["carriers"]:
            native = _observation_frame_values(carrier, frame["positions"], frame["units"], frame["location_id"])
            _measured_native_frame(native, wh)
        units.extend(frame["units"])
        carriers.extend(frame["carriers"])
        positions.extend(frame["positions"])
    if (len({unit["id"] for unit in units}) != len(units)
            or len({carrier["id"] for carrier in carriers}) != len(carriers)
            or len({(position["carrier_id"], position["ordinal"]) for position in positions}) != len(positions)
            or any(position["carrier_id"] not in {carrier["id"] for carrier in carriers} for position in positions)):
        raise ValueError("work_capture_global_row_duplicate_or_orphan")
    evidence = capture["unit_evidence"]
    if type(evidence) is not list or {item.get("unit_id") for item in evidence} != {unit["id"] for unit in units} or len(evidence) != len(units):
        raise ValueError("work_capture_unit_evidence_coverage")
    for item in evidence:
        _closed(item, {"unit_id", "reason", "effective_lot", "origin", "frontier", "history"}, "work_capture_unit_evidence_shape")
    if entry == "envelope":
        return proof
    return {frame["location_id"]: json.loads(canonical(frame)) for frame in frames}


def _validate_work_capture(connection, receipt, proof):
    from modules.wms.physical_capacity import _historical_observed_origin
    from modules.wms.physical_models import PhysicalLedger
    capture = proof["location_capture"]
    _work_capture_shape(capture, proof["policy_row"], proof["approver"])
    actual = _placement_operation(connection, receipt["organization_id"], receipt["warehouse_id"], receipt["operation_id"])
    if (type(receipt.get("id")) is not int or receipt["id"] != actual["id"]
            or canonical({key: actual[key] for key in OP_COLUMNS}) != canonical({key: receipt[key] for key in OP_COLUMNS})):
        raise ValueError("work_capture_actual_work_pk")
    table = PhysicalOperation.__table__
    predecessor = connection.execute(select(table.c.id).where(table.c.organization_id == receipt["organization_id"],
        table.c.warehouse_id == receipt["warehouse_id"], table.c.id < actual["id"]).order_by(table.c.id.desc())).scalar()
    measurement = _placement_operation(connection, receipt["organization_id"], receipt["warehouse_id"], capture["measurement_operation_id"])
    if predecessor != capture["predecessor_pk"] or not measurement["id"] <= capture["predecessor_pk"] < actual["id"]:
        raise ValueError("work_capture_predecessor_or_measurement_order")
    if connection.execute(select(PhysicalLedger.__table__.c.id).where(PhysicalLedger.__table__.c.operation_pk == actual["id"])).first() is not None:
        raise ValueError("placement_work_nonzero_ledger_packet")
    by_evidence = {item["unit_id"]: item for item in capture["unit_evidence"]}
    for frame in capture["location_frames"]:
        for unit in frame["units"]:
            value = _historical_observed_origin(connection, receipt["organization_id"], receipt["warehouse_id"],
                actual["id"], unit, work_capture=capture, full_evidence=True)
            evidence = by_evidence[unit["id"]]
            if canonical({key: evidence[key] for key in ("origin", "frontier", "history")}) != canonical(value):
                raise ValueError("work_capture_native_origin_evidence_changed")
    return capture


def _work_capture_current_rows(connection, capture, *, location_frames=None, changed_tasks=()):
    from modules.wms.physical_capacity import _measurement_db_row
    wh = capture["warehouse_id"]
    models = _work_capture_models()
    owner_ref = connection.info.get("wms_native_historical_owner_v1")
    owner = None if owner_ref is None else owner_ref()
    if owner is None or not owner.in_transaction():
        raise ValueError("physical_origin_historical_caller_untracked")
    covered_models = (StockUnit, PhysicalCarrier, CarrierPosition, *models.values())
    places = {row["location_id"] for row in capture["locations"]}
    holds = {row["id"] for row in capture["holds"]}
    counts = {row["id"] for row in capture["counts"]}
    for obj in (*owner.new, *owner.dirty, *owner.deleted):
        if not isinstance(obj, covered_models):
            continue
        if isinstance(obj, LocationRow):
            scoped = obj.id in places
        elif isinstance(obj, PickedAllocation):
            scoped = obj.hold_id in holds
        elif isinstance(obj, (CountLocationLock, PhysicalCountLine)):
            scoped = obj.count_id in counts or obj.location_id in places
        else:
            scoped = obj.warehouse_id == wh
        if scoped:
            # A native Connection SELECT cannot see an unflushed caller edit.
            # Reject it before SQL rather than treating the old DB row as proof.
            raise ValueError("work_capture_current_location_frame_drift")
    changed_tasks = set(changed_tasks)
    changed_holds = set()
    for task_id in changed_tasks:
        header = connection.execute(select(PlacementAssignment.__table__).where(PlacementAssignment.__table__.c.task_id == task_id,
            PlacementAssignment.__table__.c.warehouse_id == wh)).mappings().one_or_none()
        if header is None:
            raise ValueError("work_capture_lifecycle_header_missing")
        body, _ = validate_assignment_snapshot(dict(header))
        changed_holds.update(item["id"] for item in body["native_owned_holds"])
        terminal = header["state"] in ("completed", "canceled")
        for expected in body["native_owned_holds"]:
            actual = connection.execute(select(PhysicalHold.__table__).where(PhysicalHold.__table__.c.id == expected["id"])).mappings().one_or_none()
            expected = {**expected, "released": expected["qty"] if terminal else expected["released"]}
            if actual is None or canonical(_measurement_db_row(PhysicalHold, actual)) != canonical(expected):
                raise ValueError("work_capture_lifecycle_hold_state")
        task = connection.execute(select(PhysicalTask.__table__).where(PhysicalTask.__table__.c.id == task_id)).mappings().one_or_none()
        if task is None or canonical({key: _measurement_db_row(PhysicalTask, task)[key] for key in body["task"]}) != canonical(body["task"]):
            raise ValueError("work_capture_lifecycle_task_identity")
        expected_status = {"assigned": "open", "in_transit": "claimed", "completed": "completed", "canceled": "canceled"}[header["state"]]
        if task["status"] != expected_status or task["assignee"] != body["executor"]["sub"]:
            raise ValueError("work_capture_lifecycle_task_state")
        claims = connection.execute(select(PlacementClaim.__table__).where(PlacementClaim.__table__.c.task_id == task_id).order_by(PlacementClaim.__table__.c.ordinal)).mappings().all()
        expected_claims = [] if terminal else [{"task_id": task_id, "ordinal": ordinal, "warehouse_id": wh,
            "location_id": header["destination_location_id"], "profile_id": header["destination_profile_id"], "slot_key": key}
            for ordinal, key in enumerate(body["choice"]["slot_keys"])]
        if canonical([dict(row) for row in claims]) != canonical(expected_claims):
            raise ValueError("work_capture_lifecycle_claim_state")
    for name, model in models.items():
        rows = [_measurement_db_row(model, row) for row in connection.execute(_work_capture_statement(name, model, wh)).mappings()]
        def unaffected(row):
            return not ((name in ("tasks", "assignments", "claims") and row.get("task_id", row.get("id")) in changed_tasks)
                or (name == "holds" and row["id"] in changed_holds))
        if sorted((canonical(row) for row in rows if unaffected(row))) != sorted(canonical(row) for row in capture[name] if unaffected(row)):
            raise ValueError("work_capture_unregistered_native_row_drift")
    rows = {}
    for name, model in (("units", StockUnit), ("carriers", PhysicalCarrier), ("positions", CarrierPosition)):
        rows[name] = [_measurement_db_row(model, row) for row in connection.execute(select(model.__table__).where(model.__table__.c.warehouse_id == wh)).mappings()]
    rows["locations"] = capture["locations"]
    actual_frames = _capture_location_frames(rows)
    expected_frames = capture["location_frames"] if location_frames is None else sorted(location_frames.values(), key=lambda frame: frame["location_id"])
    if canonical(actual_frames) != canonical(expected_frames):
        raise ValueError("work_capture_current_location_frame_drift")


def make_work_receipt(row, request, approver, operators, equipment, *, location_capture=None):
    request_hash = digest({"organization_id": row["organization_id"], "actor_user_id": approver["user_id"], "actor_sub": approver["sub"], "request": request})
    proof = {"protocol": 1, "organization_id": row["organization_id"], "warehouse_id": row["warehouse_id"],
             "operation_id": row["approval_operation_id"], "kind": WORK_KIND, "request_hash": request_hash,
             "request": request, "approver": approver, "operators": operators, "policy_row": row, "equipment_receipt": equipment}
    if location_capture is not None:
        proof.update(protocol=2, location_capture=location_capture)
    proof["canonical_payload"] = canonical(proof)
    proof["digest"] = _sha(proof["canonical_payload"])
    return {"organization_id": row["organization_id"], "warehouse_id": row["warehouse_id"], "operation_id": row["approval_operation_id"],
            "kind": WORK_KIND, "request_hash": request_hash, "actor": approver["sub"], "result": {**work_ack(row), WORK_PRIVATE: proof}}


def validate_work_envelope(receipt, row):
    return _work_validation("envelope", row, receipt, None, None)


def validate_work_receipt(connection, receipt):
    """Historical replay: original rows/proof, never today's measurement head."""
    table = PlacementWorkPolicy.__table__
    candidates = connection.execute(select(table).where(table.c.organization_id == receipt["organization_id"],
        table.c.warehouse_id == receipt["warehouse_id"], table.c.approval_operation_id == receipt["operation_id"])).mappings().all()
    if len(candidates) != 1:
        raise ValueError("work_original_policy_row_missing_or_duplicate")
    row = _policy_row(candidates[0])
    columns = {key: receipt[key] for key in ("organization_id", "warehouse_id", "operation_id", "kind", "request_hash", "actor", "result")}
    proof = validate_work_envelope(columns, row)
    measured = _measurement_binding(row)
    operation_table = PhysicalOperation.__table__
    measured_row = connection.execute(select(operation_table).where(operation_table.c.organization_id == row["organization_id"],
        operation_table.c.warehouse_id == row["warehouse_id"], operation_table.c.operation_id == measured["operation_id"])).mappings().one_or_none()
    if measured_row is None or (receipt.get("id") is not None and measured_row["id"] >= receipt["id"]):
        raise ValueError("work_historical_measurement_receipt_missing_or_late")
    actual = measurement_receipt(measured_row["result"], measured_row, connection)["_placement_measured_context_v1"]
    if canonical(actual) != canonical(measured):
        raise ValueError("work_historical_measured_binding_changed")
    equipment = proof["equipment_receipt"]
    if equipment is not None:
        from modules.wms.physical_capacity import equipment_operation_receipt
        equipment_row = connection.execute(select(operation_table).where(operation_table.c.organization_id == row["organization_id"],
            operation_table.c.warehouse_id == row["warehouse_id"], operation_table.c.operation_id == equipment["operation_id"])).mappings().one_or_none()
        if equipment_row is None or (receipt.get("id") is not None and equipment_row["id"] >= receipt["id"]) or canonical(equipment_operation_receipt(equipment_row, connection)) != canonical(equipment):
            raise ValueError("work_historical_equipment_binding_changed")
    # Re-derive using exact immutable historical profile rows, including all
    # owner resource load profiles. No transient reseal can hide row changes.
    from modules.wms.physical_models import PlacementProfile, PlacementSlot, _cap_full_snapshot
    body = json.loads(row["canonical_payload"])
    ids = {binding["profile_id"] for binding in measured["profile_bindings"]}
    ids.update(rule["source_profile_id"] for rule in body["load_rules"])
    if equipment is not None:
        ids.add(equipment["profile_binding"]["profile_id"])
    native_profiles = []
    for profile_id in sorted(ids):
        native = connection.execute(select(PlacementProfile.__table__).where(PlacementProfile.__table__.c.id == profile_id)).mappings().one_or_none()
        if native is None or native["organization_id"] != row["organization_id"] or native["warehouse_id"] != row["warehouse_id"] or native["sealed"] is not True:
            raise ValueError("work_historical_load_profile_missing")
        _cap_full_snapshot(connection, dict(native))
        native_profiles.append({**json.loads(native["canonical_payload"])["profile"], "canonical_payload": native["canonical_payload"], "digest": native["digest"], "sealed": True})
    slots = [dict(item) for item in connection.execute(select(PlacementSlot.__table__).where(PlacementSlot.__table__.c.profile_id.in_(ids)).order_by(PlacementSlot.__table__.c.profile_id, PlacementSlot.__table__.c.slot_key)).mappings()]
    for slot in slots:
        for key in ("x_mm", "y_mm", "z_mm", "width_mm", "height_mm", "depth_mm"):
            slot[key] = format(slot[key], ".6f")
    derived = derive_work_record({"organization_id": row["organization_id"], "warehouse_id": row["warehouse_id"], "profiles": native_profiles, "slots": slots},
        proof["request"], measured, proof["approver"], revision=row["revision"], equipment=equipment)
    if canonical(derived) != canonical(row):
        raise ValueError("work_historical_derived_row_mismatch")
    if proof["protocol"] == 2:
        _validate_work_capture(connection, receipt, proof)
    return row


def validate_current_work_policy(connection, row, equipment):
    from modules.wms.placement_models import placement_operation_indicator
    table = PhysicalOperation.__table__
    candidates = connection.execute(select(table.c.kind, table.c.result).where(
        table.c.organization_id == row["organization_id"], table.c.warehouse_id == row["warehouse_id"])).all()
    own = _placement_operation(connection, row["organization_id"], row["warehouse_id"], row["approval_operation_id"])
    if any(placement_operation_indicator(kind, result) for kind, result in candidates):
        placement_measurement_current_view(connection, _measurement_binding(row))
    else:
        measurement_current_binding(connection, _measurement_binding(row))
        private = own["result"][WORK_PRIVATE]
        if private["protocol"] == 2:
            # Capture is immutable evidence, not permission to bypass an
            # unsupported M-W event. Fresh assign always uses the ALL-event
            # fold; old no-lifecycle approval/preview keeps253 compatibility.
            _work_capture_current_rows(connection, private["location_capture"])
    _validate_work_current_limits(connection, row, equipment)


def _validate_work_current_limits(connection, row, equipment):
    from modules.wms.physical_models import PlacementProfile, _cap_full_snapshot
    from modules.wms.physical_capacity import equipment_state_heads, equipment_current_binding
    now = datetime.now(timezone.utc)
    if not _timestamp(row["valid_from"]) <= now < _timestamp(row["valid_until"]):
        raise ValueError("work_current_window_expired")
    for rule in row["policy"]["load_rules"]:
        table = PlacementProfile.__table__
        current = connection.execute(select(table).where(table.c.organization_id == row["organization_id"],
            table.c.warehouse_id == row["warehouse_id"], table.c.resource_id == rule["resource_id"]).order_by(table.c.revision.desc(), table.c.id)).mappings().first()
        if current is None or current["sealed"] is not True or current["id"] != rule["source_profile_id"] or current["digest"] != rule["source_profile_digest"]:
            raise ValueError("work_current_load_profile_stale")
        _cap_full_snapshot(connection, dict(current))
        _cap_current_binding(connection, dict(current))
    if equipment is not None:
        heads = equipment_state_heads(connection, row["organization_id"], row["warehouse_id"])
        if canonical(heads.get(equipment["equipment_resource_id"])) != canonical(equipment):
            raise ValueError("work_current_equipment_state_stale")
        equipment_current_binding(connection, equipment)


async def _managed_operators(session, stock, organization_id, subs):
    from core.domain.models import User
    from core.services.auth import CurrentUser, has_permission, resolve_effective_oidc_user
    result = []
    for sub in subs:
        row = await session.scalar(select(User).where(User.keycloak_user_id == sub).execution_options(populate_existing=True))
        if row is None or row.status != "active" or row.role is None or type(row.id) is not int or row.id <= 0:
            reject("work_current_managed_operator_missing", 403)
        uid, role = row.id, row.role
        caller = stock._caller_seed
        issued_at = caller[6] if caller is not None and (caller[7], caller[2]) == (uid, sub) else None
        seed = CurrentUser(row.username, [role], keycloak_user_id=sub, local_user_id=uid, issued_at=issued_at)
        fresh = await resolve_effective_oidc_user(seed, session)
        await stock.core.services.accounting.source_member(session, organization_id, fresh)
        fresh = await resolve_effective_oidc_user(seed, session)
        if fresh.local_status != "active" or fresh.local_user_id != uid or fresh.keycloak_user_id != sub or fresh.roles != [role] or not has_permission(stock.core, fresh, "wms.operate"):
            reject("work_current_operator_authority_changed", 403)
        result.append({"user_id": uid, "sub": sub, "role": role})
    return result


async def execute_work_approval(session, stock, organization_id, data, actor):
    from modules.wms.physical_capacity import measurement_state_head, equipment_state_heads, equipment_current_binding, require_capacity_read_committed
    from modules.wms.physical_stock import protect_blind_projection
    from modules.wms.placement_models import cap_freeze_work_policy
    from modules.wms.placement_schemas import normalize_placement_request
    permissions = ("wms.operate", "wms.configure", "wms.approve")
    now = datetime.now(timezone.utc)
    day = _decision_day(now)
    seed = stock._caller_seed
    if seed is None or type(seed[7]) is not int or not 0 < seed[7] <= 2147483647 or type(seed[2]) is not str or not seed[2].strip():
        reject("work_original_managed_identity_required", 403)
    uid, sub, issued_at, roles = seed[7], seed[2], seed[6], tuple(seed[1])
    if issued_at is not None and (type(issued_at) is not int or issued_at < 0):
        reject("work_original_issued_at_invalid", 403)
    async def authority():
        await stock._fresh_capacity_authority(session, organization_id, actor, permissions)
        if actor != sub or stock.user.local_user_id != uid or stock.user.keycloak_user_id != sub or stock.user.issued_at != issued_at or tuple(stock.user.roles) != roles:
            reject("work_original_authority_changed", 403)
        _check_day(day)
    if not session.in_transaction():
        reject("work_route_transaction_required")
    with session.no_autoflush:
        connection = await session.connection()
        await connection.run_sync(require_capacity_read_committed)
        await authority()
        if actor != sub:
            reject("work_original_actor_mismatch", 403)
        request = normalize_placement_request(data)
        request_hash = digest({"organization_id": organization_id, "actor_user_id": uid, "actor_sub": sub, "request": request})
        async def replay(row):
            if row.warehouse_id != data.warehouse_id or row.kind != data.kind or row.request_hash != request_hash:
                reject("work_operation_identity_conflict")
            columns = {key: getattr(row, key) for key in OP_COLUMNS}
            original = await connection.run_sync(lambda conn: validate_work_receipt(conn, columns))
            original_approver = columns["result"][WORK_PRIVATE]["approver"]
            if original_approver["user_id"] != uid or original_approver["sub"] != sub:
                reject("work_original_receipt_owner_mismatch", 403)
            await protect_blind_projection(session, [data.warehouse_id])
            await authority()
            cap_freeze_work_policy(session.sync_session, original, {key: columns[key] for key in columns if key != "id"}, authority=authority, insert=False, persistent_pk=row.id)
            return work_ack(original)
        # Authentication precedes BOTH historical lookups. No lock/resource read
        # can accidentally turn a historical receipt into a current grant.
        existing = await session.scalar(select(PhysicalOperation).where(PhysicalOperation.organization_id == organization_id, PhysicalOperation.operation_id == str(data.operation_id)))
        if existing is not None:
            return await replay(existing)
        await control(session, data.warehouse_id, organization_id)
        await authority()
        existing = await session.scalar(select(PhysicalOperation).where(PhysicalOperation.organization_id == organization_id, PhysicalOperation.operation_id == str(data.operation_id)).execution_options(populate_existing=True))
        if existing is not None:
            return await replay(existing)
        snapshot = await collect_placement_snapshot(session, stock, organization_id, data.warehouse_id, actor, now)
        facts = snapshot.data()
        await protect_blind_projection(session, [data.warehouse_id])
        head = await connection.run_sync(lambda conn: measurement_state_head(conn, organization_id, data.warehouse_id))
        if head is None:
            reject("work_current_measured_context_missing")
        measurement = await session.scalar(select(PhysicalOperation).where(PhysicalOperation.organization_id == organization_id, PhysicalOperation.operation_id == head["operation_id"]))
        columns = {key: getattr(measurement, key) for key in OP_COLUMNS}
        proof = (await connection.run_sync(lambda conn: measurement_receipt(columns["result"], columns, conn)))["_placement_measured_context_v1"]
        await connection.run_sync(lambda conn: measurement_current_binding(conn, proof))
        scoped = [row for row in facts["work_policies"] if row["scope_code"] == data.work.scope_code]
        for previous in scoped:
            prior_operation = next((row for row in facts["operations"] if row["operation_id"] == previous["approval_operation_id"]), None)
            if prior_operation is None:
                reject("work_scope_history_receipt_missing")
            await connection.run_sync(lambda conn, prior=prior_operation: validate_work_receipt(conn, prior))
        revisions = sorted(row["revision"] for row in scoped)
        if revisions != list(range(1, len(scoped) + 1)) or data.expected_revision != len(scoped) or data.expected_revision == 2147483647:
            reject("work_revision_stale_or_history_gap")
        if any(row["id"] == str(data.policy_id) for row in facts["work_policies"]):
            reject("work_policy_identity_already_exists")
        equipment = None
        if data.work.access_method == "equipment":
            heads = await connection.run_sync(lambda conn: equipment_state_heads(conn, organization_id, data.warehouse_id))
            equipment = heads.get(str(data.work.equipment_resource_id))
            if equipment is None:
                reject("work_equipment_observation_missing")
            await connection.run_sync(lambda conn: equipment_current_binding(conn, equipment))
        operators = await _managed_operators(session, stock, organization_id, tuple(data.work.operator_subs))
        async def final_authority():
            await authority()
            if stock.user.local_user_id != uid or stock.user.keycloak_user_id != sub or stock.user.issued_at != issued_at or tuple(stock.user.roles) != roles:
                reject("work_original_authority_changed", 403)
            if await _managed_operators(session, stock, organization_id, tuple(data.work.operator_subs)) != operators:
                reject("work_operator_identity_changed", 403)
            await authority()
        approver = {"user_id": uid, "sub": sub, "issued_at": issued_at, "roles": list(roles), "approved_at": now.isoformat(timespec="microseconds").replace("+00:00", "Z")}
        row = derive_work_record(facts, request, proof, approver, revision=data.expected_revision + 1, equipment=equipment)
        if not (_timestamp(row["valid_from"]) <= now < _timestamp(row["valid_until"])):
            reject("work_approval_window_not_current")
        predecessor = facts["operations"][-1]["id"] if facts["operations"] else None
        if type(predecessor) is not int or predecessor < measurement.id:
            reject("work_capture_predecessor_or_measurement_order")
        capture = make_work_location_capture(facts, row, approver, predecessor)
        _work_capture_shape(capture, row, approver)
        await connection.run_sync(lambda conn: _work_capture_current_rows(conn, capture))
        receipt = make_work_receipt(row, request, approver, operators, equipment, location_capture=capture)
        await final_authority()
        cap_freeze_work_policy(session.sync_session, row, receipt, authority=final_authority, insert=True)
        await connection.run_sync(lambda conn: _work_capture_current_rows(conn, capture))
        values = {**row, **{key: _timestamp(row[key]) for key in ("valid_from", "valid_until", "approved_at")}}
        session.add(PlacementWorkPolicy(**values))
        # Row-before-receipt is legal in this TX, but never at root commit.
        await session.flush()
        session.add(PhysicalOperation(**receipt))
        await session.flush()
        await final_authority()
        return work_ack(row)


async def preview_placement(session, stock, organization_id, data, actor):
    from modules.wms.physical_stock import protect_blind_projection
    from modules.wms.physical_placement import _plain
    from modules.wms.placement_policy_codec import decode_placement_work_policy
    now = datetime.now(timezone.utc)
    snapshot = await collect_placement_snapshot(session, stock, organization_id, data.warehouse_id, actor, now)
    current = snapshot.data()
    await protect_blind_projection(session, [data.warehouse_id])
    row = next((row for row in current["work_policies"] if row["id"] == str(data.work_policy_id)), None)
    if row is None:
        reject("placement_work_policy_missing")
    receipt = next((operation for operation in current["operations"] if operation["operation_id"] == row["approval_operation_id"]), None)
    if receipt is None:
        reject("placement_work_approval_receipt_missing")
    connection = await session.connection()
    original = await connection.run_sync(lambda conn: validate_work_receipt(conn, receipt))
    if canonical(original) != canonical(row):
        reject("placement_original_work_policy_changed")
    proof = _measurement_binding(original)
    equipment = receipt["result"][WORK_PRIVATE]["equipment_receipt"]
    await connection.run_sync(lambda conn: validate_current_work_policy(conn, original, equipment))
    operators = await _managed_operators(session, stock, organization_id, tuple(original["policy"]["operator_subs"]))
    if operators != receipt["result"][WORK_PRIVATE]["operators"]:
        reject("placement_current_operator_identity_stale", 403)
    await stock._fresh_capacity_authority(session, organization_id, actor, ("wms.operate",))
    # Full original approval/proof is part of the current commitment, separate
    # from the immutable original policy digest and the transient compiler seal.
    snapshot = freeze_snapshot({**current, "selected_work_policy": original, "selected_work_receipt": receipt,
                                "selected_measured_context": proof, "current_operators": operators})
    decoded = decode_placement_work_policy({**original, **{key: _timestamp(original[key])
        for key in ("valid_from", "valid_until", "approved_at")}})
    facts = compile_placement_facts(snapshot, decoded, proof, tuple(item["sub"] for item in operators))
    query = Query(2, data.warehouse_id, tuple(str(value) for value in data.carrier_ids), tuple(data.candidate_location_ids), str(data.work_policy_id))
    result = evaluate_current(facts, query, now_utc=now)
    await stock._fresh_capacity_authority(session, organization_id, actor, ("wms.operate",))
    _check_day(_decision_day(now))
    if session.new or session.dirty or session.deleted:
        reject("placement_preview_must_remain_read_only")
    return _plain(result)


PLACEMENT_KINDS = frozenset({"capacity_placement_assign", "capacity_placement_begin",
                             "capacity_placement_complete", "capacity_placement_cancel"})
PLACEMENT_PRIVATE = {"capacity_placement_assign": "_placement_assign_v1",
                     "capacity_placement_begin": "_placement_begin_v1",
                     "capacity_placement_complete": "_placement_complete_v1",
                     "capacity_placement_cancel": "_placement_cancel_v1"}
ASSIGNMENT_KEYS = frozenset({"version", "evaluation", "choice", "task", "assigned", "executor",
                            "before", "after_own_holds", "native_owned_holds", "bindings"})


def placement_content_digest(frame):
    """Carrier-local commitment; historical full observation hashes stay intact."""
    return digest({key: frame[key] for key in ("carrier", "members", "positions")})


def _frame_shape(frame):
    from modules.wms.placement_policy_codec import _measured_native_frame
    _measured_native_frame(frame, frame["carrier"]["warehouse_id"])
    return frame


def _native_frame(data, carrier_id):
    carrier = next((row for row in data["carriers"] if row["id"] == carrier_id), None)
    if carrier is None:
        raise ValueError("placement_carrier_missing")
    return _observation_frame_values(carrier, data["positions"], data["units"], carrier["location_id"])


def _native_dimensions(row):
    from types import SimpleNamespace
    from modules.wms.physical_stock import dimensions
    converted = dict(row)
    for key in ("last_checked", "next_service"):
        if isinstance(converted[key], str):
            converted[key] = date.fromisoformat(converted[key])
    if isinstance(converted["arrived_at"], str):
        converted["arrived_at"] = datetime.fromisoformat(converted["arrived_at"])
    return dimensions(SimpleNamespace(**converted))


def _pure_assignment(evaluation, *, task_status, assignee_sub, state, current_members,
                     current_holds, action):
    from modules.wms.physical_placement import Assignment, Claim, Hold
    value = dict(evaluation)
    for name in ("task_qty", "gross_kg", "tare_kg"):
        value[name] = _decimal(value[name])
    value["source_path"] = tuple(value["source_path"])
    value["phase_paths"] = tuple(tuple(path) for path in value["phase_paths"])
    value["member_vector"] = tuple((row[0], row[1], row[2], Decimal(row[3])) for row in value["member_vector"])
    value["owned_holds"] = tuple(Hold(row["hold_id"], row["unit_id"], row["sku"], row["lot"], Decimal(row["qty"]))
                                 for row in value["owned_holds"])
    value["claims"] = tuple(Claim(**{**row, "slot_keys": tuple(row["slot_keys"])}) for row in value["claims"])
    value.update(task_status=task_status, assignee_sub=assignee_sub, state=state,
                 current_member_vector=current_members, current_owned_holds=current_holds,
                 action=action, canonical_payload="", digest="", expected_digest="")
    assignment = Assignment(**value)
    text = _canonical_payload(assignment)
    return replace(assignment, canonical_payload=text, digest=_sha(text), expected_digest=_sha(text))


def validate_assignment_snapshot(row):
    """Exact159 header and native before/hold transition; no model-fixture bypass."""
    from modules.wms.physical_placement import Assignment, Claim, Hold, _plain
    from modules.wms.physical_capacity import _measurement_db_row
    row = _measurement_db_row(PlacementAssignment, row)
    body = _closed(row["snapshot"], ASSIGNMENT_KEYS, "placement_assignment_snapshot_shape")
    if type(body["version"]) is not int or body["version"] != 1:
        raise ValueError("placement_assignment_snapshot_version")
    text = canonical(body)
    if row["canonical_payload"] != text or row["digest"] != _sha(text):
        raise ValueError("placement_assignment_header_digest")
    omitted = {"task_status", "assignee_sub", "state", "current_member_vector", "current_owned_holds",
               "action", "canonical_payload", "digest", "expected_digest"}
    expected = {field.name for field in fields(Assignment)} - omitted
    evaluation = _closed(body["evaluation"], expected, "placement_assignment_evaluation_shape")
    _closed(body["executor"], {"user_id", "sub"}, "placement_assignment_executor_shape")
    _closed(body["assigned"], {"user_id", "sub", "at"}, "placement_assignment_actor_shape")
    if (type(body["assigned"]["user_id"]) is not int or not 0 < body["assigned"]["user_id"] <= 2147483647
            or {key: body["assigned"][key] for key in ("user_id", "sub")} != body["executor"]
            or body["assigned"]["sub"] != row["assigned_by"]
            or _timestamp(body["assigned"]["at"]) != _timestamp(row["assigned_at"])):
        raise ValueError("placement_assignment_actor_binding")
    before, after = _frame_shape(body["before"]), _frame_shape(body["after_own_holds"])
    if before["carrier"] is None or before["carrier"]["id"] != row["carrier_id"]:
        raise ValueError("placement_assignment_carrier_frame")
    holds = body["native_owned_holds"]
    if type(holds) is not list or [hold["id"] for hold in holds] != sorted({hold["id"] for hold in holds}):
        raise ValueError("placement_assignment_hold_order")
    native_keys = {column.name for column in PhysicalHold.__table__.columns}
    members = {member["id"]: member for member in before["members"]}
    hold_units = set()
    expected_after = json.loads(canonical(before))
    for hold in holds:
        _closed(hold, native_keys, "placement_assignment_native_hold_shape")
        member = members.get(hold["unit_id"])
        if (type(hold["id"]) is not int or hold["id"] <= 0 or member is None or hold["unit_id"] in hold_units
                or hold["warehouse_id"] != row["warehouse_id"]
                or hold["source"] != "placement:" + row["task_id"] or hold["order_ref"] != hold["source"]
                or Decimal(hold["qty"]) <= 0 or Decimal(hold["qty"]) != Decimal(member["qty"])
                or Decimal(member["reserved"]) != 0
                or any(Decimal(hold[key]) != 0 for key in ("picked", "shipped", "consumed", "released"))
                or hold["container"] != member["container"] or hold["min_shelf_days"] != 0):
            raise ValueError("placement_assignment_native_hold_binding")
        hold_units.add(hold["unit_id"])
        for name in ("members", "location_units"):
            matching = [unit for unit in expected_after[name] if unit["id"] == hold["unit_id"]]
            if len(matching) != 1:
                raise ValueError("placement_assignment_hold_unit_scope")
            matching[0]["reserved"] = format(Decimal(hold["qty"]), ".6f")
    if hold_units != {key for key, member in members.items() if Decimal(member["qty"]) > 0}:
        raise ValueError("placement_assignment_hold_members")
    if canonical(expected_after) != canonical(after):
        raise ValueError("placement_assignment_after_hold_frame")
    if placement_content_digest(after) != row["source_content_digest"]:
        raise ValueError("placement_assignment_local_content_digest")
    choice = body["choice"]
    from modules.wms.placement_schemas import PlacementChoice, normalize_placement_request
    if normalize_placement_request(PlacementChoice.model_validate(choice)) != choice:
        raise ValueError("placement_assignment_choice_canonical")
    pairs = {"task_id": "task_id", "carrier_id": "carrier_id", "destination_location_id": "destination_location_id",
             "orientation": "orientation", "destination_profile_id": "destination_profile_id", "work_policy_id": "work_policy_id"}
    if any(choice[key] != row[column] for key, column in pairs.items()):
        raise ValueError("placement_assignment_choice_header")
    if (choice["source_content_digest"] != placement_content_digest(before)
            or choice["source_content_revision"] != row["source_content_revision"]):
        raise ValueError("placement_assignment_choice_source")
    binding_keys = {"measurement_operation_id", "measurement_digest", "work_policy", "profile_bindings",
                    "packaging_profile", "topology"}
    bindings = _closed(body["bindings"], binding_keys, "placement_assignment_bindings_shape")
    if bindings["work_policy"]["id"] != row["work_policy_id"]:
        raise ValueError("placement_assignment_work_binding")
    task_keys = {column.name for column in PhysicalTask.__table__.columns} - {"status", "assignee", "evidence"}
    task = _closed(body["task"], task_keys, "placement_assignment_task_shape")
    if (task["id"] != row["task_id"] or task["warehouse_id"] != row["warehouse_id"]
            or task["kind"] != "cap_place" or any(task[key] is not None for key in ("qty", "unit_id", "hold_id"))
            or task["destination_id"] != row["destination_location_id"]):
        raise ValueError("placement_assignment_task_binding")
    pure_holds = tuple(Hold(hold["id"], hold["unit_id"], members[hold["unit_id"]]["sku_code"],
                            next(v[2] for v in evaluation["member_vector"] if v[0] == hold["unit_id"]), Decimal(hold["qty"]))
                       for hold in holds)
    vector = tuple((v[0], v[1], v[2], Decimal(v[3])) for v in evaluation["member_vector"])
    pure = _pure_assignment(evaluation, task_status="open", assignee_sub=body["executor"]["sub"],
                            state="assigned", current_members=vector, current_holds=pure_holds, action="begin")
    if _plain(pure.owned_holds) != _plain(pure_holds):
        raise ValueError("placement_assignment_pure_hold_binding")
    header = {"task_id": pure.task_id, "organization_id": pure.organization_id, "warehouse_id": pure.warehouse_id,
              "carrier_id": pure.carrier_id, "source_location_id": pure.source_location_id,
              "destination_location_id": pure.destination_location_id, "source_profile_id": pure.source_profile_id,
              "destination_profile_id": pure.destination_profile_id, "packaging_profile_id": pure.packaging_profile_id,
              "work_policy_id": pure.work_policy_id, "source_content_revision": pure.source_revision,
              "source_content_digest": pure.source_digest, "gross_kg": pure.gross_kg, "tare_kg": pure.tare_kg}
    if canonical(_plain(header)) != canonical({key: row[key] for key in header}):
        raise ValueError("placement_assignment_pure_header_binding")
    if len(pure.claims) != 1 or pure.claims[0] != Claim(pure.task_id, pure.carrier_id, pure.destination_location_id,
            pure.destination_profile_id, tuple(choice["slot_keys"]), choice["orientation"], pure.claims[0].frame_id,
            pure.claims[0].support_carrier_id):
        raise ValueError("placement_assignment_claim_binding")
    return body, pure


def decode_active_assignments(data, *, action=None, own_task_id=None):
    """Current dynamic fields come only from locked rows, immutable fields from159."""
    from modules.wms.physical_placement import Hold
    tasks = {row["id"]: row for row in data["tasks"]}
    holds = {row["id"]: row for row in data["holds"]}
    evidence = {row["unit_id"]: row for row in data["unit_evidence"]}
    active = []
    logical_claims = []
    for row in data["assignments"]:
        if row["state"] not in ACTIVE:
            continue
        body, original = validate_assignment_snapshot(row)
        task = tasks.get(row["task_id"])
        if task is None or canonical({key: task[key] for key in body["task"]}) != canonical(body["task"]):
            raise ValueError("placement_assignment_current_task_binding")
        if row["active_carrier_id"] != row["carrier_id"]:
            raise ValueError("placement_assignment_active_identity")
        native_claims = sorted((claim for claim in data["claims"] if claim["task_id"] == row["task_id"]),
                               key=lambda claim: claim["ordinal"])
        expected_claim = original.claims[0]
        if ([claim["ordinal"] for claim in native_claims] != list(range(len(expected_claim.slot_keys)))
                or tuple(claim["slot_key"] for claim in native_claims) != expected_claim.slot_keys
                or any((claim["warehouse_id"], claim["location_id"], claim["profile_id"]) !=
                       (row["warehouse_id"], row["destination_location_id"], row["destination_profile_id"])
                       for claim in native_claims)):
            raise ValueError("placement_assignment_current_claim_binding")
        current_holds = []
        for expected in body["native_owned_holds"]:
            current = holds.get(expected["id"])
            if current is None or canonical(current) != canonical(expected):
                raise ValueError("placement_assignment_current_hold_binding")
            old = next(hold for hold in original.owned_holds if hold.hold_id == current["id"])
            current_holds.append(Hold(old.hold_id, old.unit_id, old.sku, old.lot, Decimal(current["qty"])))
        frame = _native_frame(data, row["carrier_id"])
        members = tuple((unit["id"], unit["sku_code"], evidence[unit["id"]]["effective_lot"]["lot_no"], Decimal(unit["qty"]))
                        for unit in frame["members"] if evidence[unit["id"]]["effective_lot"] is not None)
        pure = replace(original, task_status=task["status"], assignee_sub=task["assignee"], state=row["state"],
                       current_member_vector=members, current_owned_holds=tuple(current_holds),
                       action=action if row["task_id"] == own_task_id and action in {"begin", "complete"}
                       else ("complete" if row["state"] == "in_transit" else "begin"))
        active.append(pure)
        logical_claims.append(expected_claim)
    if {claim["task_id"] for claim in data["claims"]} - {row.task_id for row in active}:
        raise ValueError("placement_orphan_native_claim")
    return tuple(active), tuple(logical_claims)


def placement_ack(proof):
    answer = {key: proof[key] for key in ("operation_id", "kind", "warehouse_id")}
    answer["protocol"] = 2
    if proof["kind"] == "capacity_placement_assign":
        answer["status"] = "assigned"
        answer["tasks"] = [{"task_id": item["task_id"], "assignment_digest": item["digest"]}
                           for item in proof["assignments"]]
    else:
        answer.update(task_id=proof["task_id"], assignment_digest=proof["assignment_digest"],
                      status={"capacity_placement_begin": "in_transit", "capacity_placement_complete": "completed",
                              "capacity_placement_cancel": "canceled"}[proof["kind"]])
    return answer


def _placement_operation(connection, organization_id, warehouse_id, operation_id):
    table = PhysicalOperation.__table__
    row = connection.execute(select(table).where(table.c.organization_id == organization_id,
        table.c.warehouse_id == warehouse_id, table.c.operation_id == operation_id)).mappings().one_or_none()
    if row is None:
        raise ValueError("placement_historical_receipt_missing")
    return dict(row)


def validate_placement_envelope(row):
    """Closed typed private dispatch, including corrupt/null private indicators."""
    kind, result = row["kind"], row["result"]
    if kind not in PLACEMENT_KINDS or type(result) is not dict:
        raise ValueError("placement_receipt_kind_or_result")
    key = PLACEMENT_PRIVATE[kind]
    if any(marker in result for marker in set(PLACEMENT_PRIVATE.values()) - {key}):
        raise ValueError("placement_receipt_private_dispatch")
    proof = result.get(key)
    common = {"version", "organization_id", "warehouse_id", "operation_id", "kind", "request_hash", "request", "actor"}
    extras = {"capacity_placement_assign": {"assignments"},
              "capacity_placement_begin": {"task_id", "assignment_digest", "measurement_operation_id", "measurement_digest"},
              "capacity_placement_complete": {"task_id", "assignment_digest", "measurement_operation_id", "measurement_digest",
                  "source_before", "destination_before_units", "destination_after"},
              "capacity_placement_cancel": {"task_id", "assignment_digest", "measurement_operation_id", "measurement_digest",
                  "source_before", "source_after"}}[kind]
    _closed(proof, common | extras, "placement_private_proof_shape")
    _closed(proof["actor"], {"user_id", "sub"}, "placement_receipt_actor_shape")
    if (type(proof["version"]) is not int or proof["version"] != 1
            or type(proof["actor"]["user_id"]) is not int or not 0 < proof["actor"]["user_id"] <= 2147483647
            or type(proof["actor"]["sub"]) is not str or proof["actor"]["sub"] != row["actor"]):
        raise ValueError("placement_receipt_original_actor")
    parsed = _PLACEMENT_COMMAND_ADAPTER.validate_python(proof["request"])
    if normalize_placement_request(parsed) != proof["request"] or parsed.kind != kind:
        raise ValueError("placement_receipt_request_canonical")
    hashed = digest({"organization_id": proof["organization_id"], "actor_user_id": proof["actor"]["user_id"],
                     "actor_sub": proof["actor"]["sub"], "request": proof["request"]})
    if hashed != proof["request_hash"] or any(proof[key] != row[key] for key in
            ("organization_id", "warehouse_id", "operation_id", "kind", "request_hash")):
        raise ValueError("placement_receipt_scope_hash")
    if proof["request"]["operation_id"] != proof["operation_id"] or proof["request"]["warehouse_id"] != proof["warehouse_id"]:
        raise ValueError("placement_receipt_request_scope")
    expected_keys = set(placement_ack(proof)) | {key}
    if kind == "capacity_placement_complete":
        expected_keys.add("physical_causal_v1")
    if set(result) != expected_keys or canonical({name: result[name] for name in placement_ack(proof)}) != canonical(placement_ack(proof)):
        raise ValueError("placement_receipt_public_ack")
    return proof


def validate_placement_receipt(connection, operation, *, trail=frozenset()):
    """Actual scoped PK/order, immutable159 and whole native ledger packet."""
    from modules.wms.physical_models import PhysicalLedger
    if type(operation.get("id")) is not int or operation["id"] <= 0 or operation["id"] in trail:
        raise ValueError("placement_receipt_persistent_identity")
    actual = _placement_operation(connection, operation["organization_id"], operation["warehouse_id"], operation["operation_id"])
    if canonical({key: actual[key] for key in OP_COLUMNS}) != canonical({key: operation[key] for key in OP_COLUMNS}):
        raise ValueError("placement_receipt_persisted_row_mismatch")
    proof = validate_placement_envelope(actual)
    rows = connection.execute(select(PhysicalLedger.__table__).where(
        PhysicalLedger.__table__.c.operation_pk == actual["id"]).order_by(PhysicalLedger.__table__.c.id)).mappings().all()
    if proof["kind"] == "capacity_placement_assign":
        assigned = proof["assignments"]
        if type(assigned) is not list or len(assigned) != len(proof["request"]["choices"]) or rows:
            raise ValueError("placement_assign_receipt_packet")
        for item, choice in zip(assigned, proof["request"]["choices"]):
            _closed(item, {"task_id", "snapshot", "canonical_payload", "digest"}, "placement_assign_task_receipt_shape")
            table = PlacementAssignment.__table__
            header = connection.execute(select(table).where(table.c.task_id == item["task_id"],
                table.c.organization_id == actual["organization_id"], table.c.warehouse_id == actual["warehouse_id"])).mappings().one_or_none()
            if header is None or header["assignment_operation_id"] != actual["operation_id"]:
                raise ValueError("placement_assign_header_receipt_binding")
            body, _ = validate_assignment_snapshot(dict(header))
            if (canonical(body) != canonical(item["snapshot"]) or header["canonical_payload"] != item["canonical_payload"]
                    or header["digest"] != item["digest"] or canonical(body["choice"]) != canonical(choice)
                    or body["assigned"]["user_id"] != proof["actor"]["user_id"] or body["assigned"]["sub"] != actual["actor"]):
                raise ValueError("placement_assign_receipt_header_correspondence")
            _validate_assignment_historical_bindings(connection, body, actual["id"], actual["organization_id"], actual["warehouse_id"])
        return proof
    table = PlacementAssignment.__table__
    header = connection.execute(select(table).where(table.c.task_id == proof["task_id"],
        table.c.organization_id == actual["organization_id"], table.c.warehouse_id == actual["warehouse_id"])).mappings().one_or_none()
    if header is None or header["digest"] != proof["assignment_digest"] or proof["request"]["task_id"] != proof["task_id"] or proof["request"]["assignment_digest"] != header["digest"]:
        raise ValueError("placement_action_header_digest")
    body, _ = validate_assignment_snapshot(dict(header))
    if body["executor"] != proof["actor"] or (proof["measurement_operation_id"], proof["measurement_digest"]) != (
            body["bindings"]["measurement_operation_id"], body["bindings"]["measurement_digest"]):
        raise ValueError("placement_action_original_executor_or_measurement")
    assign = _placement_operation(connection, actual["organization_id"], actual["warehouse_id"], header["assignment_operation_id"])
    if assign["id"] >= actual["id"]:
        raise ValueError("placement_action_causal_order")
    validate_placement_receipt(connection, assign, trail=trail | {actual["id"]})
    if proof["kind"] == "capacity_placement_begin":
        if header["begin_operation_id"] != actual["operation_id"] or rows:
            raise ValueError("placement_begin_receipt_binding")
        return proof
    if header["settlement_operation_id"] != actual["operation_id"]:
        raise ValueError("placement_settlement_receipt_binding")
    if proof["kind"] == "capacity_placement_cancel":
        if header["begin_operation_id"] is not None or rows:
            raise ValueError("placement_cancel_after_begin_or_ledger")
        before, after = _frame_shape(proof["source_before"]), _frame_shape(proof["source_after"])
        _validate_hold_release_frame(body, before, after)
        return proof
    begin = _placement_operation(connection, actual["organization_id"], actual["warehouse_id"], header["begin_operation_id"])
    if not assign["id"] < begin["id"] < actual["id"]:
        raise ValueError("placement_complete_causal_order")
    begin_proof = validate_placement_receipt(connection, begin, trail=trail | {actual["id"]})
    if (begin_proof["kind"] != "capacity_placement_begin"
            or any(begin_proof[key] != proof[key] for key in (
                "task_id", "assignment_digest", "measurement_operation_id", "measurement_digest", "actor"))):
        raise ValueError("placement_complete_begin_assignment_binding")
    before, after = _frame_shape(proof["source_before"]), _frame_shape(proof["destination_after"])
    if placement_content_digest(before) != header["source_content_digest"]:
        raise ValueError("placement_complete_source_content")
    source_carrier = before["carrier"]
    destination_carrier = {**source_carrier, "location_id": header["destination_location_id"],
                           "content_revision": source_carrier["content_revision"] + 1,
                           "confirmed_content_revision": source_carrier["content_revision"] + 1}
    destination_members = [{**member, "location_id": header["destination_location_id"], "reserved": "0.000000"}
                           for member in before["members"]]
    positions = [{"carrier_id": header["carrier_id"], "ordinal": ordinal, "warehouse_id": header["warehouse_id"],
                  "location_id": header["destination_location_id"], "location_profile_id": header["destination_profile_id"],
                  "slot_key": key} for ordinal, key in enumerate(body["choice"]["slot_keys"])]
    destination_before = proof["destination_before_units"]
    if type(destination_before) is not list or {unit["id"] for unit in destination_before} & {unit["id"] for unit in destination_members}:
        raise ValueError("placement_complete_destination_before")
    expected_after = {"carrier": destination_carrier, "members": destination_members, "positions": positions,
                      "location_units": sorted(destination_before + destination_members, key=lambda unit: unit["id"])}
    if canonical(after) != canonical(expected_after):
        raise ValueError("placement_complete_native_transition")
    expected = []
    for member, moved in zip(before["members"], destination_members):
        quantity = Decimal(member["qty"])
        if quantity > 0:
            expected.extend([(member["id"], -quantity, {**_native_dimensions(member), "carrier_id": header["carrier_id"]}),
                             (member["id"], quantity, {**_native_dimensions(moved), "carrier_id": header["carrier_id"]})])
    if len(rows) != len(expected) or any((row["unit_id"], Decimal(row["qty_delta"]), canonical(row["dimensions"])) !=
            (unit, quantity, canonical(dimensions)) for row, (unit, quantity, dimensions) in zip(rows, expected)):
        raise ValueError("placement_complete_whole_ledger_packet")
    causal = _closed(actual["result"]["physical_causal_v1"],
        {"schema_version", "canonical_request", "births", "count_basis", "prior_frontiers"}, "placement_complete_causal_shape")
    if (type(causal["schema_version"]) is not int or causal["schema_version"] != 2 or causal["births"] != []
            or causal["count_basis"] != [] or canonical(causal["canonical_request"]) != canonical(proof["request"])
            or type(causal["prior_frontiers"]) is not dict or set(causal["prior_frontiers"]) != {unit["id"] for unit in before["members"]}):
        raise ValueError("placement_complete_causal_members")
    from modules.wms.item_identity import _origin_frontier_tokens
    for unit_id, frontier in causal["prior_frontiers"].items():
        _origin_frontier_tokens(frontier)
        if canonical(frontier) != canonical(_placement_prior_frontier(connection, actual["organization_id"],
                actual["warehouse_id"], unit_id, actual["id"])):
            raise ValueError("placement_complete_prior_frontier_not_whole")
    return proof


def _validate_assignment_historical_bindings(connection, body, assign_pk, org, wh):
    bindings = body["bindings"]
    work = bindings["work_policy"]
    approval = _placement_operation(connection, org, wh, work["approval_operation_id"])
    if canonical(_validate_work_neutral(connection, approval, before_pk=assign_pk)) != canonical(work):
        raise ValueError("placement_assignment_original_policy_changed")
    proof = _measurement_binding(work)
    measurement = _placement_operation(connection, org, wh, bindings["measurement_operation_id"])
    if not measurement["id"] < approval["id"] < assign_pk or proof["digest"] != bindings["measurement_digest"]:
        raise ValueError("placement_assignment_measurement_order")
    if canonical(bindings["profile_bindings"]) != canonical(proof["profile_bindings"]) or canonical(bindings["topology"]) != canonical(proof["topology"]):
        raise ValueError("placement_assignment_measured_bindings")
    package = bindings["packaging_profile"]
    table = PlacementProfile.__table__
    actual = connection.execute(select(table).where(table.c.id == package["id"], table.c.organization_id == org,
        table.c.warehouse_id == wh)).mappings().one_or_none()
    if actual is None:
        raise ValueError("placement_assignment_packaging_profile_missing")
    _cap_full_snapshot(connection, dict(actual))
    if canonical(_policy_profile(actual)) != canonical(package):
        raise ValueError("placement_assignment_packaging_profile_changed")


def _policy_profile(row):
    return {**json.loads(row["canonical_payload"])["profile"], "canonical_payload": row["canonical_payload"],
            "digest": row["digest"], "sealed": row["sealed"]}


def _validate_work_neutral(connection, row, *, before_pk=None):
    from modules.wms.physical_models import PhysicalLedger
    actual = _placement_operation(connection, row["organization_id"], row["warehouse_id"], row["operation_id"])
    if type(row.get("id")) is not int or actual["id"] != row["id"] or (before_pk is not None and row["id"] >= before_pk):
        raise ValueError("placement_work_actual_pk_order")
    if canonical({key: row[key] for key in OP_COLUMNS}) != canonical({key: actual[key] for key in OP_COLUMNS}):
        raise ValueError("placement_work_actual_row")
    validated = validate_work_receipt(connection, actual)
    if connection.execute(select(PhysicalLedger.__table__.c.id).where(
            PhysicalLedger.__table__.c.operation_pk == actual["id"])).first() is not None:
        raise ValueError("placement_work_nonzero_ledger_packet")
    return validated


def _validate_hold_release_frame(body, before, after):
    expected = json.loads(canonical(before))
    if placement_content_digest(before) != placement_content_digest(body["after_own_holds"]):
        raise ValueError("placement_cancel_source_changed")
    for hold in body["native_owned_holds"]:
        for name in ("members", "location_units"):
            unit = next((row for row in expected[name] if row["id"] == hold["unit_id"]), None)
            if unit is None or Decimal(unit["reserved"]) != Decimal(hold["qty"]):
                raise ValueError("placement_cancel_hold_conservation")
            unit["reserved"] = "0.000000"
    if canonical(expected) != canonical(after):
        raise ValueError("placement_cancel_release_transition")


def placement_ledger_dimensions(connection, ledger, operation, *, before_pk=None):
    """Strict166 D17 adapter; the persisted ledger is never rewritten."""
    from modules.wms.item_identity import _ORIGIN_DIMENSION_KEYS
    event = validate_placement_receipt(connection, operation)
    if event["kind"] != "capacity_placement_complete" or (before_pk is not None and operation["id"] >= before_pk):
        raise ValueError("placement_ledger_receipt_kind_or_order")
    dimensions = ledger["dimensions"]
    if type(dimensions) is not dict or set(dimensions) != set(_ORIGIN_DIMENSION_KEYS) | {"carrier_id"}:
        raise ValueError("placement_ledger_d17_shape")
    if dimensions["carrier_id"] != event["source_before"]["carrier"]["id"]:
        raise ValueError("placement_ledger_carrier_identity")
    member = next((unit for unit in event["source_before"]["members"] if unit["id"] == ledger["unit_id"]), None)
    moved = next((unit for unit in event["destination_after"]["members"] if unit["id"] == ledger["unit_id"]), None)
    if member is None or moved is None or Decimal(member["qty"]) <= 0:
        raise ValueError("placement_ledger_positive_member")
    quantity = Decimal(ledger["qty_delta"])
    expected = _native_dimensions(member if quantity < 0 else moved)
    if quantity not in {-Decimal(member["qty"]), Decimal(member["qty"])} or canonical(dimensions) != canonical({**expected, "carrier_id": dimensions["carrier_id"]}):
        raise ValueError("placement_ledger_historical_pair")
    return json.loads(canonical(expected))


def _placement_prior_frontier(connection, org, wh, unit_id, before_pk):
    """Native token codec over immutable rows before an ACTUAL persisted PK."""
    from modules.wms.physical_models import PhysicalLedger
    from modules.wms.physical_stock import _origin_operation_row, _origin_count_observations
    from modules.wms.placement_models import placement_operation_indicator
    ledger, operation = PhysicalLedger.__table__, PhysicalOperation.__table__
    op_names = ("id", "organization_id", "operation_id", "warehouse_id", "kind", "request_hash", "result", "actor", "created_at")
    rows = connection.execute(select(ledger.c.id, ledger.c.operation_pk, ledger.c.unit_id,
        ledger.c.qty_delta, ledger.c.dimensions, *(operation.c[name] for name in op_names)).select_from(ledger)
        .outerjoin(operation, operation.c.id == ledger.c.operation_pk).where(ledger.c.unit_id == unit_id,
            (operation.c.id < before_pk) | operation.c.id.is_(None))).all()
    frontier = {"ledgers": [], "observations": [], "opaque_counts": []}
    for row in rows:
        if row[5] is None:
            raise ValueError('placement_prior_frontier_orphan_ledger')
        op = _origin_operation_row(row[5:])
        payload = {"ledger_id": row[0], "operation_pk": row[1], "unit_id": row[2], "qty_delta": str(row[3]),
                   "dimensions": row[4], "operation_id": op["operation_id"], "organization_id": op["organization_id"],
                   "request_hash": op["request_hash"]}
        frontier["ledgers"].append({"ledger_id": row[0], "row_digest": digest(payload)})
    rows = connection.execute(select(*(operation.c[name] for name in op_names)).where(
        operation.c.organization_id == org, operation.c.id < before_pk).order_by(operation.c.id)).all()
    for raw in rows:
        op = _origin_operation_row(raw)
        if op["kind"] == "count_approve":
            observations = _origin_count_observations(op, unit_id)
            if observations is None:
                frontier["opaque_counts"].append({"operation_pk": op["id"], "operation_id": op["operation_id"], "row_digest": digest(op)})
            else:
                frontier["observations"].extend(observations)
        if op["warehouse_id"] == wh and placement_operation_indicator(op["kind"], op["result"]):
            event = validate_placement_receipt(connection, op)
            if event["kind"] == "capacity_placement_complete":
                member = next((row for row in event["source_before"]["members"] if row["id"] == unit_id), None)
                if member is not None and Decimal(member["qty"]) == 0:
                    frontier["observations"].append({"kind": "placement_zero_v1", "operation_pk": op["id"],
                        "operation_id": op["operation_id"], "unit_id": unit_id, "row_digest": digest(op)})
    for values in frontier.values():
        values.sort(key=canonical)
    return frontier


def placement_measurement_current_view(connection, proof, *, before_pk=None, current=True, include_locations=False):
    """Separate163 view. ALL actual post-M PKs must have verified transitions."""
    from modules.wms.physical_capacity import _measurement_current_binding
    from modules.wms.placement_models import placement_operation_indicator, work_operation_indicator
    measurement = _placement_operation(connection, proof["organization_id"], proof["warehouse_id"], proof["operation_id"])
    historical = measurement_receipt(measurement["result"], measurement, connection)["_placement_measured_context_v1"]
    if canonical(historical) != canonical(proof):
        raise ValueError("placement_measurement_original_receipt")
    table = PhysicalOperation.__table__
    statement = select(table).where(table.c.organization_id == proof["organization_id"],
        table.c.warehouse_id == proof["warehouse_id"], table.c.id > measurement["id"])
    if before_pk is not None:
        statement = statement.where(table.c.id < before_pk)
    operations = connection.execute(statement.order_by(table.c.id)).mappings().all()
    frames = {row["carrier_id"]: json.loads(canonical(row["after"])) for row in proof["carrier_observations"]}
    locations = {}
    latest_capture, changed_tasks = None, set()
    for frame in frames.values():
        location_id = frame["carrier"]["location_id"]
        known = locations.setdefault(location_id, {"location_id": location_id,
            "units": frame["location_units"], "carriers": [], "positions": []})
        if canonical(known["units"]) != canonical(frame["location_units"]):
            raise ValueError("placement_causal_measured_location_conflict")
        known["carriers"].append(frame["carrier"])
        known["positions"].extend(frame["positions"])
        known["carriers"].sort(key=lambda carrier: carrier["id"])
        known["positions"].sort(key=lambda position: (position["carrier_id"], position["ordinal"]))
    def check_before(frame):
        known = frames.get(frame["carrier"]["id"])
        if known is None or canonical(known) != canonical(frame):
            raise ValueError("placement_causal_full_frame_before")
    def replace_units(changes, *, remove=(), destination=None):
        for frame in frames.values():
            location = frame["carrier"]["location_id"]
            frame["members"] = [changes.get(unit["id"], unit) for unit in frame["members"] if unit["id"] not in remove]
            frame["location_units"] = [changes.get(unit["id"], unit) for unit in frame["location_units"] if unit["id"] not in remove]
            if destination == location:
                by_id = {unit["id"]: unit for unit in frame["location_units"]}
                by_id.update(changes)
                frame["location_units"] = sorted(by_id.values(), key=lambda unit: unit["id"])
        for location_id, frame in locations.items():
            frame["units"] = [changes.get(unit["id"], unit) for unit in frame["units"] if unit["id"] not in remove]
            if destination == location_id:
                by_id = {unit["id"]: unit for unit in frame["units"]}
                by_id.update(changes)
                frame["units"] = sorted(by_id.values(), key=lambda unit: unit["id"])
    for row in operations:
        row = dict(row)
        if work_operation_indicator(row["kind"], row["result"]):
            _validate_work_neutral(connection, row)
            private = row["result"][WORK_PRIVATE]
            if private["protocol"] == 2:
                capture = private["location_capture"]
                captured = _work_capture_shape(capture, private["policy_row"], private["approver"])
                if (capture["measurement_operation_id"], capture["measurement_digest"]) != (proof["operation_id"], proof["digest"]):
                    raise ValueError("placement_causal_capture_measurement")
                for location_id, full in captured.items():
                    known = locations.get(location_id)
                    if known is not None:
                        if canonical(known["units"]) != canonical(full["units"]):
                            raise ValueError("placement_causal_capture_prefix_conflict")
                        for carrier in known["carriers"]:
                            expected = next((item for item in full["carriers"] if item["id"] == carrier["id"]), None)
                            expected_positions = [item for item in full["positions"] if item["carrier_id"] == carrier["id"]]
                            known_positions = [item for item in known["positions"] if item["carrier_id"] == carrier["id"]]
                            if canonical(expected) != canonical(carrier) or canonical(expected_positions) != canonical(known_positions):
                                raise ValueError("placement_causal_capture_prefix_conflict")
                    locations[location_id] = full
                    for carrier in full["carriers"]:
                        native = _observation_frame_values(carrier, full["positions"], full["units"], location_id)
                        if carrier["id"] in frames and canonical(frames[carrier["id"]]) != canonical(native):
                            raise ValueError("placement_causal_capture_prefix_conflict")
                        frames[carrier["id"]] = native
                latest_capture, changed_tasks = capture, set()
            continue
        if not placement_operation_indicator(row["kind"], row["result"]):
            # Legacy public profile receipts have no original UID commitment.
            # A real fresh measurement after such an event is required (253).
            raise ValueError("placement_measurement_unsupported_post_event")
        event = validate_placement_receipt(connection, row)
        if event["kind"] == "capacity_placement_assign":
            changes = {}
            for item in event["assignments"]:
                body = item["snapshot"]
                changed_tasks.add(item["task_id"])
                check_before(body["before"])
                if body["bindings"]["measurement_operation_id"] != proof["operation_id"] or body["bindings"]["measurement_digest"] != proof["digest"]:
                    raise ValueError("placement_causal_assignment_measurement")
                for member in body["after_own_holds"]["members"]:
                    if member["id"] in changes:
                        raise ValueError("placement_causal_batch_members_overlap")
                    changes[member["id"]] = member
            replace_units(changes)
        elif event["kind"] == "capacity_placement_begin":
            # Receipt validator proves exactly zero whole ledger; state only.
            changed_tasks.add(event["task_id"])
            continue
        elif event["kind"] == "capacity_placement_cancel":
            changed_tasks.add(event["task_id"])
            check_before(event["source_before"])
            replace_units({unit["id"]: unit for unit in event["source_after"]["members"]})
        else:
            changed_tasks.add(event["task_id"])
            check_before(event["source_before"])
            after = event["destination_after"]
            destination = after["carrier"]["location_id"]
            known = locations.get(destination)
            if known is None:
                raise ValueError("placement_causal_destination_coverage_unknown")
            known_destination = known["units"]
            if canonical(known_destination) != canonical(event["destination_before_units"]):
                raise ValueError("placement_causal_destination_neighbors")
            member_ids = tuple(unit["id"] for unit in event["source_before"]["members"])
            changes = {unit["id"]: unit for unit in after["members"]}
            replace_units(changes, remove=member_ids)
            moving = after["carrier"]["id"]
            for location in locations.values():
                location["carriers"] = [carrier for carrier in location["carriers"] if carrier["id"] != moving]
                location["positions"] = [position for position in location["positions"] if position["carrier_id"] != moving]
            known["carriers"].append(after["carrier"])
            known["positions"].extend(after["positions"])
            known["carriers"].sort(key=lambda carrier: carrier["id"])
            known["positions"].sort(key=lambda position: (position["carrier_id"], position["ordinal"]))
            frames[after["carrier"]["id"]] = json.loads(canonical(after))
            replace_units(changes, destination=destination)
    if current:
        _measurement_current_binding(connection, proof, verified_frames=frames)
        if latest_capture is not None:
            _work_capture_current_rows(connection, latest_capture, location_frames=locations, changed_tasks=changed_tasks)
    return json.loads(canonical({"carrier_frames": frames, "location_frames": locations} if include_locations else frames))


async def _lifecycle_facts(session, stock, org, wh, actor, work_policy_id, *, action=None, task_id=None):
    from modules.wms.placement_policy_codec import decode_placement_work_policy
    now = datetime.now(timezone.utc)
    snapshot = await collect_placement_snapshot(session, stock, org, wh, actor, now)
    data = snapshot.data()
    row = next((row for row in data["work_policies"] if row["id"] == work_policy_id), None)
    if row is None:
        raise ValueError("placement_work_policy_missing")
    receipt = next((row for row in data["operations"] if row["operation_id"] == row_id), None) if (
        row_id := row["approval_operation_id"]) else None
    if receipt is None:
        raise ValueError("placement_work_approval_receipt_missing")
    connection = await session.connection()
    original = await connection.run_sync(lambda conn: _validate_work_neutral(conn, receipt))
    if canonical(row) != canonical(original):
        raise ValueError("placement_original_work_policy_changed")
    proof = _measurement_binding(original)
    coverage = await connection.run_sync(lambda conn: placement_measurement_current_view(conn, proof, include_locations=True))
    if action != "cancel":
        required = set()
        selected = {(route["source_id"], route["destination_id"]) for route in json.loads(original["canonical_payload"])["routes"]}
        for route in proof["request"]["routes"]["value"]:
            if (route["source_location_id"], route["destination_location_id"]) in selected:
                required.update(route["phase_location_ids"])
                required.update(corridor["location_id"] for corridor in route["corridors"]["value"])
        # JSON object keys are strings; absence remains unknown for v1 Work.
        if not required.issubset({int(location_id) for location_id in coverage["location_frames"]}):
            raise ValueError("placement_work_location_coverage_missing")
    await connection.run_sync(lambda conn: _validate_lifecycle_work_current(conn, original, receipt))
    operators = await _managed_operators(session, stock, org, tuple(original["policy"]["operator_subs"]))
    if operators != receipt["result"][WORK_PRIVATE]["operators"]:
        reject("placement_current_operator_identity_stale", 403)
    for assignment in data["assignments"]:
        # Terminal and malformed rows never make the loader revert to legacy.
        operation = next((item for item in data["operations"] if item["operation_id"] == assignment["assignment_operation_id"]), None)
        if operation is None:
            raise ValueError("placement_assignment_original_receipt_missing")
        await connection.run_sync(lambda conn, op=operation: validate_placement_receipt(conn, op))
    decoded = decode_placement_work_policy({**original, **{key: _timestamp(original[key])
        for key in ("valid_from", "valid_until", "approved_at")}})
    snapshot = freeze_snapshot({**data, "selected_work_policy": original, "selected_work_receipt": receipt,
                                "selected_measured_context": proof, "current_operators": operators})
    facts = compile_placement_facts(snapshot, decoded, proof, tuple(item["sub"] for item in operators),
                                    action=action, own_task_id=task_id)
    return now, snapshot, facts, proof


def _validate_lifecycle_work_current(connection, row, receipt):
    # The old binder remains strict and unchanged for no-lifecycle callers.
    # Reuse its non-frame profile/equipment checks with the already verified163
    # current view; no client switch or metadata-neutral allowance exists.
    _validate_work_current_limits(connection, row, receipt["result"][WORK_PRIVATE]["equipment_receipt"])


async def execute_placement_lifecycle(session, stock, org, data, actor):
    from modules.wms.physical_models import PhysicalLedger
    from modules.wms.physical_stock import capture_physical_prior_frontiers, protect_blind_projection
    from modules.wms.placement_models import cap_freeze_placement
    from modules.wms.placement_schemas import PlacementAssign, normalize_placement_request
    from modules.wms.physical_placement import Assignment, Claim, Hold, _plain, _box, _support, _micro
    if not session.in_transaction():
        raise ValueError("placement_route_root_transaction_required")
    seed = stock._caller_seed
    if seed is None:
        reject("placement_original_authority_required", 403)
    uid, sub, iat, roles = seed[7], seed[2], seed[6], tuple(seed[1])
    async def authority():
        await stock._fresh_capacity_authority(session, org, actor, ("wms.operate",))
        if (stock.user.local_user_id, stock.user.keycloak_user_id, stock.user.issued_at, tuple(stock.user.roles)) != (uid, sub, iat, roles) or actor != sub:
            reject("placement_original_authority_changed", 403)
    request = normalize_placement_request(data)
    request_hash = digest({"organization_id": org, "actor_user_id": uid, "actor_sub": sub, "request": request})
    await authority()
    async def replay(row):
        if (row.warehouse_id, row.kind, row.request_hash) != (data.warehouse_id, data.kind, request_hash):
            raise ValueError("placement_operation_identity_conflict")
        connection = await session.connection()
        proof = await connection.run_sync(lambda conn: validate_placement_receipt(conn,
            {key: getattr(row, key) for key in OP_COLUMNS}))
        if proof["actor"] != {"user_id": uid, "sub": sub}:
            reject("placement_original_receipt_owner_mismatch", 403)
        # Historical fold ends before the actual original event PK.
        if data.kind == "capacity_placement_assign":
            binding = proof["assignments"][0]["snapshot"]["bindings"]["work_policy"]
        else:
            header = await session.get(PlacementAssignment, proof["task_id"])
            binding = header.snapshot["bindings"]["work_policy"]
        await connection.run_sync(lambda conn: placement_measurement_current_view(conn,
            _measurement_binding(binding), before_pk=row.id, current=False))
        await authority()
        cap_freeze_placement(session.sync_session, {key: getattr(row, key) for key in OP_COLUMNS},
                             authority=authority, insert=False)
        return placement_ack(proof)
    with session.no_autoflush:
        existing = await session.scalar(select(PhysicalOperation).where(PhysicalOperation.organization_id == org,
            PhysicalOperation.operation_id == str(data.operation_id)))
        if existing is not None:
            return await replay(existing)
        await control(session, data.warehouse_id, org)
        await authority()
        existing = await session.scalar(select(PhysicalOperation).where(PhysicalOperation.organization_id == org,
            PhysicalOperation.operation_id == str(data.operation_id)).execution_options(populate_existing=True))
        if existing is not None:
            return await replay(existing)
        if isinstance(data, PlacementAssign):
            if len({str(choice.work_policy_id) for choice in data.choices}) != 1:
                raise ValueError("placement_batch_policy_ambiguous")
            work_id = str(data.choices[0].work_policy_id)
            now, snapshot, facts, measured = await _lifecycle_facts(session, stock, org, data.warehouse_id, actor, work_id)
            raw = snapshot.data()
            query = Query(2, data.warehouse_id, tuple(str(choice.carrier_id) for choice in data.choices),
                          tuple(dict.fromkeys(choice.destination_location_id for choice in data.choices)), work_id)
            proposed = evaluate_current(facts, query, now)
            if proposed.status != "proposed" or len(proposed.assignments) != len(data.choices):
                raise ValueError("placement_choices_not_currently_proposed")
            choices = {choice.carrier_id: choice for choice in proposed.assignments}
            native_created = []
            pending = []
            for supplied in data.choices:
                selected = choices.get(str(supplied.carrier_id))
                if selected is None or canonical({key: _plain(getattr(selected, key)) for key in request["choices"][0] if key != "task_id"}) != canonical(
                        {key: value for key, value in normalize_placement_request(supplied).items() if key != "task_id"}):
                    raise ValueError("placement_choice_stale_or_substituted")
                task_id = str(supplied.task_id)
                if any(row["id"] == task_id for row in raw["tasks"]):
                    raise ValueError("placement_task_identity_already_exists")
                carrier = next(carrier for carrier in facts.carriers if carrier.id == str(supplied.carrier_id))
                before = _native_frame(raw, carrier.id)
                task = PhysicalTask(id=task_id, warehouse_id=data.warehouse_id, kind="cap_place", status="open",
                    source="placement:" + task_id, unit_id=None, hold_id=None, qty=None,
                    destination_id=supplied.destination_location_id, due_at=None, assignee=sub,
                    evidence="", created_at=now)
                session.add(task)
                native_created.append(task)
                holds = []
                for member in before["members"]:
                    if Decimal(member["qty"]) <= 0:
                        continue
                    hold = PhysicalHold(warehouse_id=data.warehouse_id, source="placement:" + task_id,
                        order_ref="placement:" + task_id, unit_id=member["id"], qty=Decimal(member["qty"]),
                        picked=Decimal(0), shipped=Decimal(0), consumed=Decimal(0), released=Decimal(0),
                        min_shelf_days=0, container=member["container"])
                    session.add(hold)
                    holds.append(hold)
                    unit = await session.get(StockUnit, member["id"])
                    unit.reserved += Decimal(member["qty"])
                    native_created.append(unit)
                pending.append((supplied, selected, carrier, before, task, holds))
            # Native IDs are allocated before the immutable header references them;
            # no receipt has been inserted and no partial transaction can commit.
            await session.flush()
            receipt_assignments = []
            support_extra = {}
            for supplied, selected, carrier, before, task, holds in pending:
                task_id = str(supplied.task_id)
                after = json.loads(canonical(before))
                for hold in holds:
                    for name in ("members", "location_units"):
                        next(unit for unit in after[name] if unit["id"] == hold.unit_id)["reserved"] = format(hold.qty, ".6f")
                destination = next(location for location in facts.locations if location.id == selected.destination_location_id)
                source = next(location for location in facts.locations if location.id == carrier.location_id)
                route = next(route for route in facts.policy.routes if route.source_id == source.id and route.destination_id == destination.id)
                profiles_by_id = {profile.native.id: profile for profile in facts.profiles}
                destination_profile = profiles_by_id[selected.destination_profile_id]
                bearing, _, support_error = _support(facts, destination_profile, _box(destination_profile, selected.slot_keys),
                    carrier.gross_kg, tuple(position for row in facts.carriers for position in row.positions),
                    facts.active_claims, {row.id: row for row in facts.carriers}, profiles_by_id, support_extra)
                if support_error:
                    raise ValueError("placement_choice_support_changed")
                support_carrier = bearing if bearing in {row.id for row in facts.carriers} else None
                if support_carrier is not None:
                    support_extra[support_carrier] = support_extra.get(support_carrier, 0) + _micro(carrier.gross_kg)
                claim = Claim(task_id, carrier.id, destination.id, selected.destination_profile_id, selected.slot_keys,
                              selected.orientation, destination.frame_id, support_carrier)
                own_holds = tuple(Hold(hold.id, hold.unit_id, next(unit.sku for unit in carrier.members if unit.id == hold.unit_id),
                                      next(unit.lot for unit in carrier.members if unit.id == hold.unit_id), hold.qty) for hold in holds)
                pure = Assignment(task_id, org, data.warehouse_id, "cap_place", "open", None, None, None, destination.id,
                    sub, "assigned", carrier.id, source.id, destination.id, source.current_profile_id, destination.current_profile_id,
                    carrier.packaging_profile_id, work_id, carrier.content_revision, placement_content_digest(after),
                    carrier.gross_kg, carrier.tare_kg, tuple(item[0] for item in source.resource_path), route.phase_resource_paths,
                    _members(carrier), _members(carrier), own_holds, own_holds, (claim,), "", "", "", "begin")
                pure_text = _canonical_payload(pure)
                evaluation = json.loads(pure_text)
                package = next(row for row in raw["profiles"] if row["id"] == carrier.packaging_profile_id)
                body = {"version": 1, "evaluation": evaluation, "choice": normalize_placement_request(supplied),
                        "task": {key: value for key, value in _observation_row(task).items() if key not in {"status", "assignee", "evidence"}},
                        "assigned": {"user_id": uid, "sub": sub, "at": now.isoformat(timespec="microseconds").replace("+00:00", "Z")},
                        "executor": {"user_id": uid, "sub": sub}, "before": before, "after_own_holds": after,
                        "native_owned_holds": [_observation_row(hold) for hold in sorted(holds, key=lambda hold: hold.id)],
                        "bindings": {"measurement_operation_id": measured["operation_id"], "measurement_digest": measured["digest"],
                            "work_policy": raw["selected_work_policy"], "profile_bindings": measured["profile_bindings"],
                            "packaging_profile": package, "topology": measured["topology"]}}
                text = canonical(body)
                header = PlacementAssignment(task_id=task_id, organization_id=org, warehouse_id=data.warehouse_id,
                    carrier_id=carrier.id, active_carrier_id=carrier.id, source_location_id=source.id,
                    destination_location_id=destination.id, source_profile_id=source.current_profile_id, destination_profile_id=destination.current_profile_id,
                    packaging_id=carrier.packaging_id, packaging_profile_id=carrier.packaging_profile_id, work_policy_id=work_id,
                    source_content_revision=carrier.content_revision, source_content_digest=placement_content_digest(after),
                    gross_kg=carrier.gross_kg, tare_kg=carrier.tare_kg, orientation=selected.orientation, snapshot=body,
                    canonical_payload=text, digest=_sha(text), state="assigned", assigned_by=sub, assigned_at=now,
                    assignment_operation_id=str(data.operation_id), begin_operation_id=None, settlement_operation_id=None)
                validate_assignment_snapshot(_observation_row(header))
                session.add(header)
                native_created.append(header)
                for ordinal, key in enumerate(selected.slot_keys):
                    claim_row = PlacementClaim(task_id=task_id, ordinal=ordinal, warehouse_id=data.warehouse_id,
                        location_id=destination.id, profile_id=destination.current_profile_id, slot_key=key)
                    session.add(claim_row)
                    native_created.append(claim_row)
                native_created.extend(holds)
                receipt_assignments.append({"task_id": task_id, "snapshot": body, "canonical_payload": text, "digest": header.digest})
            private_extra = {"assignments": receipt_assignments}
            causal = None
        else:
            header = await session.get(PlacementAssignment, str(data.task_id))
            if header is None or (header.organization_id, header.warehouse_id, header.digest) != (org, data.warehouse_id, data.assignment_digest):
                raise ValueError("placement_action_assignment_scope_or_digest")
            action = {"capacity_placement_begin": "begin", "capacity_placement_complete": "complete", "capacity_placement_cancel": "cancel"}[data.kind]
            now, snapshot, facts, measured = await _lifecycle_facts(session, stock, org, data.warehouse_id, actor,
                header.work_policy_id, action=action, task_id=header.task_id)
            raw = snapshot.data()
            original, _ = validate_assignment_snapshot(_observation_row(header))
            if original["executor"] != {"user_id": uid, "sub": sub}:
                reject("placement_assignment_original_executor_mismatch", 403)
            own = next((row for row in facts.active_assignments if row.task_id == header.task_id), None)
            if own is None:
                raise ValueError("placement_assignment_not_active")
            query = Query(2, data.warehouse_id, (header.carrier_id,), (header.destination_location_id,), header.work_policy_id)
            evaluated = evaluate_current(facts, query, now, own)
            if evaluated.status != "proposed" or len(evaluated.assignments) != 1:
                raise ValueError("placement_action_current_validation_failed:" + ",".join(item.code for item in evaluated.violations))
            task = await session.get(PhysicalTask, header.task_id)
            native_created = [header, task]
            private_extra = {"task_id": header.task_id, "assignment_digest": header.digest,
                             "measurement_operation_id": measured["operation_id"], "measurement_digest": measured["digest"]}
            causal = None
            if data.kind == "capacity_placement_begin":
                if (header.state, task.status, header.begin_operation_id) != ("assigned", "open", None):
                    raise ValueError("placement_begin_state")
                header.state, task.status, header.begin_operation_id = "in_transit", "claimed", str(data.operation_id)
                task.evidence = data.evidence
            else:
                before = _native_frame(raw, header.carrier_id)
                if data.kind == "capacity_placement_cancel" and (header.state, task.status, header.begin_operation_id) != ("assigned", "open", None):
                    raise ValueError("placement_cancel_after_begin_denied")
                if data.kind == "capacity_placement_complete":
                    # ALL positive and zero members before receipt INSERT.
                    prior = await capture_physical_prior_frontiers(session, org, [unit["id"] for unit in before["members"]])
                    causal = {"schema_version": 2, "canonical_request": request, "births": [], "count_basis": [], "prior_frontiers": prior}
                    destination_before = [unit for unit in raw["units"] if unit["location_id"] == header.destination_location_id]
                    carrier = await session.get(PhysicalCarrier, header.carrier_id)
                    carrier.location_id = header.destination_location_id
                    carrier.content_revision += 1
                    carrier.confirmed_content_revision = carrier.content_revision
                    native_created.append(carrier)
                for expected in original["native_owned_holds"]:
                    hold = await session.get(PhysicalHold, expected["id"])
                    hold.released += hold.qty
                    unit = await session.get(StockUnit, hold.unit_id)
                    unit.reserved -= hold.qty
                    native_created.extend((hold, unit))
                claims = list((await session.scalars(select(PlacementClaim).where(PlacementClaim.task_id == header.task_id))).all())
                for claim in claims:
                    await session.delete(claim)
                deleted = [{"table": PlacementClaim.__table__.name, "key": [claim.task_id, claim.ordinal]} for claim in claims]
                if data.kind == "capacity_placement_complete":
                    current_units = []
                    for member in before["members"]:
                        unit = await session.get(StockUnit, member["id"])
                        unit.location_id = header.destination_location_id
                        current_units.append(_observation_row(unit))
                        native_created.append(unit)
                    old_positions = list((await session.scalars(select(CarrierPosition).where(CarrierPosition.carrier_id == header.carrier_id))).all())
                    # Update existing ordinal rows in place; append/delete only when
                    # destination group has a different number of native slot rows.
                    new_positions = []
                    for ordinal, slot_key in enumerate(original["choice"]["slot_keys"]):
                        position = next((row for row in old_positions if row.ordinal == ordinal), None)
                        if position is None:
                            position = CarrierPosition(carrier_id=header.carrier_id, ordinal=ordinal, warehouse_id=data.warehouse_id)
                            session.add(position)
                        position.location_id, position.location_profile_id, position.slot_key = header.destination_location_id, header.destination_profile_id, slot_key
                        native_created.append(position)
                        new_positions.append(_observation_row(position))
                    for position in old_positions[len(new_positions):]:
                        await session.delete(position)
                        deleted.append({"table": CarrierPosition.__table__.name, "key": [position.carrier_id, position.ordinal]})
                    after = {"carrier": _observation_row(carrier), "members": current_units, "positions": new_positions,
                             "location_units": sorted(destination_before + current_units, key=lambda unit: unit["id"])}
                    private_extra.update(source_before=before, destination_before_units=destination_before, destination_after=after)
                    header.state, task.status = "completed", "completed"
                else:
                    after = json.loads(canonical(before))
                    for hold in original["native_owned_holds"]:
                        for name in ("members", "location_units"):
                            next(unit for unit in after[name] if unit["id"] == hold["unit_id"])["reserved"] = "0.000000"
                    private_extra.update(source_before=before, source_after=after)
                    header.state, task.status = "canceled", "canceled"
                task.evidence = data.evidence
                header.active_carrier_id, header.settlement_operation_id = None, str(data.operation_id)
        proof = {"version": 1, "organization_id": org, "warehouse_id": data.warehouse_id, "operation_id": str(data.operation_id),
                 "kind": data.kind, "request_hash": request_hash, "request": request, "actor": {"user_id": uid, "sub": sub}, **private_extra}
        result = {**placement_ack(proof), PLACEMENT_PRIVATE[data.kind]: proof}
        if causal is not None:
            result["physical_causal_v1"] = causal
        receipt = {"organization_id": org, "warehouse_id": data.warehouse_id, "operation_id": str(data.operation_id),
                   "kind": data.kind, "request_hash": request_hash, "actor": sub, "result": result}
        validate_placement_envelope(receipt)
        await authority()
        await protect_blind_projection(session, [data.warehouse_id])
        cap_freeze_placement(session.sync_session, receipt, authority=authority, insert=True,
                             rows=native_created, deleted=deleted if "deleted" in locals() else [])
        operation = PhysicalOperation(**receipt)
        session.add(operation)
    await session.flush()
    if causal is not None:
        for member, moved in zip(proof["source_before"]["members"], proof["destination_after"]["members"]):
            amount = Decimal(member["qty"])
            if amount > 0:
                for quantity, unit in ((-amount, member), (amount, moved)):
                    session.add(PhysicalLedger(operation_pk=operation.id, unit_id=member["id"], qty_delta=quantity,
                        dimensions={**_native_dimensions(unit), "carrier_id": header.carrier_id}))
        await session.flush()
    connection = await session.connection()
    await connection.run_sync(lambda conn: validate_placement_receipt(conn, {key: getattr(operation, key) for key in OP_COLUMNS}))
    await authority()
    _check_day(_decision_day(now))
    return placement_ack(proof)
