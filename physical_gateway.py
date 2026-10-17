"""Shared sales/WMS stock facade. Cutover is per physical warehouse, never global."""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid5

from sqlalchemy import select

from modules.wms.models import WarehouseDirectory
from modules.wms.physical_models import (
    CountLocationLock,
    PhysicalLocation,
    StockUnit,
    WarehouseControl,
)
from modules.wms.physical_schemas import Release, Reserve
from modules.wms.physical_stock import (
    PhysicalStock,
    _on_hand_units,
    control,
    decimal_text,
    eligible,
    pending_move_qty,
)


class PhysicalStockGateway:
    def __init__(self, reference, event_bus):
        self.reference = reference
        self.event_bus = event_bus

    async def batch_by_id(self, session, organization_id, batch_id):
        return await self.reference.batch_by_id(session, organization_id, batch_id)

    async def register_batch(self, session, organization_id, values):
        return await self.reference.register_batch(session, organization_id, values)

    async def list_batches(self, session, organization_id, sku_code=None):
        return await self.reference.list_batches(session, organization_id, sku_code)

    async def assembly_workplace(self, session, organization_id, warehouse_id,
                                 order_id, specifications):
        from modules.wms.assembly_workplace import read_assembly_workplace

        return await read_assembly_workplace(
            session,
            organization_id,
            warehouse_id,
            order_id,
            specifications,
            stock_gateway=self,
        )

    async def assembly_order_other_workplaces(
        self, session, organization_id, order_id, selected_warehouse_id, specifications,
    ):
        from modules.wms.assembly_workplace import read_assembly_order_other_workplaces

        return await read_assembly_order_other_workplaces(
            session, organization_id, order_id, selected_warehouse_id, specifications,
            stock_gateway=self,
        )

    async def assembly_warehouse(self, session, organization_id, warehouse_id, *, lock=False):
        from modules.wms.assembly_workplace import read_assembly_warehouse

        return await read_assembly_warehouse(
            session, organization_id, warehouse_id, lock=lock,
        )

    async def assembly_station(
        self, session, organization_id, warehouse_id, station_location_id,
        *, lock=False, require_active=True,
    ):
        from modules.wms.assembly_workplace import read_assembly_station

        return await read_assembly_station(
            session, organization_id, warehouse_id, station_location_id,
            lock=lock, require_active=require_active,
        )

    async def assembly_stations(self, session, organization_id, warehouse_id):
        from modules.wms.assembly_workplace import read_assembly_stations

        return await read_assembly_stations(session, organization_id, warehouse_id)

    async def _physical_items(self, session, items):
        if not items:
            return False
        explicit = ["warehouse_id" in row for row in items]
        if any(explicit) and not all(explicit):
            raise ValueError("Нельзя смешивать источники остатка в одной операции")
        if all(explicit):
            required = {"organization_id", "warehouse_id", "operation_id", "source", "container"}
            if any(not required.issubset(row) for row in items):
                raise ValueError("Нужны организация, склад, операция, строка заказа и контейнер")
            return True
        switched = (await session.scalars(select(WarehouseDirectory.name).join(WarehouseControl,
            WarehouseControl.warehouse_id == WarehouseDirectory.id).where(WarehouseControl.mode == "erp"))).all()
        native_preparing = await session.scalar(select(WarehouseControl.warehouse_id).where(
            WarehouseControl.mode == "preparing", WarehouseControl.onec_ref.is_(None)).limit(1))
        if switched or native_preparing is not None:
            # Legacy sales requests lack the company/address identity required to
            # safely distinguish a moved warehouse from its 1C mirror or a native
            # warehouse whose opening balance is not yet known.
            raise ValueError("Выберите организацию и warehouse_id для адресации физического остатка ERP")
        return False

    async def lock_reservation_items(self, session, items):
        if not await self._physical_items(session, items):
            return await self.reference.lock_reservation_items(session, items)
        keys = {(row["organization_id"], row["warehouse_id"]) for row in items}
        for org, warehouse in sorted(keys):
            await control(session, warehouse, org)

    async def reserve(self, session, items):
        if not await self._physical_items(session, items):
            return await self.reference.reserve(session, items)
        output = []
        for item in items:
            operation_id = uuid5(UUID(str(item["operation_id"])), f"reserve:{item['source']}:{item['sku_code']}")
            data = Reserve(kind="reserve", operation_id=operation_id, warehouse_id=item["warehouse_id"],
                sku_code=item["sku_code"], qty=item["qty"], source=item["source"], container=item["container"],
                characteristic=item.get("characteristic", ""), purpose=item.get("purpose", ""),
                min_shelf_days=item.get("min_shelf_days", 0), due_at=item.get("due_at"))
            result = await PhysicalStock(self.reference, self.event_bus).execute(session, item["organization_id"], data,
                item.get("actor", "sales"))
            directory = await session.get(WarehouseDirectory, item["warehouse_id"])
            output.append({**item, "qty": decimal_text(sum((Decimal(h["qty"]) for h in result["holds"]), Decimal(0))),
                "warehouse": directory.name, "source_of_stock": "erp", "holds": result["holds"]})
        return output

    async def release(self, session, items):
        if not await self._physical_items(session, items):
            return await self.reference.release(session, items)
        output = []
        seen = set()
        for item in items:
            key = item["organization_id"], item["warehouse_id"], item["source"]
            if key in seen:
                continue
            seen.add(key)
            data = Release(kind="release", operation_id=uuid5(UUID(str(item["operation_id"])), f"release:{item['source']}"),
                warehouse_id=item["warehouse_id"], source=item["source"], evidence=item.get("evidence", "sales cancellation"))
            result = await PhysicalStock(self.reference, self.event_bus).execute(session, item["organization_id"], data, item.get("actor", "sales"))
            output.append({**item, "qty": result["released"], "source_of_stock": "erp"})
        return output

    async def stock_by_sku(self, session, sku_code, organization_id=None, warehouse_id=None):
        if warehouse_id is not None:
            if organization_id is None:
                raise ValueError("Для warehouse_id требуется организация")
            native_preparing = await session.scalar(select(WarehouseControl).where(
                WarehouseControl.organization_id == organization_id,
                WarehouseControl.warehouse_id == warehouse_id,
                WarehouseControl.mode == "preparing", WarehouseControl.onec_ref.is_(None)))
            if native_preparing is not None:
                from fastapi import HTTPException
                raise HTTPException(409, "Начальный остаток неизвестен до утверждения полного пересчёта")
        query = select(WarehouseControl).where(WarehouseControl.mode == "erp")
        if organization_id is not None:
            query = query.where(WarehouseControl.organization_id == organization_id)
        if warehouse_id is not None:
            query = query.where(WarehouseControl.warehouse_id == warehouse_id)
        warehouses = (await session.scalars(query)).all()
        if warehouses and await session.scalar(select(CountLocationLock.location_id).join(PhysicalLocation,
            PhysicalLocation.location_id == CountLocationLock.location_id).where(
                PhysicalLocation.warehouse_id.in_([w.warehouse_id for w in warehouses]))):
            from fastapi import HTTPException
            raise HTTPException(409, "Остатки скрыты на время слепого пересчёта; используйте документ пересчёта")
        legacy = await self.reference.stock_by_sku(session, sku_code)
        all_names = (await session.scalars(select(WarehouseDirectory.name).join(WarehouseControl,
            WarehouseControl.warehouse_id == WarehouseDirectory.id).where(WarehouseControl.mode == "erp"))).all()
        native_preparing_query = select(WarehouseDirectory.name).join(WarehouseControl,
            WarehouseControl.warehouse_id == WarehouseDirectory.id).where(WarehouseControl.mode == "preparing",
                WarehouseControl.onec_ref.is_(None))
        if organization_id is not None:
            native_preparing_query = native_preparing_query.where(WarehouseControl.organization_id == organization_id)
        native_preparing_names = (await session.scalars(native_preparing_query)).all()
        excluded_legacy_names = set(all_names) | set(native_preparing_names)
        if not excluded_legacy_names and warehouse_id is None:
            return legacy
        legacy_rows = [] if not legacy or warehouse_id else [r for r in legacy["rows"] if r["warehouse"] not in excluded_legacy_names]
        rows = [{**r, "source_of_stock": "1c"} for r in legacy_rows]
        if organization_id is not None:
            for warehouse in warehouses:
                units = (await session.scalars(select(StockUnit).where(StockUnit.warehouse_id == warehouse.warehouse_id,
                    StockUnit.sku_code == sku_code))).all()
                if not units:
                    continue
                units = _on_hand_units(units)
                directory = await session.get(WarehouseDirectory, warehouse.warehouse_id)
                physical = sum((unit.qty for unit in units), Decimal(0))
                reserved = sum((unit.reserved for unit in units), Decimal(0))
                available = Decimal(0)
                for unit in units:
                    reason, _ = await eligible(session, self.reference, warehouse, unit)
                    if not reason:
                        available += unit.qty - unit.reserved - await pending_move_qty(session, unit.id)
                # qty_available keeps the legacy physical-total contract. Eligible
                # availability is explicit and must be used to allocate an order.
                rows.append({"warehouse": directory.name, "warehouse_id": warehouse.warehouse_id,
                    "organization_id": organization_id, "source_of_stock": "erp",
                    "qty_available": decimal_text(physical), "qty_reserved": decimal_text(reserved),
                    "qty_eligible_free": decimal_text(available), "qty_forecast": "0", "price": None, "cost": None})
        if not rows:
            return None
        return {"rows": rows, "total_available": decimal_text(sum((Decimal(str(r["qty_available"])) for r in rows), Decimal(0))),
            "total_reserved": decimal_text(sum((Decimal(str(r["qty_reserved"])) for r in rows), Decimal(0))),
            "price": legacy.get("price") if legacy else None, "cost": legacy.get("cost") if legacy else None,
            "updated_at": legacy.get("updated_at") if legacy else None}

    async def batches_by_sku(self, session, sku_code):
        return await self.reference.batches_by_sku(session, sku_code)

    async def physical_availability(self, session, organization_id, sku_codes):
        rows, basis = [], {}
        warehouses = (await session.scalars(select(WarehouseControl).where(WarehouseControl.organization_id == organization_id,
            WarehouseControl.mode == "erp").order_by(WarehouseControl.warehouse_id))).all()
        for warehouse in warehouses:
            await control(session, warehouse.warehouse_id, organization_id)
            directory = await session.get(WarehouseDirectory, warehouse.warehouse_id)
            basis[directory.name] = {"source": "erp", "warehouse_id": warehouse.warehouse_id,
                "opening_count_id": warehouse.opening_count_id}
            for sku in sku_codes:
                result = await self.stock_by_sku(session, sku, organization_id, warehouse.warehouse_id)
                if result:
                    row = result["rows"][0]
                    rows.append({"sku_code": sku, "warehouse": directory.name, "warehouse_id": warehouse.warehouse_id,
                        "physical": row["qty_available"], "reserved": row["qty_reserved"], "free": row["qty_eligible_free"]})
        return {"rows": rows, "basis_by_warehouse": basis}

