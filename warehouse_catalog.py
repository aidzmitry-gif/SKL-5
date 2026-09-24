"""Organization-owned physical warehouse catalogue for new WMS receipts."""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from core.runtime.deps import get_core, get_session
from core.services.auth import require_permission
from modules.wms.models import WarehouseDirectory

router = APIRouter(tags=["Справочник складов"])


class WarehouseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=255)


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
