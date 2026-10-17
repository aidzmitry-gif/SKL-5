"""Release ERP holds only after the native invoice proof has been verified.

The caller owns organization/document locks and the transaction. This helper is
internal; no TSD command can use it to bypass sales cancellation/remainder rules.
"""
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from modules.wms.models import WarehouseDirectory
from modules.wms.physical_models import (
    PhysicalHold,
    PhysicalOperation,
    PhysicalTask,
    StockUnit,
    WarehouseControl,
)
from modules.wms.physical_stock import control, decimal_text, digest, location, reject


async def release_verified_sources(session, org, release_key, lines, actor):
    warehouses = (await session.execute(select(WarehouseControl, WarehouseDirectory.name)
        .join(WarehouseDirectory, WarehouseControl.warehouse_id == WarehouseDirectory.id)
        .where(WarehouseControl.organization_id == org, WarehouseControl.mode == "erp")
        .order_by(WarehouseControl.warehouse_id))).all()
    for warehouse, name in warehouses:
        addressed = [line for line in lines if line["warehouse"] == name and Decimal(line["released_qty"]) > 0]
        if not addressed:
            continue
        await control(session, warehouse.warehouse_id, org)
        operation_id = str(uuid5(NAMESPACE_URL, f"wms:verified-release:{org}:{release_key}:{warehouse.warehouse_id}"))
        request_hash = digest({"release_key": str(release_key), "lines": addressed})
        saved = await session.scalar(select(PhysicalOperation).where(PhysicalOperation.organization_id == org,
            PhysicalOperation.operation_id == operation_id))
        if saved:
            if saved.request_hash != request_hash:
                reject("Подтверждённая отмена уже имеет другое содержимое")
            continue
        total = Decimal(0)
        for line in addressed:
            holds = (await session.scalars(select(PhysicalHold).where(PhysicalHold.warehouse_id == warehouse.warehouse_id,
                PhysicalHold.source == line["source"]).order_by(PhysicalHold.id))).all()
            remaining = sum((h.qty - h.shipped - h.released for h in holds), Decimal(0))
            if remaining != Decimal(line["released_qty"]) or any(h.picked != h.shipped for h in holds):
                reject("Отмена счёта не совпадает с резервом ERP; сначала верните неподтверждённый подбор")
            for hold in holds:
                unit = await session.get(StockUnit, hold.unit_id)
                await location(session, warehouse.warehouse_id, unit.location_id)
                qty = hold.qty - hold.picked - hold.released
                if qty < 0 or unit.reserved < qty:
                    reject("Резерв ERP требует сверки перед отменой")
                unit.reserved -= qty
                hold.released += qty
                total += qty
            tasks = (await session.scalars(select(PhysicalTask).where(PhysicalTask.warehouse_id == warehouse.warehouse_id,
                PhysicalTask.source == line["source"], PhysicalTask.kind == "pick",
                PhysicalTask.status.in_(["open", "claimed"])))).all()
            for task in tasks:
                task.status = "canceled"
        session.add(PhysicalOperation(organization_id=org, warehouse_id=warehouse.warehouse_id,
            operation_id=operation_id, kind="verified_invoice_release", request_hash=request_hash, actor=actor,
            result={"operation_id": operation_id, "kind": "verified_invoice_release",
                "warehouse_id": warehouse.warehouse_id, "release_key": str(release_key), "released": decimal_text(total)}))
    await session.flush()
