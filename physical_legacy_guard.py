"""Old shadow-ledger writers cannot fulfill an ERP-owned warehouse."""
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select

from core.runtime.deps import get_session
from core.services.auth import get_current_user
from modules.wms.models import InventoryCount, Location, Receipt, Task, WarehouseDirectory
from modules.wms.physical_models import CountLocationLock, WarehouseControl


async def guard_legacy_write(request: Request, session=Depends(get_session), user=Depends(get_current_user)):
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        if any(part in request.url.path for part in ("/stock", "/balances", "/movements", "/inventory", "/reconciliation", "/dashboard")):
            if await session.scalar(select(CountLocationLock.location_id).limit(1)):
                raise HTTPException(409, "На время слепого пересчёта используйте адресный документ ТСД")
        return
    if request.url.path.endswith(("/preview", "/history", "/accounting-preview")):
        return
    try:
        payload = await request.json()
    except ValueError:
        payload = {}
    names, ids = set(), set()
    def visit(value):
        if isinstance(value, dict):
            if isinstance(value.get("warehouse"), str):
                names.add(value["warehouse"])
            if type(value.get("warehouse_id")) is int:
                ids.add(value["warehouse_id"])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(payload)
    for parameter, model in (("receipt_id", Receipt), ("task_id", Task), ("count_id", InventoryCount), ("loc_id", Location)):
        value = request.path_params.get(parameter)
        if value is not None:
            row = await session.get(model, int(value))
            if row:
                names.add(row.warehouse)
    if not names and not ids:
        return
    query = select(WarehouseControl).join(WarehouseDirectory, WarehouseDirectory.id == WarehouseControl.warehouse_id).where(
        WarehouseControl.mode == "erp")
    org = request.path_params.get("organization_id", request.path_params.get("org"))
    if org is not None:
        query = query.where(WarehouseControl.organization_id == int(org))
    rows = (await session.execute(query.add_columns(WarehouseDirectory.name))).all()
    if any(cfg.warehouse_id in ids or name in names for cfg, name in rows):
        raise HTTPException(409, "Склад ведёт физический учёт ERP: выполните операцию через ТСД и общий складской шлюз")
