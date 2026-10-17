"""ERP-only snapshots; legacy reserve/pick shadow movements never enter them."""
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import select

from modules.wms.models import WarehouseDirectory
from modules.wms.physical_models import (
    CountLocationLock,
    PhysicalLedger,
    PhysicalLocation,
    PhysicalOperation,
    StockUnit,
    WarehouseControl,
)
from modules.wms.physical_stock import _on_hand_units, digest, reject


async def warehouse_by_name(session, organization_id, name):
    return await session.scalar(select(WarehouseControl).join(WarehouseDirectory,
        WarehouseDirectory.id == WarehouseControl.warehouse_id).where(
        WarehouseControl.organization_id == organization_id, WarehouseDirectory.name == name,
        WarehouseControl.mode == "erp").with_for_update().execution_options(populate_existing=True))


async def _ledger_projection(session, warehouse, cutoff=None):
    from modules.wms.item_identity import (
        _origin_dimensions,
        _origin_operation_snapshot,
        _origin_transfer_dimensions,
        _origin_validate_operation,
        _OriginUnproven,
    )

    query = select(PhysicalLedger, PhysicalOperation).join(PhysicalOperation).where(PhysicalOperation.organization_id == warehouse.organization_id,
        PhysicalLedger.dimensions["warehouse_id"].as_integer() == warehouse.warehouse_id)
    if cutoff is not None:
        query = query.where(PhysicalLedger.id <= cutoff)
    paired = (await session.execute(query.order_by(PhysicalLedger.id))).all()
    rows = [row for row, _ in paired]
    identity = [[row.id, row.unit_id, format(row.qty_delta, "f"), row.dimensions] for row in rows]
    version = "erp:" + digest([warehouse.organization_id, warehouse.warehouse_id, identity])[:60]
    balances = defaultdict(lambda: Decimal(0))
    operations = {}
    for row, operation in paired:
        try:
            state, link = "on_hand", None
            from modules.wms.transfer_loss_protocol import loss_operation_indicator

            if loss_operation_indicator(operation.kind, operation.result):
                from modules.wms.transfer_loss_service import loss_ledger_projection

                cargo = await loss_ledger_projection(session, warehouse.organization_id,
                    _origin_operation_snapshot(operation), row)
                state, link = "in_transit", cargo["dispatch_operation_id"]
            elif operation.kind in {"transfer_dispatch", "transfer_receive"}:
                if operation.id not in operations:
                    operations[operation.id] = _origin_validate_operation(
                        _origin_operation_snapshot(operation), warehouse.organization_id)
                _, request, result = operations[operation.id]
                cargo = _origin_transfer_dimensions(row.dimensions)
                sent = result["transfer"]
                qty = Decimal(request["qty"])
                dispatching = operation.kind == "transfer_dispatch"
                parent = sent["source_unit_id"] if dispatching else sent["transit_unit_id"]
                child = sent["transit_unit_id"] if dispatching else sent["received_unit_id"]
                if row.unit_id == parent:
                    state = "on_hand" if dispatching else "in_transit"
                    expected_delta = -qty
                    expected_warehouse = sent["source_warehouse_id"]
                elif row.unit_id == child:
                    state = "in_transit" if dispatching else "on_hand"
                    expected_delta = qty
                    expected_warehouse = sent["source_warehouse_id"] if dispatching else sent["destination_warehouse_id"]
                else:
                    raise _OriginUnproven
                link = sent["dispatch_operation_id"] if state == "in_transit" else None
                if (row.qty_delta != expected_delta or cargo["warehouse_id"] != expected_warehouse
                        or row.dimensions["stock_state"] != state
                        or row.dimensions["dispatch_operation_id"] != link
                        or (state == "in_transit" and row.dimensions["container"] != sent["container"])
                        or (dispatching and row.unit_id == parent
                            and request["location_scan"] != f"LOC-{cargo['location_id']}")
                        or (not dispatching and row.unit_id == child
                            and cargo["location_id"] != request["destination_id"])):
                    raise _OriginUnproven
            else:
                cargo = _origin_dimensions(row.dimensions)
        except _OriginUnproven:
            reject("Состояние остатка в журнале ERP не подтверждено; требуется сверка")
        balances[(row.unit_id, cargo["sku_code"], state, link)] += row.qty_delta
    if any(qty < 0 for qty in balances.values()):
        reject("Физический остаток ERP не совпадает с его журналом; требуется сверка")
    totals = defaultdict(lambda: Decimal(0))
    for (_, sku, state, _), qty in balances.items():
        if state == "on_hand":
            totals[sku] += qty
    return dict(totals), version, rows[-1].id if rows else 0, dict(balances)


async def history(session, warehouse, cutoff=None):
    totals, version, latest, _ = await _ledger_projection(session, warehouse, cutoff)
    return totals, version, latest


async def snapshot(session, warehouse):
    if await session.scalar(select(CountLocationLock.location_id).join(PhysicalLocation,
        PhysicalLocation.location_id == CountLocationLock.location_id).where(PhysicalLocation.warehouse_id == warehouse.warehouse_id)):
        reject("Учётные количества скрыты до завершения слепого пересчёта")
    if not warehouse.opening_count_id:
        reject("ERP warehouse has no approved opening count")
    totals, version, cutoff, expected = await _ledger_projection(session, warehouse)
    units = (await session.scalars(select(StockUnit).where(StockUnit.warehouse_id == warehouse.warehouse_id))).all()
    _on_hand_units(units)
    actual = defaultdict(lambda: Decimal(0))
    for unit in units:
        actual[(unit.id, unit.sku_code, unit.stock_state, unit.dispatch_operation_id)] += unit.qty
    if any(actual[key] != expected.get(key, Decimal(0)) for key in set(actual) | set(expected)):
        reject("Физический остаток ERP не совпадает с его журналом; требуется сверка")
    return totals, version, cutoff
