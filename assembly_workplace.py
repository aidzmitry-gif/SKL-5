"""Read-only, warehouse-owned assembly execution projection."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy import or_, select

from modules.wms.models import Location, WarehouseDirectory
from modules.wms.physical_models import (
    AssemblyConsumption,
    AssemblyExecution,
    AssemblyInputLink,
    CountLocationLock,
    PhysicalHold,
    PhysicalLocation,
    StockUnit,
    WarehouseControl,
)
from modules.wms.physical_stock import batch as scoped_batch
from modules.wms.physical_stock import control as scoped_control

_CONTEXT_KEYS = {
    "id",
    "revision",
    "digest",
    "instructions_version",
    "lines",
    "current_order_digest",
    "current_bom_digest",
    "recorded_approval_id",
}
_EXECUTION_SOURCE_KEYS = {
    "specification_id",
    "specification_revision",
    "specification_digest",
    "approval_id",
    "approval_revision",
    "order_digest",
    "bom_digest",
    "output",
}


def _conflict():
    raise HTTPException(409, "Assembly workplace physical history is inconsistent")


def _positive_int(value):
    return type(value) is int and value > 0


def _digest(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _text(value, *, maximum=200):
    return isinstance(value, str) and 0 < len(value) <= maximum


def _exact_decimal(value):
    if isinstance(value, (bool, float)):
        _conflict()
    if isinstance(value, Decimal):
        result = value
    elif type(value) is int:
        result = Decimal(value)
    elif isinstance(value, str) and value and value.strip() == value:
        try:
            result = Decimal(value)
        except InvalidOperation:
            _conflict()
    else:
        _conflict()
    if not result.is_finite():
        _conflict()
    return result


def _timestamp(value):
    if value is None or not callable(getattr(value, "isoformat", None)):
        _conflict()
    return value.isoformat()


def _validate_specifications(specifications):
    if not isinstance(specifications, dict):
        _conflict()
    revisions = set()
    for specification_id, value in specifications.items():
        if (not _positive_int(specification_id)
                or not isinstance(value, dict)
                or set(value) != _CONTEXT_KEYS
                or not _positive_int(value["id"])
                or value["id"] != specification_id):
            _conflict()
        if not _positive_int(value["revision"]) or not _digest(value["digest"]):
            _conflict()
        if not _text(value["instructions_version"], maximum=64):
            _conflict()
        if value["current_order_digest"] is not None and not _digest(value["current_order_digest"]):
            _conflict()
        if value["current_bom_digest"] is not None and not _digest(value["current_bom_digest"]):
            _conflict()
        approval_id = value["recorded_approval_id"]
        if approval_id is not None and not _positive_int(approval_id):
            _conflict()
        lines = value["lines"]
        if not isinstance(lines, dict) or not 1 <= len(lines) <= 500:
            _conflict()
        for line_id, sku_code in lines.items():
            if (not isinstance(line_id, str) or not line_id.isdecimal()
                    or str(int(line_id)) != line_id or int(line_id) <= 0
                    or not _text(sku_code, maximum=64)):
                _conflict()
        if value["revision"] in revisions:
            _conflict()
        revisions.add(value["revision"])
    return specifications


def _validate_execution_source(execution, specification):
    source = execution.source_snapshot
    if not isinstance(source, dict) or set(source) != _EXECUTION_SOURCE_KEYS:
        _conflict()
    if (not _positive_int(source["specification_id"])
            or type(source["specification_revision"]) is not int
            or source["specification_revision"] <= 0
            or not _digest(source["specification_digest"])
            or source["specification_id"] != execution.specification_id
            or source["specification_revision"] != specification["revision"]
            or source["specification_digest"] != specification["digest"]
            or execution.specification_digest != specification["digest"]):
        _conflict()
    if (not _positive_int(source["approval_id"])
            or not _positive_int(source["approval_revision"])
            or not _digest(source["order_digest"])
            or not _digest(source["bom_digest"])):
        _conflict()
    output = source["output"]
    if (not isinstance(output, dict)
            or set(output) != {"sku_code", "base_unit", "precision", "serialized"}
            or not _text(output["sku_code"], maximum=64)
            or not _text(output["base_unit"], maximum=16)
            or type(output["precision"]) is not int
            or not 0 <= output["precision"] <= 6
            or type(output["serialized"]) is not bool):
        _conflict()
    return source


def _validate_execution(execution, organization_id, warehouse_id, order_id, specifications):
    if (type(execution.organization_id) is not int
            or type(execution.warehouse_id) is not int
            or type(execution.order_id) is not int
            or execution.organization_id != organization_id
            or execution.warehouse_id != warehouse_id
            or execution.order_id != order_id
            or not _text(execution.id, maximum=36)
            or not isinstance(execution.status, str)
            or execution.status not in {"active", "closed"}
            or not _positive_int(execution.specification_id)):
        _conflict()
    specification = specifications.get(execution.specification_id)
    if specification is None:
        _conflict()
    source = _validate_execution_source(execution, specification)
    if not _text(execution.container, maximum=24):
        _conflict()

    output_keys = execution.output_keys
    consumed_keys = execution.consumed_output_keys
    if (not isinstance(output_keys, list)
            or not output_keys
            or any(not _text(key, maximum=64) for key in output_keys)
            or len(output_keys) != len(set(output_keys))
            or not isinstance(consumed_keys, list)
            or any(not _text(key, maximum=64) for key in consumed_keys)
            or len(consumed_keys) != len(set(consumed_keys))
            or not set(consumed_keys).issubset(output_keys)):
        _conflict()
    output_quantity = _exact_decimal(execution.output_qty)
    if output_quantity <= 0 or output_quantity != Decimal(len(output_keys)):
        _conflict()
    return {
        "row": execution,
        "specification": specification,
        "source": source,
        "output_keys": output_keys,
        "consumed_keys": consumed_keys,
        "output_quantity": output_quantity,
    }


def _validate_dimension_snapshot(dimensions, warehouse_id, *, required_container=None):
    if not isinstance(dimensions, dict):
        _conflict()
    location_id = dimensions.get("location_id")
    if (type(dimensions.get("warehouse_id")) is not int
            or dimensions["warehouse_id"] != warehouse_id
            or type(location_id) is not int or location_id <= 0):
        _conflict()
    if (not _text(dimensions.get("sku_code"), maximum=64)
            or type(dimensions.get("batch_id")) is not int
            or dimensions["batch_id"] <= 0
            or (dimensions.get("packaging_id") is not None
                and not _positive_int(dimensions["packaging_id"]))
            or not isinstance(dimensions.get("characteristic"), str)
            or not isinstance(dimensions.get("purpose"), str)):
        _conflict()
    if required_container is not None and dimensions.get("container") != required_container:
        _conflict()
    return location_id


def _validate_dimensions(dimensions, unit):
    if (dimensions["sku_code"] != unit.sku_code
            or dimensions["batch_id"] != unit.batch_id
            or dimensions.get("packaging_id") != unit.packaging_id
            or dimensions["characteristic"] != unit.characteristic
            or dimensions["purpose"] != unit.purpose):
        _conflict()


async def _execution_projection(
    session,
    organization_id,
    warehouse_id,
    order_id,
    specifications,
    stock_gateway,
):
    rows = (await session.scalars(
        select(AssemblyExecution).where(
            AssemblyExecution.organization_id == organization_id,
            AssemblyExecution.warehouse_id == warehouse_id,
            AssemblyExecution.order_id == order_id,
        ).order_by(AssemblyExecution.created_at, AssemblyExecution.id)
        .execution_options(populate_existing=True)
    )).all()
    if not rows:
        return []
    if not specifications:
        _conflict()
    executions = {}
    latest_revision = max(item["revision"] for item in specifications.values())
    for row in rows:
        item = _validate_execution(
            row, organization_id, warehouse_id, order_id, specifications
        )
        if row.id in executions:
            _conflict()
        executions[row.id] = item

    execution_ids = sorted(executions)
    links = (await session.scalars(
        select(AssemblyInputLink).where(
            AssemblyInputLink.execution_id.in_(execution_ids)
        ).order_by(AssemblyInputLink.execution_id, AssemblyInputLink.id)
        .execution_options(populate_existing=True)
    )).all()
    links_by_id = {}
    links_by_execution = {execution_id: [] for execution_id in execution_ids}
    links_by_hold = {}
    linked_hold_ids = set()
    unit_ids = set()
    location_ids = set()
    for link in links:
        item = executions.get(link.execution_id)
        if (item is None or type(link.id) is not int or link.id <= 0
                or not isinstance(link.specification_line_id, str)
                or not _text(link.specification_line_id, maximum=64)
                or not _text(link.actual_kit_unit_id, maximum=36)
                or not _text(link.original_unit_id, maximum=36)
                or not _text(link.kit_move_operation_id, maximum=36)
                or type(link.hold_id) is not int or link.hold_id <= 0):
            _conflict()
        specification = item["specification"]
        if link.specification_line_id not in specification["lines"]:
            _conflict()
        moved = _exact_decimal(link.qty)
        consumed = _exact_decimal(link.consumed_qty)
        returned = _exact_decimal(link.returned_qty)
        if moved <= 0 or consumed < 0 or returned < 0 or consumed + returned > moved:
            _conflict()
        if link.id in links_by_id:
            _conflict()
        links_by_execution[link.execution_id].append(link)
        links_by_id[link.id] = link
        links_by_hold.setdefault(link.hold_id, []).append(link)
        linked_hold_ids.add(link.hold_id)
        unit_ids.update((link.original_unit_id, link.actual_kit_unit_id))
        source_location = _validate_dimension_snapshot(
            link.source_dimensions, warehouse_id
        )
        kit_location = _validate_dimension_snapshot(
            link.kit_dimensions,
            warehouse_id,
            required_container=item["row"].container,
        )
        location_ids.update((source_location, kit_location))

    traces = (await session.scalars(
        select(AssemblyConsumption).where(
            AssemblyConsumption.execution_id.in_(execution_ids)
        ).order_by(AssemblyConsumption.execution_id, AssemblyConsumption.id)
        .execution_options(populate_existing=True)
    )).all()
    trace_quantity_by_link = {}
    trace_keys_by_execution = {execution_id: set() for execution_id in execution_ids}
    trace_owners = {}
    trace_identity = set()
    for trace in traces:
        item = executions.get(trace.execution_id)
        link = links_by_id.get(trace.input_link_id)
        if (item is None or link is None
                or link.execution_id != trace.execution_id
                or link.specification_line_id != trace.specification_line_id
                or trace.output_key not in item["output_keys"]
                or not _text(trace.operation_id, maximum=36)):
            _conflict()
        quantity = _exact_decimal(trace.qty)
        if quantity <= 0:
            _conflict()
        identity = (
            trace.execution_id,
            trace.output_key,
            trace.specification_line_id,
            trace.input_link_id,
        )
        if identity in trace_identity:
            _conflict()
        trace_identity.add(identity)
        trace_quantity_by_link[trace.input_link_id] = (
            trace_quantity_by_link.get(trace.input_link_id, Decimal(0)) + quantity
        )
        trace_keys_by_execution[trace.execution_id].add(trace.output_key)
        owner = trace_owners.setdefault(trace.output_key, trace.execution_id)
        if owner != trace.execution_id:
            _conflict()

    order_refs = ["assembly:" + execution_id for execution_id in execution_ids]
    conditions = [PhysicalHold.order_ref.in_(order_refs)]
    if linked_hold_ids:
        conditions.append(PhysicalHold.id.in_(sorted(linked_hold_ids)))
    holds = (await session.scalars(
        select(PhysicalHold).where(or_(*conditions))
        .order_by(PhysicalHold.id).execution_options(populate_existing=True)
    )).all()
    holds_by_id = {}
    holds_by_execution = {execution_id: [] for execution_id in execution_ids}
    hold_execution = {}
    for hold in holds:
        if not isinstance(hold.order_ref, str):
            _conflict()
        execution_id = hold.order_ref.removeprefix("assembly:")
        item = executions.get(execution_id)
        if (item is None or type(hold.id) is not int or hold.id <= 0
                or type(hold.warehouse_id) is not int
                or hold.id in holds_by_id
                or hold.warehouse_id != warehouse_id
                or hold.order_ref != "assembly:" + execution_id
                or hold.container != item["row"].container):
            _conflict()
        source_prefix = "assembly:" + execution_id + ":"
        if not isinstance(hold.source, str) or not hold.source.startswith(source_prefix):
            _conflict()
        line_id = hold.source[len(source_prefix):]
        if line_id not in item["specification"]["lines"]:
            _conflict()
        qty = _exact_decimal(hold.qty)
        picked = _exact_decimal(hold.picked)
        shipped = _exact_decimal(hold.shipped)
        consumed = _exact_decimal(hold.consumed)
        released = _exact_decimal(hold.released)
        if (qty <= 0 or min(picked, shipped, consumed, released) < 0
                or shipped + consumed > picked or picked + released > qty):
            _conflict()
        holds_by_id[hold.id] = hold
        holds_by_execution[execution_id].append(hold)
        hold_execution[hold.id] = execution_id
        if not _text(hold.unit_id, maximum=36):
            _conflict()
        unit_ids.add(hold.unit_id)

    units = (await session.scalars(
        select(StockUnit).where(StockUnit.id.in_(sorted(unit_ids)))
        .order_by(StockUnit.id).execution_options(populate_existing=True)
    )).all() if unit_ids else []
    units_by_id = {unit.id: unit for unit in units}
    if set(units_by_id) != unit_ids:
        _conflict()
    for unit in units:
        if (type(unit.warehouse_id) is not int
                or unit.warehouse_id != warehouse_id
                or type(unit.location_id) is not int or unit.location_id <= 0
                or not _text(unit.sku_code, maximum=64)
                or type(unit.batch_id) is not int or unit.batch_id <= 0
                or (unit.packaging_id is not None and not _positive_int(unit.packaging_id))
                or not isinstance(unit.characteristic, str)
                or not isinstance(unit.purpose, str)):
            _conflict()
        location_ids.add(unit.location_id)

    for hold in holds:
        execution_id = hold_execution[hold.id]
        item = executions[execution_id]
        line_id = hold.source[len("assembly:" + execution_id + ":"):]
        original = units_by_id.get(hold.unit_id)
        if (original is None
                or original.sku_code != item["specification"]["lines"][line_id]):
            _conflict()

    for link in links:
        item = executions[link.execution_id]
        hold = holds_by_id.get(link.hold_id)
        original = units_by_id.get(link.original_unit_id)
        kit = units_by_id.get(link.actual_kit_unit_id)
        if (hold is None or hold_execution.get(link.hold_id) != link.execution_id
                or hold.unit_id != link.original_unit_id
                or hold.source != "assembly:" + link.execution_id + ":" + link.specification_line_id
                or original is None or kit is None
                or original.warehouse_id != warehouse_id or kit.warehouse_id != warehouse_id
                or original.sku_code != kit.sku_code
                or original.batch_id != kit.batch_id
                or original.packaging_id != kit.packaging_id
                or original.characteristic != kit.characteristic
                or original.purpose != kit.purpose
                or original.sku_code != item["specification"]["lines"][link.specification_line_id]):
            _conflict()
        _validate_dimensions(link.source_dimensions, original)
        _validate_dimensions(link.kit_dimensions, kit)

    locations = (await session.scalars(
        select(PhysicalLocation).where(
            PhysicalLocation.location_id.in_(sorted(location_ids))
        ).order_by(PhysicalLocation.location_id)
        .execution_options(populate_existing=True)
    )).all() if location_ids else []
    if (len(locations) != len(location_ids)
            or any(location.warehouse_id != warehouse_id for location in locations)):
        _conflict()

    for execution_id, item in executions.items():
        for link in links_by_execution[execution_id]:
            if trace_quantity_by_link.get(link.id, Decimal(0)) != _exact_decimal(link.consumed_qty):
                _conflict()
        if trace_keys_by_execution[execution_id] != set(item["consumed_keys"]):
            _conflict()
        for hold in holds_by_execution[execution_id]:
            hold_links = links_by_hold.get(hold.id, [])
            if (sum((_exact_decimal(link.consumed_qty) for link in hold_links), Decimal(0))
                    != _exact_decimal(hold.consumed)
                    or sum((_exact_decimal(link.qty) - _exact_decimal(link.returned_qty)
                            for link in hold_links), Decimal(0))
                    != _exact_decimal(hold.picked)):
                _conflict()
        if item["row"].status == "closed":
            execution_holds = holds_by_execution[execution_id]
            execution_links = links_by_execution[execution_id]
            settled = bool(execution_holds) and all(
                _exact_decimal(hold.qty)
                == _exact_decimal(hold.shipped)
                + _exact_decimal(hold.consumed)
                + _exact_decimal(hold.released)
                and _exact_decimal(hold.shipped) == 0
                for hold in execution_holds
            ) and all(
                _exact_decimal(link.qty)
                == _exact_decimal(link.consumed_qty)
                + _exact_decimal(link.returned_qty)
                for link in execution_links
            )
            if not settled:
                _conflict()

    active_owner = {}
    for execution_id, item in executions.items():
        if item["row"].status == "active":
            for output_key in item["output_keys"]:
                owner = active_owner.setdefault(output_key, execution_id)
                if owner != execution_id:
                    _conflict()
    if any(
        key in active_owner and active_owner[key] != owner
        for key, owner in trace_owners.items()
    ):
        _conflict()

    if links and stock_gateway is None:
        raise HTTPException(503, "Stock batch gateway is unavailable")
    batch_cache = {}
    projected = []
    for execution_id, item in executions.items():
        execution = item["row"]
        specification = item["specification"]
        source = item["source"]
        order_status = ("unavailable" if specification["current_order_digest"] is None else
            "matches_recorded_digest" if source["order_digest"] == specification["current_order_digest"] else "changed")
        bom_status = ("unavailable" if specification["current_bom_digest"] is None else
            "matches_recorded_digest" if source["bom_digest"] == specification["current_bom_digest"] else "changed")
        approval_id = specification["recorded_approval_id"]
        if approval_id is None:
            approval_status = "unavailable"
        elif source["approval_id"] == approval_id:
            approval_status = "matches_recorded_id"
        else:
            approval_status = "different_recorded_id"

        projected_links = []
        for link in links_by_execution[execution_id]:
            original = units_by_id[link.original_unit_id]
            cache_key = original.batch_id, original.sku_code
            if cache_key not in batch_cache:
                batch_cache[cache_key] = await scoped_batch(
                    session,
                    stock_gateway,
                    organization_id,
                    original.batch_id,
                    original.sku_code,
                )
            lot = batch_cache[cache_key]
            if not isinstance(lot, dict) or not _text(lot.get("lot_no"), maximum=64):
                _conflict()
            moved = _exact_decimal(link.qty)
            consumed = _exact_decimal(link.consumed_qty)
            returned = _exact_decimal(link.returned_qty)
            projected_links.append({
                "id": link.id,
                "specification_line_id": link.specification_line_id,
                "hold_id": link.hold_id,
                "original_unit_id": link.original_unit_id,
                "actual_kit_unit_id": link.actual_kit_unit_id,
                "kit_move_operation_id": link.kit_move_operation_id,
                "sku_code": original.sku_code,
                "batch_id": original.batch_id,
                "lot_no": lot["lot_no"],
                "moved_quantity": format(moved, "f"),
                "consumed_quantity": format(consumed, "f"),
                "returned_quantity": format(returned, "f"),
                "remaining_kit_quantity": format(moved - consumed - returned, "f"),
            })
        projected.append({
            "id": execution.id,
            "status": execution.status,
            "created_at": _timestamp(execution.created_at),
            "executed_specification": {
                "id": execution.specification_id,
                "revision": specification["revision"],
                "digest": specification["digest"],
                "instructions_version": specification["instructions_version"],
            },
            "specification_relation": (
                "latest" if specification["revision"] == latest_revision
                else "older_revision"
            ),
            "execution_source_status": {
                "order": order_status,
                "bom": bom_status,
                "approval": approval_status,
            },
            "output_quantity": format(item["output_quantity"], "f"),
            "planned_output_keys": list(item["output_keys"]),
            "consumed_output_keys": list(item["consumed_keys"]),
            "consumption_status": "recorded_component_consumption",
            "kit_links": projected_links,
        })
    return projected


async def read_assembly_warehouse(session, organization_id, warehouse_id, *, lock=False):
    if (
        not _positive_int(organization_id)
        or not _positive_int(warehouse_id)
        or type(lock) is not bool
    ):
        _conflict()
    if lock:
        if not session.in_transaction():
            raise HTTPException(409, "Assembly warehouse lock requires an existing transaction")
        from modules.wms.physical_capacity import require_capacity_read_committed

        connection = await session.connection()
        await connection.run_sync(require_capacity_read_committed)
        with session.no_autoflush:
            await scoped_control(session, warehouse_id, organization_id, lock=True)

    query = (
        select(WarehouseDirectory, WarehouseControl)
        .join(
            WarehouseControl,
            (WarehouseControl.warehouse_id == WarehouseDirectory.id)
            & (WarehouseControl.organization_id == WarehouseDirectory.organization_id),
        )
        .where(
            WarehouseDirectory.id == warehouse_id,
            WarehouseDirectory.organization_id == organization_id,
            WarehouseDirectory.is_active.is_(True),
            WarehouseControl.warehouse_id == warehouse_id,
            WarehouseControl.organization_id == organization_id,
        )
        .execution_options(populate_existing=True)
    )
    if lock:
        with session.no_autoflush:
            result = await session.execute(query)
    else:
        result = await session.execute(query)
    row = result.one_or_none()
    if row is None:
        raise HTTPException(404, "Warehouse not found in this organization")
    _directory, control = row
    if control.mode not in {"preparing", "erp"}:
        _conflict()
    return {
        "organization_id": organization_id,
        "warehouse_id": warehouse_id,
        "mode": control.mode,
    }


def _assembly_station_dto(physical, legacy, directory, organization_id, warehouse_id):
    if (
        physical is None
        or legacy is None
        or directory is None
        or type(organization_id) is not int or organization_id <= 0
        or type(warehouse_id) is not int or warehouse_id <= 0
        or type(directory.id) is not int or directory.id != warehouse_id
        or type(directory.organization_id) is not int
        or directory.organization_id != organization_id
        or type(directory.is_active) is not bool or not directory.is_active
        or not _text(directory.name, maximum=128)
        or type(physical.location_id) is not int or physical.location_id <= 0
        or type(physical.warehouse_id) is not int or physical.warehouse_id != warehouse_id
        or type(physical.kind) is not str or physical.kind != "assembly"
        or type(physical.code) is not str or not _text(physical.code, maximum=64)
        or not physical.code.strip()
        or type(legacy.id) is not int or legacy.id != physical.location_id
        or type(legacy.warehouse) is not str or legacy.warehouse != directory.name
        or type(legacy.code) is not str or legacy.code != physical.code
        or type(legacy.title) is not str or len(legacy.title) > 255
        or type(legacy.is_active) is not bool
    ):
        _conflict()
    return {
        "organization_id": organization_id,
        "warehouse_id": warehouse_id,
        "station_location_id": physical.location_id,
        "code": physical.code,
        "title": legacy.title,
        "kind": physical.kind,
        "is_active": legacy.is_active,
    }


async def read_assembly_station(
    session,
    organization_id,
    warehouse_id,
    station_location_id,
    *,
    lock=False,
    require_active=True,
):
    if (
        not _positive_int(organization_id)
        or not _positive_int(warehouse_id)
        or not _positive_int(station_location_id)
        or type(lock) is not bool
        or type(require_active) is not bool
    ):
        _conflict()
    await read_assembly_warehouse(
        session, organization_id, warehouse_id, lock=lock,
    )
    query = (
        select(PhysicalLocation, Location, WarehouseDirectory)
        .join(
            WarehouseDirectory,
            (WarehouseDirectory.id == PhysicalLocation.warehouse_id)
            & (WarehouseDirectory.organization_id == organization_id),
        )
        .join(Location, Location.id == PhysicalLocation.location_id)
        .where(
            PhysicalLocation.location_id == station_location_id,
            PhysicalLocation.warehouse_id == warehouse_id,
            PhysicalLocation.kind == "assembly",
            WarehouseDirectory.id == warehouse_id,
            WarehouseDirectory.organization_id == organization_id,
            WarehouseDirectory.is_active.is_(True),
        )
        .execution_options(populate_existing=True)
    )
    if lock:
        query = query.with_for_update(of=(PhysicalLocation, Location))
        with session.no_autoflush:
            row = (await session.execute(query)).one_or_none()
    else:
        row = (await session.execute(query)).one_or_none()
    if row is None:
        raise HTTPException(404, "Assembly station not found in this warehouse")
    physical, legacy, directory = row
    station = _assembly_station_dto(
        physical, legacy, directory, organization_id, warehouse_id,
    )
    if require_active and not station["is_active"]:
        raise HTTPException(404, "Assembly station not found in this warehouse")
    return station


async def read_assembly_stations(session, organization_id, warehouse_id):
    if not _positive_int(organization_id) or not _positive_int(warehouse_id):
        _conflict()
    await read_assembly_warehouse(session, organization_id, warehouse_id)
    rows = (await session.execute(
        select(PhysicalLocation, Location, WarehouseDirectory)
        .join(
            WarehouseDirectory,
            (WarehouseDirectory.id == PhysicalLocation.warehouse_id)
            & (WarehouseDirectory.organization_id == organization_id),
        )
        .join(Location, Location.id == PhysicalLocation.location_id)
        .where(
            PhysicalLocation.warehouse_id == warehouse_id,
            PhysicalLocation.kind == "assembly",
            Location.is_active.is_(True),
            WarehouseDirectory.id == warehouse_id,
            WarehouseDirectory.organization_id == organization_id,
            WarehouseDirectory.is_active.is_(True),
        )
        .order_by(PhysicalLocation.code, PhysicalLocation.location_id)
        .execution_options(populate_existing=True)
    )).all()
    return [
        _assembly_station_dto(
            physical, legacy, directory, organization_id, warehouse_id,
        )
        for physical, legacy, directory in rows
    ]


async def _assembly_count_locked(session, warehouse_id):
    return await session.scalar(
        select(CountLocationLock.location_id)
        .join(
            PhysicalLocation,
            PhysicalLocation.location_id == CountLocationLock.location_id,
        )
        .where(PhysicalLocation.warehouse_id == warehouse_id)
        .limit(1)
        .execution_options(populate_existing=True)
    ) is not None


async def read_assembly_blind_count(session, organization_id, warehouse_id):
    await scoped_control(session, warehouse_id, organization_id)
    return await _assembly_count_locked(session, warehouse_id)


async def read_assembly_workplace(
    session,
    organization_id,
    warehouse_id,
    order_id,
    specifications,
    *,
    stock_gateway,
):
    if not _positive_int(order_id):
        _conflict()
    specifications = _validate_specifications(specifications)
    warehouse = await read_assembly_warehouse(session, organization_id, warehouse_id)
    warehouse_mode = warehouse["mode"]

    if await _assembly_count_locked(session, warehouse_id):
        status = "blind_count_hidden"
        executions = None
    elif warehouse_mode != "erp":
        status = "opening_balance_unknown"
        executions = None
    else:
        status = "visible"
        executions = await _execution_projection(
            session,
            organization_id,
            warehouse_id,
            order_id,
            specifications,
            stock_gateway,
        )
    return {
        "warehouse": {"id": warehouse_id, "mode": warehouse_mode},
        "physical_read_status": status,
        "executions": executions,
    }


async def read_assembly_order_other_workplaces(
    session, organization_id, order_id, selected_warehouse_id, specifications,
    *, stock_gateway,
):
    """Complete warehouse set; caller reuses its selected read in the same snapshot."""
    if not all(_positive_int(value) for value in (
        organization_id, order_id, selected_warehouse_id,
    )):
        _conflict()
    specifications = _validate_specifications(specifications)
    warehouse_ids = (await session.scalars(
        select(AssemblyExecution.warehouse_id).where(
            AssemblyExecution.organization_id == organization_id,
            AssemblyExecution.order_id == order_id,
        ).distinct().order_by(AssemblyExecution.warehouse_id)
    )).all()
    if any(not _positive_int(value) for value in warehouse_ids):
        _conflict()
    warehouse_ids = sorted(set(warehouse_ids) | {selected_warehouse_id})
    other_workplaces = []
    for warehouse_id in warehouse_ids:
        if warehouse_id != selected_warehouse_id:
            try:
                physical = await read_assembly_workplace(
                    session, organization_id, warehouse_id, order_id, specifications,
                    stock_gateway=stock_gateway,
                )
            except HTTPException as error:
                if error.status_code != 404:
                    raise
                # Historical executions still count as a missing source. Never
                # drop this warehouse or infer zero from its deactivation.
                physical = {"warehouse": {"id": warehouse_id, "mode": "preparing"},
                    "physical_read_status": "opening_balance_unknown", "executions": None}
            other_workplaces.append(physical)
    return {"warehouse_ids": warehouse_ids, "other_workplaces": other_workplaces}


async def read_assembly_outputs(session, core, organization_id, order_id, warehouse_id, user):
    """Proved read state for the real output/QC/return caller, including refresh."""
    from core.services.auth import has_permission
    from modules.wms.assembly_quality import (
        _fresh_actor, physical_assembly_binding, physical_assembly_identity,
        physical_assembly_process, physical_assembly_qc_records, physical_assembly_validate_qc,
        read_process_run_h11,
    )
    from modules.wms.item_identity import (
        _OriginUnproven, _prove_assembly_shipment, assembly_unit_qc_birth,
        prove_assembly_shipment_return,
    )
    from modules.wms.physical_models import (
        AssemblyOutputIdentity, CurrentStockStateError, PhysicalOperation, PickedAllocation,
        require_current_in_transit,
    )
    from modules.wms.physical_schemas import AssemblyOutputReadProjection, AssemblyOutputReadProjectionV2
    from modules.wms.physical_stock import PhysicalStock, assembly_lot_snapshot, effective_lot, protect_blind_receipt

    # Freeze the actual normal caller before the first await. Nullable issuance
    # follows normal resolver revocation semantics, without a native-IAT default.
    stock = PhysicalStock(core.services.stock, core.event_bus, services=core.services, user=user, core=core)
    seed = stock._caller_seed
    if seed is None or not _positive_int(seed[7]) or not _text(seed[2], maximum=200):
        raise HTTPException(403, "Managed original assembly reader is required")
    expected_actor = (seed[7], seed[2])
    from core.services.auth import CurrentUser

    def original_principal():
        return CurrentUser(seed[0], list(seed[1]), keycloak_user_id=seed[2], local_status=None,
            deal_visibility=seed[4], crm_restricted=seed[5], issued_at=seed[6], local_user_id=seed[7])

    fresh, _managed, _subject = await _fresh_actor(session, core, organization_id, original_principal(), expected_actor=expected_actor)
    if fresh.issued_at != seed[6] or fresh.local_user_id != seed[7]:
        raise HTTPException(403, "Assembly reader changed before authorization")
    h11 = await read_process_run_h11(session, core, organization_id, order_id, original_principal(), warehouse_id=warehouse_id)
    if h11["actor"] != {"user_id": seed[7], "sub": seed[2]}:
        raise HTTPException(403, "Assembly reader changed during H11 authorization")
    warehouse = await read_assembly_warehouse(session, organization_id, warehouse_id, lock=True)
    blind = await read_assembly_blind_count(session, organization_id, warehouse_id)
    result = {"schema_version": 1, "organization_id": organization_id, "order_id": order_id,
        "warehouse_id": warehouse_id, "status": "blind_count_hidden" if blind else "unavailable",
        "execution_states": None, "identities": None, "batches": None, "locations": None,
        "return_candidates": None, "actor": h11["actor"]}
    eligibility = []
    execution_eligibility = []
    return_eligibility = []
    if not blind and warehouse["mode"] == "erp":
        result.update(status="visible", execution_states=[], identities=[], batches=[], locations=[], return_candidates=[])
        current_anchor = current_prepared = None
        if (h11["status"] == "confirmed" and h11["document"] is not None
                and all(h11["document"][field]["status"] == "resolved"
                    for field in ("manufacturing_policy", "serial_policy", "expiry_policy"))):
            current_anchor, _inputs, current_prepared = await physical_assembly_process(session, organization_id, order_id)
        executions = (await session.scalars(select(AssemblyExecution).where(
            AssemblyExecution.organization_id == organization_id, AssemblyExecution.order_id == order_id,
            AssemblyExecution.warehouse_id == warehouse_id).order_by(AssemblyExecution.id))).all()
        for execution in executions:
            bind = await session.scalar(select(PhysicalOperation).where(
                PhysicalOperation.organization_id == organization_id, PhysicalOperation.operation_id == execution.bind_operation_id,
                PhysicalOperation.kind == "assembly_bind"))
            if bind is None:
                _conflict()
            anchor = None
            first_qc = None
            if "assembly_bind_proof_v1" in bind.result:
                raw = bind.result["assembly_bind_proof_v1"]
                if not isinstance(raw, dict) or not isinstance(raw.get("anchor"), dict):
                    _conflict()
                anchor = raw["anchor"]
                await physical_assembly_binding(session, execution, anchor)
                first = await session.scalar(select(AssemblyOutputIdentity).where(
                    AssemblyOutputIdentity.organization_id == organization_id,
                    AssemblyOutputIdentity.order_id == order_id,
                    AssemblyOutputIdentity.output_key == anchor["designated_first_output_key"]))
                first_qc = "unborn"
                if first is not None:
                    first, _output, first_birth = await physical_assembly_identity(session, organization_id, first.id)
                    if first.anchor == anchor:
                        records = await physical_assembly_qc_records(session, first.id, organization_id, first_birth.operation_id)
                        first_qc = "pending"
                        if records:
                            await physical_assembly_validate_qc(session, first, *records[0])
                            first_qc = records[0][1].verdict
            state = {"execution_id": execution.id, "configured": anchor is not None,
                "confirmed_revision_id": None if anchor is None else anchor["confirmed_revision_id"],
                "policy_inputs_digest": None if anchor is None else anchor["policy_inputs_digest"],
                "designated_first_output_key": None if anchor is None else anchor["designated_first_output_key"],
                "first_qc": first_qc, "can_consume_other": False, "can_output_other": False}
            result["execution_states"].append(state)
            execution_eligibility.append((state, anchor == current_anchor and anchor is not None,
                execution.status == "active", first_qc == "PASS"))
        identities = (await session.scalars(select(AssemblyOutputIdentity).where(
            AssemblyOutputIdentity.organization_id == organization_id, AssemblyOutputIdentity.order_id == order_id)
            .order_by(AssemblyOutputIdentity.id))).all()
        await stock._assembly_whole_identity(session, organization_id)
        shipments = (await session.scalars(select(PhysicalOperation).where(
            PhysicalOperation.organization_id == organization_id, PhysicalOperation.warehouse_id == warehouse_id,
            PhysicalOperation.kind == "ship").order_by(PhysicalOperation.id))).all()
        proved_shipments = {}
        for ship in shipments:
            if not isinstance(ship.result, dict) or not isinstance(ship.result.get("shipped"), list):
                _conflict()
            for line in ship.result["shipped"]:
                if not isinstance(line, dict) or type(line.get("pick_id")) is not int:
                    _conflict()
                pick = await session.get(PickedAllocation, line["pick_id"])
                unit = None if pick is None else await session.get(StockUnit, pick.unit_id)
                if pick is None or unit is None:
                    _conflict()
                if unit.assembly_output_id is not None:
                    identity, _output, _birth = await physical_assembly_identity(session, organization_id, unit.assembly_output_id)
                    try:
                        proof = await _prove_assembly_shipment(session, organization_id, warehouse_id, identity, ship.operation_id, pick.id)
                    except _OriginUnproven as exc:
                        raise HTTPException(409, "Assembly output shipment is unproved") from exc
                    if identity.order_id == order_id:
                        proved_shipments.setdefault(identity.id, []).append(proof)
        for stored in identities:
            identity, _output, birth = await physical_assembly_identity(session, organization_id, stored.id)
            positives = (await session.scalars(select(StockUnit).where(
                StockUnit.assembly_output_id == identity.id, StockUnit.qty > 0))).all()
            if len(positives) > 1:
                _conflict()
            positive = positives[0] if positives else None
            local = positive if positive is not None and positive.warehouse_id == warehouse_id else None
            execution = await session.get(AssemblyExecution, identity.execution_id)
            related = execution.warehouse_id == warehouse_id or local is not None or identity.id in proved_shipments
            if not related:
                continue
            lot = await scoped_batch(session, core.services.stock, organization_id, identity.batch_id, identity.sku_code)
            if assembly_lot_snapshot(lot, identity.batch_id, identity.sku_code) != identity.batch_snapshot:
                _conflict()
            qc_birth = verdict = current_unit = current_transit = None
            records = []
            if local is not None:
                if local.qty != 1:
                    _conflict()
                if local.stock_state == "in_transit":
                    try:
                        require_current_in_transit(local)
                    except CurrentStockStateError as exc:
                        raise HTTPException(409, "Assembly transit state is inconsistent") from exc
                    await effective_lot(session, core.services.stock, organization_id, local,
                        expected_state="in_transit")
                    dispatch = await session.scalar(select(PhysicalOperation).where(
                        PhysicalOperation.organization_id == organization_id,
                        PhysicalOperation.warehouse_id == warehouse_id,
                        PhysicalOperation.operation_id == local.dispatch_operation_id,
                        PhysicalOperation.kind == "transfer_dispatch").execution_options(populate_existing=True))
                    if dispatch is None:
                        _conflict()
                    await protect_blind_receipt(session, dispatch)
                    current_transit = {"id": local.id, "dispatch_operation_id": local.dispatch_operation_id, "qty": "1"}
                else:
                    await effective_lot(session, core.services.stock, organization_id, local)
                    qc_birth = await assembly_unit_qc_birth(session, organization_id, local)
                    records = await physical_assembly_qc_records(session, identity.id, organization_id, qc_birth)
                    verdict = "pending"
                    if records:
                        await physical_assembly_validate_qc(session, identity, *records[0])
                        verdict = records[0][1].verdict
                    current_unit = {"id": local.id, "location_id": local.location_id, "quality": local.quality,
                        "qty": "1", "reserved": format(local.reserved, ".6f")}
            qualified = current_anchor is not None and identity.anchor == current_anchor
            projection = {"identity_id": identity.id, "execution_id": identity.execution_id,
                "output_key": identity.output_key, "confirmed_revision_id": identity.anchor["confirmed_revision_id"],
                "effective_lot": identity.effective_lot, "current_unit": current_unit, "current_transit": current_transit,
                "qc_birth_operation_id": qc_birth, "verdict": verdict, "can_qc": False,
                "policy_inputs_digest": identity.anchor["policy_inputs_digest"], "serial_number": identity.serial_number,
                "qc": current_prepared.content["qc"] if qualified else None}
            result["identities"].append(projection)
            eligibility.append((projection, identity, qualified, current_unit is not None and local.quality == "quarantine"
                and local.reserved == 0 and not records))
            for proof in proved_shipments.get(identity.id, []):
                already = await session.scalar(select(PhysicalOperation.id).where(
                    PhysicalOperation.organization_id == organization_id, PhysicalOperation.kind == "assembly_return",
                    PhysicalOperation.result["assembly_return_v1"]["identity_id"].as_string() == identity.id,
                    PhysicalOperation.result["assembly_return_v1"]["ship_operation_id"].as_string() == proof["ship_operation_id"],
                    PhysicalOperation.result["assembly_return_v1"]["pick_id"].as_integer() == proof["pick_id"]).limit(1))
                if positive is None and already is None:
                    await prove_assembly_shipment_return(session, organization_id, warehouse_id, identity,
                        proof["ship_operation_id"], proof["pick_id"])
                    candidate = {key: proof[key] for key in ("identity_id", "ship_operation_id", "pick_id", "shipped_unit_id")}
                    candidate.update(warehouse_id=warehouse_id, can_return=False)
                    result["return_candidates"].append(candidate)
                    return_eligibility.append(candidate)
        skus = sorted({entry["effective_lot"]["sku_code"] for entry in result["identities"]})
        if current_anchor is not None:
            from modules.wms.assembly_quality import _source_dto, _source_method
            source = _source_dto(await _source_method(core)(session, organization_id, order_id, lock=True), organization_id, order_id)
            if source["status"] == "validated":
                skus = sorted(set(skus) | {source["composition"]["snapshot"]["output"]["sku_code"]})
        for sku in skus:
            batches = await core.services.stock.list_batches(session, organization_id, sku)
            if (not isinstance(batches, dict) or type(batches.get("truncated")) is not bool
                    or batches["truncated"] or not isinstance(batches.get("items"), list)):
                _conflict()
            for lot in batches["items"]:
                verified = await scoped_batch(session, core.services.stock, organization_id, lot["id"], sku)
                result["batches"].append(assembly_lot_snapshot(verified, lot["id"], sku))
        places = (await session.execute(select(PhysicalLocation, Location).join(Location,
            Location.id == PhysicalLocation.location_id).where(PhysicalLocation.warehouse_id == warehouse_id,
                PhysicalLocation.kind.in_(["quarantine", "returns"]), Location.is_active.is_(True))
            .order_by(PhysicalLocation.location_id))).all()
        result["locations"] = [{"location_id": physical.location_id, "code": physical.code, "kind": physical.kind}
            for physical, _legacy in places]
    # Repeat the registered source/assignment/H11 authority branch after the
    # last projection wait. A changed source cannot inherit earlier eligibility.
    final_h11 = await read_process_run_h11(session, core, organization_id, order_id, original_principal(), warehouse_id=warehouse_id)
    if any(final_h11[key] != h11[key] for key in ("actor", "read_scope", "assignment", "status", "source", "head", "document")):
        raise HTTPException(409, "Assembly output source changed during projection")
    fresh, _managed, subject = await _fresh_actor(session, core, organization_id, original_principal(), expected_actor=expected_actor)
    if fresh.issued_at != seed[6] or subject != seed[2] or fresh.local_user_id != seed[7]:
        raise HTTPException(403, "Assembly reader changed while reading outputs")
    operate = has_permission(core, fresh, "wms.operate")
    approve = has_permission(core, fresh, "wms.approve")
    for state, qualified, active, first_pass in execution_eligibility:
        state["can_consume_other"] = bool(operate and qualified and active and first_pass)
        state["can_output_other"] = bool(operate and qualified and first_pass)
    for projection, identity, qualified, physical_eligible in eligibility:
        nominee = None if not qualified else current_prepared.content["responsibility"]
        independent = bool(not qualified or current_prepared.content["independence"]["rule"] != "independent_required"
            or all(expected_actor != (executor["original"]["user_id"], executor["original"]["sub"])
                for executor in (identity.output_executor, identity.consume_executor)))
        projection["can_qc"] = bool(operate and approve and qualified and physical_eligible and independent
            and expected_actor == (nominee["qc_responsible_user_id"], nominee["qc_responsible_sub"]))
    for candidate in return_eligibility:
        candidate["can_return"] = bool(operate)
    try:
        if any(row["current_transit"] is not None for row in result["identities"] or []):
            result["schema_version"] = 2
            return AssemblyOutputReadProjectionV2.model_validate(result).model_dump(mode="json")
        for row in result["identities"] or []:
            del row["current_transit"]
        return AssemblyOutputReadProjection.model_validate(result).model_dump(mode="json")
    except (ValueError, TypeError) as exc:
        raise HTTPException(409, "Assembly output projection is inconsistent") from exc
