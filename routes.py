"""HTTP-API модуля WMS. Монтируется под префиксом ``/wms``."""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.runtime.deps import get_session
from modules.wms.models import StockMovement
from modules.wms.schemas import StockMovementCreate, StockMovementOut

router = APIRouter(tags=["wms"])


@router.get("/movements", response_model=list[StockMovementOut])
async def list_movements(session: AsyncSession = Depends(get_session)):
    """Движения по складу (приход/расход)."""
    return (
        await session.execute(select(StockMovement).order_by(StockMovement.id.desc()))
    ).scalars().all()


@router.post("/movements", response_model=StockMovementOut, status_code=201)
async def create_movement(
    payload: StockMovementCreate, session: AsyncSession = Depends(get_session)
):
    """Зафиксировать движение по складу."""
    obj = StockMovement(
        sku_code=payload.sku_code,
        warehouse=payload.warehouse,
        kind=payload.kind,
        qty=Decimal(str(payload.qty)),
    )
    session.add(obj)
    await session.commit()
    await session.refresh(obj)
    return obj
