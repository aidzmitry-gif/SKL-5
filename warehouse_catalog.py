"""Organization-owned physical warehouse catalogue for new WMS receipts."""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from core.runtime.deps import get_core, get_session
from core.services.auth import require_permission
from modules.wms.models import ReservationVersion, Task, WarehouseDirectory
from modules.wms.reservations import _latest_versions

router = APIRouter(tags=["Справочник складов"])


class WarehouseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=255)


class WarehouseModeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_wms_enabled: bool
    wms_enabled: bool


class WarehouseStockPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_prohibit_negative_stock: bool
    prohibit_negative_stock: bool


async def transaction(session=Depends(get_session)):
    try:
        yield session
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "Warehouse name was registered concurrently; refresh the catalogue") from exc
    except Exception:
        await session.rollback()
        raise


async def scoped(org_id, session, core, user):
    accounting = getattr(core.services, "accounting", None)
    if accounting is None:
        raise HTTPException(503, "Organization access service is unavailable")
    return await accounting.source_member(session, org_id, user)


def output(row):
    return {"id": row.id, "organization_id": row.organization_id,
            "name": row.name, "title": row.title, "is_active": row.is_active,
            "wms_enabled": row.wms_enabled,
            "prohibit_negative_stock": row.prohibit_negative_stock,
            "created_by": row.created_by, "created_at": row.created_at}


@router.get("/organizations/{org_id}/warehouse-options")
async def warehouse_options(org_id: int, q: str = Query("", max_length=100),
                            session=Depends(transaction, scope="function"),
                            core=Depends(get_core), user=Depends(require_permission("wms.count"))):
    await scoped(org_id, session, core, user)
    query = select(WarehouseDirectory).where(WarehouseDirectory.organization_id == org_id,
                                             WarehouseDirectory.is_active.is_(True))
    if q.strip():
        term = q.strip()
        query = query.where(or_(WarehouseDirectory.name.icontains(term, autoescape=True),
                                WarehouseDirectory.title.icontains(term, autoescape=True)))
    rows = (await session.scalars(query.order_by(WarehouseDirectory.name, WarehouseDirectory.id).limit(51))).all()
    return {"organization_id": org_id, "items": [output(row) for row in rows[:50]],
            "truncated": len(rows) > 50}


@router.post("/organizations/{org_id}/warehouses", status_code=201)
async def create_warehouse(org_id: int, data: WarehouseCreate,
                           session=Depends(transaction, scope="function"),
                           core=Depends(get_core), user=Depends(require_permission("wms.count"))):
    actor = await scoped(org_id, session, core, user)
    existing = await session.scalar(select(WarehouseDirectory).where(
        WarehouseDirectory.organization_id == org_id, WarehouseDirectory.name == data.name,
    ).with_for_update())
    if existing is not None:
        if not existing.is_active or existing.title != data.title:
            raise HTTPException(409, "Warehouse name already identifies different catalogue data")
        return output(existing)
    row = WarehouseDirectory(organization_id=org_id, name=data.name, title=data.title,
                             is_active=True, created_by=actor)
    session.add(row)
    await session.flush()
    return output(row)


@router.patch("/organizations/{org_id}/warehouses/{warehouse_id}/stock-policy")
async def update_stock_policy(org_id: int, warehouse_id: int, data: WarehouseStockPolicyUpdate,
                              session=Depends(transaction, scope="function"),
                              core=Depends(get_core), user=Depends(require_permission("wms.count"))):
    accounting = getattr(core.services, "accounting", None)
    authorize = getattr(accounting, "warehouse_mode_admin", None)
    if not callable(authorize):
        raise HTTPException(503, "Управление настройками склада недоступно")
    actor = await authorize(session, org_id, user)
    row = await session.scalar(select(WarehouseDirectory).where(
        WarehouseDirectory.id == warehouse_id,
        WarehouseDirectory.organization_id == org_id,
        WarehouseDirectory.is_active.is_(True),
    ).with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise HTTPException(404, "Активный склад не найден в этой организации")
    if type(row.prohibit_negative_stock) is not bool:
        raise HTTPException(409, "Настройка контроля остатков не определена; требуется проверка склада")
    if row.prohibit_negative_stock != data.expected_prohibit_negative_stock:
        raise HTTPException(409, "Настройка склада уже изменена. Обновите страницу")
    if row.prohibit_negative_stock == data.prohibit_negative_stock:
        return output(row)
    previous = row.prohibit_negative_stock
    row.prohibit_negative_stock = data.prohibit_negative_stock
    core.event_bus.emit(session, "wms.warehouse.stock_policy_changed", {
        "organization_id": org_id, "warehouse_id": row.id, "warehouse": row.name,
        "previous_prohibit_negative_stock": previous,
        "prohibit_negative_stock": row.prohibit_negative_stock, "actor": actor,
    })
    await session.flush()
    return output(row)


@router.patch("/organizations/{org_id}/warehouses/{warehouse_id}/execution-mode")
async def update_execution_mode(org_id: int, warehouse_id: int, data: WarehouseModeUpdate,
                                session=Depends(transaction, scope="function"),
                                core=Depends(get_core), user=Depends(require_permission("wms.count"))):
    accounting = getattr(core.services, "accounting", None)
    authorize = getattr(accounting, "warehouse_mode_admin", None)
    if not callable(authorize):
        raise HTTPException(503, "Warehouse mode administration is unavailable")
    actor = await authorize(session, org_id, user)
    row = await session.scalar(select(WarehouseDirectory).where(
        WarehouseDirectory.id == warehouse_id,
        WarehouseDirectory.organization_id == org_id,
        WarehouseDirectory.is_active.is_(True),
    ).with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise HTTPException(404, "Active warehouse not found in this organization")
    if type(row.wms_enabled) is not bool:
        raise HTTPException(409, "Stored warehouse mode requires reconciliation")
    if row.wms_enabled != data.expected_wms_enabled:
        raise HTTPException(409, "Warehouse mode changed; refresh the catalogue and retry")
    if row.wms_enabled == data.wms_enabled:
        return output(row)

    latest = _latest_versions(org_id)
    outstanding = await session.scalar(select(ReservationVersion.id).join(latest,
        (ReservationVersion.source == latest.c.source)
        & (ReservationVersion.version == latest.c.version)
    ).where(ReservationVersion.organization_id == org_id,
            ReservationVersion.warehouse == row.name,
            ReservationVersion.qty > 0).limit(1))
    active_task = await session.scalar(select(Task.id).where(
        Task.warehouse == row.name,
        Task.status.in_(("open", "in_progress")),
        or_(Task.organization_id == org_id, Task.organization_id.is_(None)),
    ).order_by(Task.id).limit(1).with_for_update())
    if outstanding is not None or active_task is not None:
        raise HTTPException(409, "Warehouse mode cannot change while reservations or active tasks remain")

    previous = row.wms_enabled
    row.wms_enabled = data.wms_enabled
    core.event_bus.emit(session, "wms.warehouse.execution_mode_changed", {
        "organization_id": org_id, "warehouse_id": row.id, "warehouse": row.name,
        "previous_wms_enabled": previous, "wms_enabled": row.wms_enabled, "actor": actor,
    })
    await session.flush()
    return output(row)
