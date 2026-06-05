"""HTTP-API модуля WMS. Монтируется под префиксом ``/wms``."""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.runtime.deps import get_session
from core.runtime.funnel import FunnelBoardOut, FunnelCard, build_board
from modules.wms.models import StockMovement, WarehouseOp
from modules.wms.schemas import (
    StageUpdate,
    StockMovementCreate,
    StockMovementOut,
    WarehouseOpCreate,
    WarehouseOpOut,
)
from modules.wms.stages import STAGES

router = APIRouter(tags=["wms"])


# --- Журнал движений (приход/расход; наполняется и событиями других модулей) ---


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


# --- Воронка складских операций (поступление → отгрузка) ---


# Подпись основной кнопки по стадии (как пилюли в референсе склада).
_WMS_ACTIONS = {
    "inbound": "Создать приёмку",
    "receiving": "Завершить приёмку",
    "qc": "Принять качество",
    "putaway": "Подтвердить место",
    "picking": "К упаковке",
    "ready": "Передать клиенту",
    "shipped": "Отслеживать заказ",
}


def _to_card(r: WarehouseOp) -> FunnelCard:
    tags: list[str] = []
    if r.items_count:
        tags.append(f"{r.items_count} поз.")
    if r.zone:
        tags.append(r.zone)
    # Soft-панель пересчёта на приёмке/контроле
    details: list[dict[str, str]] = []
    if r.stage in ("receiving", "qc") and r.items_count:
        details = [
            {"k": "План", "v": f"{r.items_count} поз."},
            {"k": "Принято", "v": f"{r.items_count} поз."},
            {"k": "Отклонения", "v": "нет"},
        ]
    return FunnelCard(
        id=r.id,
        code=r.number or f"ОП-{r.id}",
        title=r.title or r.counterparty,
        subtitle=r.counterparty if r.title else "",
        amount=float(r.amount),
        priority=r.priority,
        owner=r.owner,
        date=r.op_date or "",
        action=_WMS_ACTIONS.get(r.stage, ""),
        details=details,
        tags=tags,
    )


@router.get("/ops", response_model=list[WarehouseOpOut])
async def list_ops(session: AsyncSession = Depends(get_session)):
    """Складские операции (плоский список)."""
    return (
        await session.execute(select(WarehouseOp).order_by(WarehouseOp.id.desc()))
    ).scalars().all()


@router.get("/board", response_model=FunnelBoardOut)
async def board(session: AsyncSession = Depends(get_session)) -> FunnelBoardOut:
    """Воронка операций: складские операции сгруппированы по стадиям цикла."""
    rows = (await session.execute(select(WarehouseOp))).scalars().all()
    return build_board(STAGES, rows, _to_card)


@router.post("/ops", response_model=WarehouseOpOut, status_code=201)
async def create_op(payload: WarehouseOpCreate, session: AsyncSession = Depends(get_session)):
    """Создать складскую операцию. Номер генерируется автоматически, если не задан."""
    data = payload.model_dump()
    data["amount"] = Decimal(str(data["amount"]))
    obj = WarehouseOp(**data)
    session.add(obj)
    await session.flush()
    if not obj.number:
        obj.number = f"ОП-2026-{obj.id:04d}"
    await session.commit()
    await session.refresh(obj)
    return obj


@router.patch("/ops/{op_id}", response_model=WarehouseOpOut)
async def update_op(
    op_id: int, payload: StageUpdate, session: AsyncSession = Depends(get_session)
):
    """Сменить стадию складской операции."""
    obj = await session.get(WarehouseOp, op_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Операция не найдена")
    obj.stage = payload.stage
    await session.commit()
    await session.refresh(obj)
    return obj
