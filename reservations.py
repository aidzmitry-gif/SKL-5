"""Explicit manual reservation versions. Legacy sales events are not imported here."""
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.runtime.core import Core
from core.runtime.deps import get_core, get_session
from core.services.auth import require_permission
from modules.wms.models import ReservationVersion
from modules.wms.organization import visible_organization_ids

router = APIRouter()

class ReservationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    organization_id: int = Field(gt=0, strict=True)
    source: str = Field(min_length=1, max_length=200)
    version: int = Field(gt=0, strict=True)
    sku_code: str = Field(min_length=1, max_length=64)
    warehouse: str = Field(min_length=1, max_length=128)
    qty: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    evidence: str = Field(min_length=1, max_length=1000)


def output(row):
    return {"id": row.id, "organization_id": row.organization_id, "source": row.source,
            "version": row.version, "sku_code": row.sku_code, "warehouse": row.warehouse,
            "qty": format(row.qty, ".2f"), "evidence": row.evidence, "actor": row.actor}


@router.post("/reservations", status_code=201)
async def record_reservation(payload: ReservationInput, session: AsyncSession = Depends(get_session),
                             core: Core = Depends(get_core), user=Depends(require_permission("wms.count"))):
    if payload.source.startswith("invoice:"):
        raise HTTPException(422, "Invoice reservations require the verified invoice workflow")
    gateway = getattr(core.services, "accounting", None)
    if gateway is None:
        raise HTTPException(503, "Сервис доступа к юрлицам недоступен")
    actor = await gateway.source_member(session, payload.organization_id, user)
    # Organization lock also serializes the first version of a previously absent key.
    rows = (await session.scalars(select(ReservationVersion).where(
        ReservationVersion.organization_id == payload.organization_id,
        ReservationVersion.source == payload.source,
    ).order_by(ReservationVersion.version.desc()))).all()
    same = next((row for row in rows if row.version == payload.version), None)
    if same:
        if any(getattr(same, key) != value for key, value in payload.model_dump().items()):
            raise HTTPException(409, "Эта версия резерва уже записана с другими данными")
        return output(same)
    latest = rows[0] if rows else None
    if payload.version != (latest.version + 1 if latest else 1):
        raise HTTPException(409, "Версии резерва должны поступать последовательно")
    if latest and (latest.sku_code != payload.sku_code or latest.warehouse != payload.warehouse):
        raise HTTPException(409, "Номенклатура и склад исходной строки резерва неизменны")
    if latest is None and payload.qty == 0:
        raise HTTPException(422, "Первый резерв должен иметь положительное количество")
    row = ReservationVersion(**payload.model_dump(), actor=actor)
    session.add(row)
    await session.flush()
    core.event_bus.emit(session, "wms.reservation.recorded", output(row))
    await session.commit()
    return output(row)


def _latest_versions(organization_id):
    return select(ReservationVersion.source, func.max(ReservationVersion.version).label("version")).where(
        ReservationVersion.organization_id == organization_id,
    ).group_by(ReservationVersion.source).subquery()


@router.get("/reservations")
async def list_reservations(organization_id: int = Query(gt=0), session: AsyncSession = Depends(get_session),
                            core: Core = Depends(get_core), user=Depends(require_permission("wms.read"))):
    await visible_organization_ids(session, core, user, organization_id)
    latest = _latest_versions(organization_id)
    rows = (await session.scalars(select(ReservationVersion).join(latest,
        (ReservationVersion.source == latest.c.source) & (ReservationVersion.version == latest.c.version),
    ).where(ReservationVersion.organization_id == organization_id).order_by(ReservationVersion.source))).all()
    return [output(row) for row in rows]


@router.get("/reservations/summary")
async def reservation_summary(organization_id: int = Query(gt=0), session: AsyncSession = Depends(get_session),
                              core: Core = Depends(get_core), user=Depends(require_permission("wms.read"))):
    await visible_organization_ids(session, core, user, organization_id)
    latest = _latest_versions(organization_id)
    rows = (await session.execute(select(
        ReservationVersion.sku_code, ReservationVersion.warehouse,
        func.sum(ReservationVersion.qty), func.count(ReservationVersion.id),
    ).join(latest, (ReservationVersion.source == latest.c.source) & (ReservationVersion.version == latest.c.version))
      .where(ReservationVersion.organization_id == organization_id, ReservationVersion.qty > 0)
      .group_by(ReservationVersion.sku_code, ReservationVersion.warehouse)
      .order_by(ReservationVersion.sku_code, ReservationVersion.warehouse))).all()
    return [{"organization_id": organization_id, "sku_code": sku, "warehouse": warehouse,
             "qty": format(qty, ".2f"), "source_count": count} for sku, warehouse, qty, count in rows]


@router.get("/reservations/history")
async def reservation_history(organization_id: int = Query(gt=0), source: str = Query(min_length=1, max_length=200),
                              session: AsyncSession = Depends(get_session), core: Core = Depends(get_core),
                              user=Depends(require_permission("wms.read"))):
    await visible_organization_ids(session, core, user, organization_id)
    rows = (await session.scalars(select(ReservationVersion).where(
        ReservationVersion.organization_id == organization_id, ReservationVersion.source == source,
    ).order_by(ReservationVersion.version))).all()
    return [output(row) for row in rows]
