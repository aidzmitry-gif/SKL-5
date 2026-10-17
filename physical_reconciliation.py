"""Read-side 1C observations, strictly separate from ERP physical stock."""
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import select

from modules.wms.physical_models import ReconciliationSnapshot, StockUnit, WarehouseControl
from modules.wms.physical_stock import canonical, decimal_text, reject


async def record_snapshot(session, warehouse, snapshot_ref, warehouse_ref, rows, *, scope="sku"):
    if warehouse.mode != "erp" or warehouse.onec_ref != warehouse_ref:
        reject("Сверка требует точного идентификатора переключённого склада")
    if scope not in {"sku", "sku_characteristic_purpose"}:
        reject("Неизвестный состав размерностей сверки", 422)
    external, observed = defaultdict(lambda: Decimal(0)), defaultdict(lambda: Decimal(0))
    def key(row):
        return row["sku_code"] if scope == "sku" else canonical([row["sku_code"], row.get("characteristic", ""), row.get("purpose", "")])
    for row in rows:
        external[key(row)] += Decimal(row["qty"])
    units = (await session.scalars(select(StockUnit).where(StockUnit.warehouse_id == warehouse.warehouse_id))).all()
    for unit in units:
        observed[key({"sku_code": unit.sku_code, "characteristic": unit.characteristic, "purpose": unit.purpose})] += unit.qty
    differences = {code: {"erp": decimal_text(observed[code]), "onec": decimal_text(external[code]),
        "difference": decimal_text(observed[code] - external[code])} for code in sorted(set(external) | set(observed))
        if external[code] != observed[code]}
    snapshot = ReconciliationSnapshot(warehouse_id=warehouse.warehouse_id, snapshot_ref=snapshot_ref,
        external={"scope": scope, "quantities": {k: decimal_text(v) for k, v in external.items()}},
        observed={"scope": scope, "quantities": {k: decimal_text(v) for k, v in observed.items()}}, differences=differences)
    session.add(snapshot)
    await session.flush()
    return {"id": snapshot.id, "snapshot_ref": snapshot_ref, "scope": scope, "differences": differences}


async def on_stock_observed(payload, ctx):
    """Imports with unresolved company/warehouse identity cannot certify a comparison."""
    for scope in payload.get("warehouses", []):
        org = scope.get("organization_id")
        if type(org) is not int or not scope.get("warehouse_ref") or scope.get("complete") is not True:
            continue
        await ctx.services.accounting.lock_event_organization(ctx.session, org)
        warehouse = await ctx.session.scalar(select(WarehouseControl).where(WarehouseControl.organization_id == org,
            WarehouseControl.onec_ref == scope["warehouse_ref"], WarehouseControl.mode == "erp").with_for_update())
        if warehouse:
            await record_snapshot(ctx.session, warehouse, payload["snapshot_ref"], scope["warehouse_ref"], scope["rows"],
                scope=scope.get("scope", "sku"))
