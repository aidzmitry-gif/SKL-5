"""Location ownership checks shared by warehouse and production operations."""

from fastapi import HTTPException
from sqlalchemy import select

from modules.wms.models import Location, WarehouseDirectory


async def owned_warehouse(session, organization_id: int, warehouse: str):
    if type(organization_id) is not int or organization_id <= 0 or not warehouse.strip():
        raise HTTPException(422, "Укажите юрлицо и конкретный склад")
    row = await session.scalar(select(WarehouseDirectory).where(
        WarehouseDirectory.organization_id == organization_id,
        WarehouseDirectory.name == warehouse,
        WarehouseDirectory.is_active.is_(True),
    ).with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise HTTPException(422, "Активный склад не найден в указанном юрлице")
    return row


async def validate_location_ids(session, organization_id: int, warehouse: str, *location_ids):
    ids = sorted({value for value in location_ids if value is not None})
    if not ids:
        return []
    await owned_warehouse(session, organization_id, warehouse)
    rows = (await session.scalars(select(Location).where(Location.id.in_(ids))
        .order_by(Location.id).with_for_update().execution_options(populate_existing=True))).all()
    if len(rows) != len(ids) or any(
        row.organization_id != organization_id or row.warehouse != warehouse or not row.is_active
        for row in rows
    ):
        raise HTTPException(422, "Ячейка должна принадлежать выбранным юрлицу и складу и быть активной")
    return rows
