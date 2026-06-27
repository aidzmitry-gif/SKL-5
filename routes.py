"""HTTP-API модуля WMS. Монтируется под префиксом ``/wms``."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.domain.models import Sku
from core.runtime.core import Core
from core.runtime.deps import get_core, get_session
from core.runtime.funnel import FunnelBoardOut, FunnelCard, build_board
from core.services.auth import require_permission
from modules.wms.models import InventoryCount, InventoryLine, StockMovement, WarehouseOp
from modules.wms.schemas import (
    InventoryCountCreate,
    InventoryCountOut,
    InventoryDetailOut,
    InventoryLineCreate,
    InventoryLineOut,
    InventoryLineUpdate,
    InventorySummary,
    StageUpdate,
    StockMirror,
    StockMirrorRow,
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


# --- Остатки по складам: зеркало 1С (read-only через шлюз core.services.stock) ---


@router.get("/stock", response_model=StockMirror)
async def stock_mirror(
    q: str | None = None,
    warehouse: str | None = None,
    limit: int = 200,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.read")),
) -> StockMirror:
    """Остатки/резервы по складам — зеркало 1С (read-only).

    Истина остатка — 1С/integrations; склад только отображает (не пишет, источник
    истины не трогаем). Перебираем SKU из shared-kernel постранично и читаем остаток
    по каждому через фасад ``core.services.stock`` — bulk-метода у шлюза нет.
    # ponytail: N+1 по шлюзу (≤ limit вызовов); bulk-чтение остатков на StockGateway —
    # когда номенклатура вырастет, согласовать с СИНК (владелец integrations).
    """
    gw = core.services.stock
    if gw is None:  # integrations выключен → источник остатков не подключён (не маскируем нулём)
        return StockMirror(
            rows=[], warehouses=[], total_available=0.0, total_reserved=0.0,
            total_free=0.0, sku_count=0, gateway=False, truncated=False,
        )

    limit = max(1, min(limit, 1000))
    stmt = select(Sku).order_by(Sku.code)
    if q and q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Sku.code.ilike(like), Sku.title.ilike(like)))
    # +1 сверх limit, чтобы понять, есть ли ещё SKU за страницей (truncated).
    skus = (await session.execute(stmt.offset(offset).limit(limit + 1))).scalars().all()
    truncated = len(skus) > limit
    skus = skus[:limit]

    rows: list[StockMirrorRow] = []
    sku_count = 0
    for sku in skus:
        data = await gw.stock_by_sku(session, sku.code)
        if not data:
            continue  # нет остатка по коду — в зеркале склада не показываем
        contributed = False
        for r in data["rows"]:
            if warehouse and r["warehouse"] != warehouse:
                continue
            avail = r["qty_available"]
            res = r["qty_reserved"]
            rows.append(
                StockMirrorRow(
                    sku_code=sku.code,
                    title=sku.title,
                    unit=sku.unit,
                    warehouse=r["warehouse"],
                    qty_available=avail,
                    qty_reserved=res,
                    qty_free=max(avail - res, 0.0),
                    qty_forecast=r["qty_forecast"],
                    updated_at=data.get("updated_at"),
                )
            )
            contributed = True
        if contributed:
            sku_count += 1

    return StockMirror(
        rows=rows,
        warehouses=sorted({r.warehouse for r in rows}),
        total_available=round(sum(r.qty_available for r in rows), 2),
        total_reserved=round(sum(r.qty_reserved for r in rows), 2),
        total_free=round(sum(r.qty_free for r in rows), 2),
        sku_count=sku_count,
        gateway=True,
        truncated=truncated,
    )


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


# --- Инвентаризация: пересчёт склада со сверкой против 1С (через шлюз) ---
#
# Документ инвентаризации — РЕАЛЬНЫЙ складской учёт в рамках контракта остатков:
# ожидаемое берётся из 1С (истина, фаза 1) через шлюз ``core.services.stock``, факт
# вносит кладовщик, расхождение (недостача/излишек) считается и оценивается в деньгах.
# ⚠️ В 1С НЕ пишем — корректировка остатков 1С заморожена до фазы 2 (решение владельца).


def _line_out(line: InventoryLine) -> InventoryLineOut:
    """Строка инвентаризации с расхождением: variance = факт − ожидаемое, в деньгах × себес."""
    expected = float(line.expected_qty)
    counted = float(line.counted_qty) if line.counted_qty is not None else None
    cost = float(line.unit_cost) if line.unit_cost is not None else None
    variance = round(counted - expected, 2) if counted is not None else None
    variance_value = (
        round(variance * cost, 2) if (variance is not None and cost is not None) else None
    )
    return InventoryLineOut(
        id=line.id,
        sku_code=line.sku_code,
        sku_title=line.sku_title,
        unit=line.unit,
        expected_qty=expected,
        counted_qty=counted,
        unit_cost=cost,
        variance=variance,
        variance_value=variance_value,
        note=line.note,
    )


def _summary(outs: list[InventoryLineOut]) -> InventorySummary:
    """Сводка по документу: сколько посчитано, недостачи/излишки и их денежная оценка."""
    counted = [o for o in outs if o.counted_qty is not None]
    shortages = [o for o in counted if o.variance is not None and o.variance < 0]
    surpluses = [o for o in counted if o.variance is not None and o.variance > 0]
    shortage_value = round(
        sum(o.variance_value for o in shortages if o.variance_value is not None), 2
    )
    surplus_value = round(
        sum(o.variance_value for o in surpluses if o.variance_value is not None), 2
    )
    return InventorySummary(
        lines=len(outs),
        counted=len(counted),
        shortages=len(shortages),
        surpluses=len(surpluses),
        shortage_value=shortage_value,
        surplus_value=surplus_value,
        net_value=round(shortage_value + surplus_value, 2),
    )


async def _inventory_detail(session: AsyncSession, doc: InventoryCount) -> InventoryDetailOut:
    lines = (
        await session.execute(
            select(InventoryLine)
            .where(InventoryLine.count_id == doc.id)
            .order_by(InventoryLine.id)
        )
    ).scalars().all()
    outs = [_line_out(line) for line in lines]
    return InventoryDetailOut(
        id=doc.id,
        number=doc.number,
        warehouse=doc.warehouse,
        status=doc.status,
        note=doc.note,
        created_at=doc.created_at,
        completed_at=doc.completed_at,
        lines=outs,
        summary=_summary(outs),
    )


async def _open_count(session: AsyncSession, count_id: int) -> InventoryCount:
    """Документ инвентаризации, проверенный на существование и статус ``open``."""
    doc = await session.get(InventoryCount, count_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Инвентаризация не найдена")
    if doc.status != "open":
        raise HTTPException(status_code=409, detail="Инвентаризация уже проведена или отменена")
    return doc


async def _snapshot_from_1c(
    core: Core, session: AsyncSession, sku_code: str, warehouse: str
) -> tuple[Decimal, Decimal | None]:
    """Снимок (ожидаемое, себес) из 1С по складу через шлюз; (0, None) если данных нет."""
    gw = core.services.stock
    if gw is None:
        return Decimal("0"), None
    data = await gw.stock_by_sku(session, sku_code)
    if not data:
        return Decimal("0"), None
    row = next((r for r in data["rows"] if r["warehouse"] == warehouse), None)
    if row is None:
        return Decimal("0"), None
    cost = Decimal(str(row["cost"])) if row["cost"] is not None else None
    return Decimal(str(row["qty_available"])), cost


@router.get("/inventory", response_model=list[InventoryCountOut])
async def list_inventory(
    status: str | None = None,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.read")),
):
    """Список документов инвентаризации (DESC по id), опц. фильтр по статусу."""
    stmt = select(InventoryCount).order_by(InventoryCount.id.desc())
    if status:
        stmt = stmt.where(InventoryCount.status == status)
    return (await session.execute(stmt)).scalars().all()


@router.post("/inventory", response_model=InventoryCountOut, status_code=201)
async def create_inventory(
    payload: InventoryCountCreate,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.count")),
):
    """Создать документ инвентаризации. Номер генерируется (`ИНВ-2026-{id:04d}`)."""
    doc = InventoryCount(warehouse=payload.warehouse, note=payload.note)
    session.add(doc)
    await session.flush()
    if not doc.number:
        doc.number = f"ИНВ-2026-{doc.id:04d}"
    await session.commit()
    await session.refresh(doc)
    return doc


@router.get("/inventory/{count_id}", response_model=InventoryDetailOut)
async def get_inventory(
    count_id: int,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.read")),
) -> InventoryDetailOut:
    """Документ инвентаризации со строками, расхождениями и денежной сводкой."""
    doc = await session.get(InventoryCount, count_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Инвентаризация не найдена")
    return await _inventory_detail(session, doc)


@router.post("/inventory/{count_id}/populate", response_model=InventoryDetailOut)
async def populate_inventory(
    count_id: int,
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.count")),
) -> InventoryDetailOut:
    """Заполнить документ ожидаемыми остатками склада из 1С (по всем SKU с остатком).

    Снимок ожидаемого/себеса фиксируется в строках; факт вносит кладовщик далее. Уже
    добавленные SKU пропускаются (повторный вызов не плодит дубли).
    # ponytail: N+1 по шлюзу; bulk-чтение остатков на StockGateway — когда номенклатура
    # вырастет, согласовать с СИНК (владелец integrations).
    """
    doc = await _open_count(session, count_id)
    if core.services.stock is None:
        raise HTTPException(status_code=503, detail="Шлюз остатков (1С/integrations) не подключён")
    existing = set(
        (
            await session.execute(
                select(InventoryLine.sku_code).where(InventoryLine.count_id == count_id)
            )
        ).scalars().all()
    )
    skus = (await session.execute(select(Sku).order_by(Sku.code))).scalars().all()
    for sku in skus:
        if sku.code in existing:
            continue
        expected, cost = await _snapshot_from_1c(core, session, sku.code, doc.warehouse)
        if expected == 0 and cost is None:
            continue  # по этому складу остатка нет — в документ не тянем
        session.add(
            InventoryLine(
                count_id=count_id,
                sku_code=sku.code,
                sku_title=sku.title,
                unit=sku.unit,
                expected_qty=expected,
                unit_cost=cost,
            )
        )
    await session.commit()
    return await _inventory_detail(session, doc)


@router.post("/inventory/{count_id}/lines", response_model=InventoryLineOut, status_code=201)
async def add_inventory_line(
    count_id: int,
    payload: InventoryLineCreate,
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.count")),
) -> InventoryLineOut:
    """Добавить строку вручную (напр. найден товар, которого нет в 1С → ожидаемое 0)."""
    doc = await _open_count(session, count_id)
    sku = (
        await session.execute(select(Sku).where(Sku.code == payload.sku_code))
    ).scalars().first()
    expected, cost = await _snapshot_from_1c(core, session, payload.sku_code, doc.warehouse)
    line = InventoryLine(
        count_id=count_id,
        sku_code=payload.sku_code,
        sku_title=sku.title if sku else "",
        unit=sku.unit if sku else "шт",
        expected_qty=expected,
        counted_qty=Decimal(str(payload.counted_qty)) if payload.counted_qty is not None else None,
        unit_cost=cost,
    )
    session.add(line)
    await session.commit()
    await session.refresh(line)
    return _line_out(line)


@router.patch("/inventory/lines/{line_id}", response_model=InventoryLineOut)
async def update_inventory_line(
    line_id: int,
    payload: InventoryLineUpdate,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.count")),
) -> InventoryLineOut:
    """Внести факт пересчёта / заметку по строке. Документ должен быть открыт."""
    line = await session.get(InventoryLine, line_id)
    if line is None:
        raise HTTPException(status_code=404, detail="Строка инвентаризации не найдена")
    await _open_count(session, line.count_id)  # 409, если документ уже проведён
    if payload.counted_qty is not None:
        line.counted_qty = Decimal(str(payload.counted_qty))
    if payload.note is not None:
        line.note = payload.note
    await session.commit()
    await session.refresh(line)
    return _line_out(line)


@router.post("/inventory/{count_id}/complete", response_model=InventoryCountOut)
async def complete_inventory(
    count_id: int,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.count")),
):
    """Провести (заморозить) инвентаризацию: статус ``done`` + время.

    ⚠️ Остатки 1С НЕ корректируются — это решение владельца (фаза 2). Документ лишь
    фиксирует расхождения как факт для разбора (недостача = сигнал безопасности/денег).
    """
    doc = await _open_count(session, count_id)
    doc.status = "done"
    doc.completed_at = datetime.utcnow()
    await session.commit()
    await session.refresh(doc)
    return doc
