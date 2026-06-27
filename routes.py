"""HTTP-API модуля WMS. Монтируется под префиксом ``/wms``."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.domain.models import Sku
from core.runtime.core import Core
from core.runtime.deps import get_core, get_session
from core.runtime.funnel import FunnelBoardOut, FunnelCard, build_board
from core.services.auth import require_permission
from modules.wms.models import (
    InventoryCount,
    InventoryLine,
    Location,
    Receipt,
    ReceiptLine,
    StockMovement,
    StockThreshold,
    Task,
    WarehouseOp,
)
from modules.wms.schemas import (
    AdjustmentIn,
    AlertRow,
    AlertsOut,
    BalanceRow,
    BalancesOut,
    InventoryCountCreate,
    InventoryCountOut,
    InventoryDetailOut,
    InventoryLineCreate,
    InventoryLineOut,
    InventoryLineUpdate,
    InventorySummary,
    LocationCreate,
    LocationOut,
    LocationUpdate,
    MovementOpIn,
    QcDecisionIn,
    ReceiptCreate,
    ReceiptDetailOut,
    ReceiptLineOut,
    ReceiptOut,
    ReconOut,
    ReconRow,
    StageUpdate,
    StockMirror,
    StockMirrorRow,
    StockMovementCreate,
    StockMovementOut,
    TaskCreate,
    TaskOut,
    TaskUpdate,
    ThresholdCreate,
    ThresholdOut,
    TransferIn,
    WarehouseOpCreate,
    WarehouseOpOut,
)
from modules.wms.stages import STAGES

router = APIRouter(tags=["wms"])


# --- Журнал движений (приход/расход; наполняется и событиями других модулей) ---


@router.get("/movements", response_model=list[StockMovementOut])
async def list_movements(
    sku: str | None = None,
    warehouse: str | None = None,
    reason: str | None = None,
    limit: int = 200,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.read")),
):
    """Движения по складу (приход/расход), новые первыми; опц. фильтры."""
    stmt = select(StockMovement).order_by(StockMovement.id.desc())
    if sku and sku.strip():
        stmt = stmt.where(StockMovement.sku_code.ilike(f"%{sku.strip()}%"))
    if warehouse:
        stmt = stmt.where(StockMovement.warehouse == warehouse)
    if reason:
        stmt = stmt.where(StockMovement.reason == reason)
    stmt = stmt.limit(max(1, min(limit, 1000)))
    return (await session.execute(stmt)).scalars().all()


@router.post("/movements", response_model=StockMovementOut, status_code=201)
async def create_movement(
    payload: StockMovementCreate,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.count")),
):
    """Зафиксировать движение по складу (низкоуровневое; обычно — операции ниже)."""
    obj = StockMovement(
        sku_code=payload.sku_code,
        warehouse=payload.warehouse,
        kind=payload.kind,
        qty=Decimal(str(payload.qty)),
        reason=payload.reason,
        location_id=payload.location_id,
        batch_ref=payload.batch_ref,
        doc_ref=payload.doc_ref,
        note=payload.note,
    )
    session.add(obj)
    await session.commit()
    await session.refresh(obj)
    return obj


# --- Складские операции: всё пишется движениями в ОПЕРАЦИОННЫЙ журнал WMS (дубль факта;
#     1С остаётся истиной остатка — мы её не трогаем). ---


@router.post("/receipt", response_model=StockMovementOut, status_code=201)
async def receipt(
    payload: MovementOpIn,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.count")),
):
    """Приёмка: приходное движение (reason=receipt)."""
    obj = StockMovement(
        sku_code=payload.sku_code,
        warehouse=payload.warehouse,
        kind="in",
        qty=Decimal(str(payload.qty)),
        reason="receipt",
        location_id=payload.location_id,
        batch_ref=payload.batch_ref,
        doc_ref=payload.doc_ref,
        note=payload.note,
    )
    session.add(obj)
    await session.commit()
    await session.refresh(obj)
    return obj


@router.post("/shipment", response_model=StockMovementOut, status_code=201)
async def shipment(
    payload: MovementOpIn,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.count")),
):
    """Отгрузка: расходное движение (reason=shipment)."""
    obj = StockMovement(
        sku_code=payload.sku_code,
        warehouse=payload.warehouse,
        kind="out",
        qty=Decimal(str(payload.qty)),
        reason="shipment",
        location_id=payload.location_id,
        batch_ref=payload.batch_ref,
        doc_ref=payload.doc_ref,
        note=payload.note,
    )
    session.add(obj)
    await session.commit()
    await session.refresh(obj)
    return obj


@router.post("/transfer", response_model=list[StockMovementOut], status_code=201)
async def transfer(
    payload: TransferIn,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.count")),
) -> list[StockMovement]:
    """Перемещение: пара движений out@from + in@to, связанных одним doc_ref (TRF-…)."""
    qty = Decimal(str(payload.qty))
    if qty <= 0:
        raise HTTPException(status_code=400, detail="Количество должно быть больше нуля")
    if payload.from_location_id == payload.to_location_id:
        raise HTTPException(status_code=400, detail="Источник и назначение совпадают")
    out = StockMovement(
        sku_code=payload.sku_code, warehouse=payload.warehouse, kind="out", qty=qty,
        reason="transfer", location_id=payload.from_location_id,
        batch_ref=payload.batch_ref, note=payload.note,
    )
    inn = StockMovement(
        sku_code=payload.sku_code, warehouse=payload.warehouse, kind="in", qty=qty,
        reason="transfer", location_id=payload.to_location_id,
        batch_ref=payload.batch_ref, note=payload.note,
    )
    session.add_all([out, inn])
    await session.flush()
    ref = f"TRF-{out.id:05d}"
    out.doc_ref = ref
    inn.doc_ref = ref
    await session.commit()
    await session.refresh(out)
    await session.refresh(inn)
    return [out, inn]


@router.post("/adjustment", response_model=StockMovementOut, status_code=201)
async def adjustment(
    payload: AdjustmentIn,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.count")),
):
    """Ручная коррекция остатка WMS: qty знаковая (+ излишек → in, − недостача → out).

    Правит только теневой журнал WMS — остатки 1С не меняются (1С = истина, фаза 1).
    """
    qty = Decimal(str(payload.qty))
    if qty == 0:
        raise HTTPException(status_code=400, detail="Коррекция на ноль не имеет смысла")
    obj = StockMovement(
        sku_code=payload.sku_code,
        warehouse=payload.warehouse,
        kind="in" if qty > 0 else "out",
        qty=abs(qty),
        reason="adjustment",
        location_id=payload.location_id,
        batch_ref=payload.batch_ref,
        note=payload.note,
    )
    session.add(obj)
    await session.commit()
    await session.refresh(obj)
    return obj


# --- Топология склада: зоны/ячейки (адресное хранение) ---


@router.get("/locations", response_model=list[LocationOut])
async def list_locations(
    warehouse: str | None = None,
    active: bool | None = None,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.read")),
):
    """Ячейки склада (зона → код), сортировка по складу/зоне/коду."""
    stmt = select(Location).order_by(Location.warehouse, Location.zone, Location.code)
    if warehouse:
        stmt = stmt.where(Location.warehouse == warehouse)
    if active is not None:
        stmt = stmt.where(Location.is_active == active)
    return (await session.execute(stmt)).scalars().all()


@router.post("/locations", response_model=LocationOut, status_code=201)
async def create_location(
    payload: LocationCreate,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.count")),
):
    """Создать ячейку/зону хранения."""
    loc = Location(**payload.model_dump())
    session.add(loc)
    await session.commit()
    await session.refresh(loc)
    return loc


@router.patch("/locations/{loc_id}", response_model=LocationOut)
async def update_location(
    loc_id: int,
    payload: LocationUpdate,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.count")),
):
    """Переименовать/архивировать ячейку (is_active=false — скрыть из подбора)."""
    loc = await session.get(Location, loc_id)
    if loc is None:
        raise HTTPException(status_code=404, detail="Ячейка не найдена")
    if payload.title is not None:
        loc.title = payload.title
    if payload.is_active is not None:
        loc.is_active = payload.is_active
    await session.commit()
    await session.refresh(loc)
    return loc


# --- Оперативный остаток (знаковая сумма движений). СВЕРЯТЬ с 1С — не истина! ---


@router.get("/balances", response_model=BalancesOut)
async def balances(
    sku: str | None = None,
    warehouse: str | None = None,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.read")),
) -> BalancesOut:
    """Оперативный остаток WMS из движений: in − out по (SKU, склад, ячейка, партия).

    Это ТЕНЕВОЙ остаток (дубль движений), его положено сверять с 1С (истина остатка).
    Отрицательные значения = аномалия журнала (сигнал к разбору).
    """
    signed = case((StockMovement.kind == "in", StockMovement.qty), else_=-StockMovement.qty)
    stmt = (
        select(
            StockMovement.sku_code,
            StockMovement.warehouse,
            StockMovement.location_id,
            StockMovement.batch_ref,
            func.coalesce(func.sum(signed), 0),
        )
        .group_by(
            StockMovement.sku_code,
            StockMovement.warehouse,
            StockMovement.location_id,
            StockMovement.batch_ref,
        )
    )
    if sku and sku.strip():
        stmt = stmt.where(StockMovement.sku_code.ilike(f"%{sku.strip()}%"))
    if warehouse:
        stmt = stmt.where(StockMovement.warehouse == warehouse)
    rows = (await session.execute(stmt)).all()

    sku_codes = {r[0] for r in rows}
    loc_ids = {r[2] for r in rows if r[2] is not None}
    titles = (
        dict((await session.execute(select(Sku.code, Sku.title).where(Sku.code.in_(sku_codes)))).all())
        if sku_codes
        else {}
    )
    loc_codes = (
        dict(
            (await session.execute(select(Location.id, Location.code).where(Location.id.in_(loc_ids)))).all()
        )
        if loc_ids
        else {}
    )
    out_rows = [
        BalanceRow(
            sku_code=code,
            sku_title=titles.get(code, ""),
            warehouse=wh,
            location_id=loc_id,
            location_code=loc_codes.get(loc_id, "") if loc_id is not None else "",
            batch_ref=batch or "",
            qty=float(qty),
        )
        for code, wh, loc_id, batch, qty in rows
    ]
    out_rows.sort(key=lambda r: (r.sku_code, r.location_code, r.batch_ref))
    return BalancesOut(rows=out_rows, sku_count=len({r.sku_code for r in out_rows}))


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
    """Провести (заморозить) инвентаризацию: статус ``done`` + коррекция теневого журнала.

    Для каждой посчитанной строки с расхождением пишется движение ``adjustment`` в
    ОПЕРАЦИОННЫЙ журнал WMS (in — излишек, out — недостача), чтобы теневой остаток склада
    сошёлся с фактом пересчёта.
    ⚠️ Остатки 1С НЕ корректируются — это решение владельца (фаза 2). 1С остаётся истиной;
    документ лишь фиксирует расхождения как факт и правит СВОЙ журнал.
    """
    doc = await _open_count(session, count_id)
    lines = (
        await session.execute(
            select(InventoryLine).where(InventoryLine.count_id == count_id)
        )
    ).scalars().all()
    for line in lines:
        if line.counted_qty is None:
            continue
        variance = line.counted_qty - line.expected_qty
        if variance == 0:
            continue
        session.add(
            StockMovement(
                sku_code=line.sku_code,
                warehouse=doc.warehouse,
                kind="in" if variance > 0 else "out",
                qty=abs(variance),
                reason="adjustment",
                batch_ref="",
                doc_ref=doc.number,
                note="инвентаризация",
            )
        )
    doc.status = "done"
    doc.completed_at = datetime.utcnow()
    await session.commit()
    await session.refresh(doc)
    return doc


# --- Приёмка с QC-гейтом: вход товара (закупка/производство/вручную). Приходное движение
#     пишется только после accept по факту QC (D2). Событие прихода рождает pending_qc (events.py). ---


async def _sku_title(session: AsyncSession, code: str) -> str:
    sku = (await session.execute(select(Sku).where(Sku.code == code))).scalars().first()
    return sku.title if sku else ""


async def _receipt_detail(session: AsyncSession, r: Receipt) -> ReceiptDetailOut:
    lines = (
        await session.execute(
            select(ReceiptLine).where(ReceiptLine.receipt_id == r.id).order_by(ReceiptLine.id)
        )
    ).scalars().all()
    return ReceiptDetailOut(
        id=r.id, number=r.number, source=r.source, entity_ref=r.entity_ref,
        warehouse=r.warehouse, status=r.status, counterparty=r.counterparty,
        created_at=r.created_at, decided_at=r.decided_at, decided_by=r.decided_by,
        lines=[ReceiptLineOut.model_validate(line) for line in lines],
    )


@router.get("/receipts", response_model=list[ReceiptOut])
async def list_receipts(
    status: str | None = None,
    warehouse: str | None = None,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.read")),
):
    """Документы приёмки (DESC по id); опц. фильтр по статусу/складу."""
    stmt = select(Receipt).order_by(Receipt.id.desc())
    if status:
        stmt = stmt.where(Receipt.status == status)
    if warehouse:
        stmt = stmt.where(Receipt.warehouse == warehouse)
    return (await session.execute(stmt)).scalars().all()


@router.get("/receipts/{receipt_id}", response_model=ReceiptDetailOut)
async def get_receipt(
    receipt_id: int,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.read")),
) -> ReceiptDetailOut:
    """Документ приёмки со строками."""
    r = await session.get(Receipt, receipt_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Приёмка не найдена")
    return await _receipt_detail(session, r)


@router.post("/receipts", response_model=ReceiptDetailOut, status_code=201)
async def create_receipt(
    payload: ReceiptCreate,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.count")),
) -> ReceiptDetailOut:
    """Ручная приёмка: документ в pending_qc + строки. Приход — после QC accept (D2)."""
    r = Receipt(
        source=payload.source or "manual",
        entity_ref=payload.entity_ref,
        warehouse=payload.warehouse,
        counterparty=payload.counterparty,
        status="pending_qc",
    )
    session.add(r)
    await session.flush()
    r.number = f"ПРМ-2026-{r.id:04d}"
    for ln in payload.lines:
        session.add(
            ReceiptLine(
                receipt_id=r.id,
                sku_code=ln.sku_code,
                sku_title=await _sku_title(session, ln.sku_code),
                expected_qty=Decimal(str(ln.expected_qty)),
                batch_ref=ln.batch_ref,
                location_id=ln.location_id,
            )
        )
    await session.commit()
    await session.refresh(r)
    return await _receipt_detail(session, r)


@router.post("/receipts/{receipt_id}/qc", response_model=ReceiptDetailOut)
async def qc_receipt(
    receipt_id: int,
    payload: QcDecisionIn,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.count")),
) -> ReceiptDetailOut:
    """Зафиксировать решение QC по строкам (принято/брак/причина/ячейка). Без движений —
    приход пишет accept (D2). Документ остаётся pending_qc до проведения."""
    r = await session.get(Receipt, receipt_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Приёмка не найдена")
    if r.status != "pending_qc":
        raise HTTPException(status_code=409, detail="Приёмка уже обработана")
    lines = {
        line.id: line
        for line in (
            await session.execute(
                select(ReceiptLine).where(ReceiptLine.receipt_id == receipt_id)
            )
        ).scalars().all()
    }
    for d in payload.decisions:
        line = lines.get(d.line_id)
        if line is None:
            continue  # чужая/несуществующая строка — пропускаем
        line.accepted_qty = Decimal(str(d.accepted_qty))
        line.rejected_qty = Decimal(str(d.rejected_qty))
        line.reject_reason = d.reject_reason
        if d.location_id is not None:
            line.location_id = d.location_id
    if payload.decided_by:
        r.decided_by = payload.decided_by
    await session.commit()
    await session.refresh(r)
    return await _receipt_detail(session, r)


@router.post("/receipts/{receipt_id}/accept", response_model=ReceiptDetailOut)
async def accept_receipt(
    receipt_id: int,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.count")),
) -> ReceiptDetailOut:
    """Провести приёмку: по каждой строке с принятым кол-вом — приходное движение
    (reason=receipt, doc_ref=номер приёмки). Брак НЕ приходуется на свободный остаток —
    остаётся зафиксированным на строке (rejected_qty). Идемпотентно: повторный accept
    не плодит движения (по статусу).
    # ponytail: карантинную ячейку/движение reason=quarantine завести, когда появится
    # физическая зона карантина; пока брак только фиксируется на строке (не в балансе).
    """
    r = await session.get(Receipt, receipt_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Приёмка не найдена")
    if r.status != "pending_qc":
        if r.status == "accepted":
            return await _receipt_detail(session, r)  # идемпотентно
        raise HTTPException(status_code=409, detail=f"Нельзя провести приёмку в статусе {r.status}")
    lines = (
        await session.execute(
            select(ReceiptLine).where(ReceiptLine.receipt_id == receipt_id)
        )
    ).scalars().all()
    for line in lines:
        accepted = line.accepted_qty if line.accepted_qty is not None else line.expected_qty
        if accepted and accepted > 0:
            session.add(
                StockMovement(
                    sku_code=line.sku_code,
                    warehouse=r.warehouse,
                    kind="in",
                    qty=accepted,
                    reason="receipt",
                    doc_ref=r.number,
                    location_id=line.location_id,
                    batch_ref=line.batch_ref,
                )
            )
            # авто-задача размещения: из приёмной ячейки в постоянную (выбирается при завершении)
            session.add(
                Task(
                    kind="putaway",
                    sku_code=line.sku_code,
                    qty=accepted,
                    warehouse=r.warehouse,
                    from_location_id=line.location_id,
                    doc_ref=r.number,
                    status="open",
                )
            )
    r.status = "accepted"
    r.decided_at = datetime.utcnow()
    await session.commit()
    await session.refresh(r)
    return await _receipt_detail(session, r)


# --- Задачи кладовщику: размещение (put-away) и подбор (pick) ---


@router.get("/tasks", response_model=list[TaskOut])
async def list_tasks(
    kind: str | None = None,
    status: str | None = None,
    assignee: str | None = None,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.read")),
):
    """Задачи склада (DESC по id); фильтры kind/status/assignee."""
    stmt = select(Task).order_by(Task.id.desc())
    if kind:
        stmt = stmt.where(Task.kind == kind)
    if status:
        stmt = stmt.where(Task.status == status)
    if assignee:
        stmt = stmt.where(Task.assignee == assignee)
    return (await session.execute(stmt)).scalars().all()


@router.post("/tasks", response_model=TaskOut, status_code=201)
async def create_task(
    payload: TaskCreate,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.count")),
):
    """Создать задачу (put-away/pick) вручную."""
    t = Task(**payload.model_dump())
    session.add(t)
    await session.commit()
    await session.refresh(t)
    return t


@router.patch("/tasks/{task_id}", response_model=TaskOut)
async def update_task(
    task_id: int,
    payload: TaskUpdate,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.count")),
):
    """Взять в работу / завершить / отменить. Завершение пишет движение:
    put-away → transfer приёмная→постоянная (нужна to_location), pick → out reason=pick."""
    t = await session.get(Task, task_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if t.status in ("done", "canceled"):
        raise HTTPException(status_code=409, detail="Задача уже закрыта")
    if payload.assignee is not None:
        t.assignee = payload.assignee
    if payload.to_location_id is not None:
        t.to_location_id = payload.to_location_id
    if payload.note is not None:
        t.note = payload.note
    if payload.status == "in_progress":
        t.status = "in_progress"
    elif payload.status == "canceled":
        t.status = "canceled"
    elif payload.status == "done":
        if t.kind == "putaway":
            if t.to_location_id is None:
                raise HTTPException(status_code=400, detail="Укажите ячейку назначения (to_location_id)")
            ref = f"PUT-{t.id:05d}"
            session.add_all([
                StockMovement(sku_code=t.sku_code, warehouse=t.warehouse, kind="out", qty=t.qty,
                              reason="transfer", location_id=t.from_location_id, doc_ref=ref),
                StockMovement(sku_code=t.sku_code, warehouse=t.warehouse, kind="in", qty=t.qty,
                              reason="transfer", location_id=t.to_location_id, doc_ref=ref),
            ])
        elif t.kind == "pick":
            session.add(
                StockMovement(sku_code=t.sku_code, warehouse=t.warehouse, kind="out", qty=t.qty,
                              reason="pick", location_id=t.from_location_id,
                              doc_ref=t.doc_ref or f"PICK-{t.id:05d}")
            )
        t.status = "done"
        t.done_at = datetime.utcnow()
    await session.commit()
    await session.refresh(t)
    return t


@router.post("/pack", response_model=list[StockMovementOut], status_code=201)
async def pack(
    payload: MovementOpIn,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.count")),
) -> list[StockMovement]:
    """Упаковка подобранного: balance-нейтральная пара движений reason=pack (перевод в зону
    «готово к отгрузке», ``location_id`` — упаковочная ячейка). Связь с волной — по ``doc_ref``.

    Нейтральна для оперативного остатка (out из подбора + in в упаковку) — физический расход
    даёт отгрузка (/shipment reason=shipment).
    """
    qty = Decimal(str(payload.qty))
    if qty <= 0:
        raise HTTPException(status_code=400, detail="Количество должно быть больше нуля")
    ref = payload.doc_ref or ""
    out = StockMovement(
        sku_code=payload.sku_code, warehouse=payload.warehouse, kind="out", qty=qty,
        reason="pack", batch_ref=payload.batch_ref, doc_ref=ref, note=payload.note,
    )
    inn = StockMovement(
        sku_code=payload.sku_code, warehouse=payload.warehouse, kind="in", qty=qty,
        reason="pack", location_id=payload.location_id, batch_ref=payload.batch_ref,
        doc_ref=ref, note=payload.note,
    )
    session.add_all([out, inn])
    await session.flush()
    if not ref:
        out.doc_ref = inn.doc_ref = f"PACK-{out.id:05d}"
    await session.commit()
    await session.refresh(out)
    await session.refresh(inn)
    return [out, inn]


# --- Сверка теневого остатка WMS с зеркалом 1С (деньго-защита) ---


@router.get("/reconciliation", response_model=ReconOut)
async def reconciliation(
    warehouse: str | None = None,
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.read")),
) -> ReconOut:
    """Сверка: оперативный остаток WMS (in−out по складу) ПРОТИВ зеркала 1С (qty_available).

    diff = wms − onec; diff_value = diff × себес из 1С. Строки сортируются по |diff_value|
    убыв. (где деньги расходятся — сверху). 1С = истина: расхождение — СИГНАЛ к разбору,
    в 1С ничего не пишем.
    # ponytail: N+1 по шлюзу (один вызов на SKU); bulk-чтение остатков на StockGateway —
    # согласовать с СИНК, когда номенклатура вырастет.
    """
    gw = core.services.stock
    if gw is None:
        return ReconOut(rows=[], gateway=False, total_abs_diff_value=0.0)

    signed = case((StockMovement.kind == "in", StockMovement.qty), else_=-StockMovement.qty)
    stmt = select(
        StockMovement.sku_code, StockMovement.warehouse, func.coalesce(func.sum(signed), 0)
    ).group_by(StockMovement.sku_code, StockMovement.warehouse)
    if warehouse:
        stmt = stmt.where(StockMovement.warehouse == warehouse)
    wms = {(code, wh): float(q) for code, wh, q in (await session.execute(stmt)).all()}

    codes = {code for code, _ in wms}
    onec: dict[tuple[str, str], tuple[float, float | None]] = {}
    for code in codes:
        data = await gw.stock_by_sku(session, code)
        if not data:
            continue
        for r in data["rows"]:
            if warehouse and r["warehouse"] != warehouse:
                continue
            onec[(code, r["warehouse"])] = (r["qty_available"], r["cost"])

    titles = (
        dict((await session.execute(select(Sku.code, Sku.title).where(Sku.code.in_(codes)))).all())
        if codes
        else {}
    )
    rows: list[ReconRow] = []
    for key in set(wms) | set(onec):
        code, wh = key
        wq = wms.get(key, 0.0)
        oq, cost = onec.get(key, (0.0, None))
        diff = round(wq - oq, 2)
        rows.append(
            ReconRow(
                sku_code=code, title=titles.get(code, ""), warehouse=wh,
                wms_qty=wq, onec_qty=oq, diff=diff,
                diff_value=round(diff * cost, 2) if cost is not None else None,
            )
        )
    rows.sort(key=lambda r: abs(r.diff_value or 0), reverse=True)
    total = round(sum(abs(r.diff_value) for r in rows if r.diff_value is not None), 2)
    return ReconOut(rows=rows, gateway=True, total_abs_diff_value=total)


# --- Low-stock: пороги дефицита и алерты «нужно дозаказать» (деньго-защита) ---


@router.get("/thresholds", response_model=list[ThresholdOut])
async def list_thresholds(
    warehouse: str | None = None,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.read")),
):
    """Пороги дефицита (min/reorder) по SKU/складу."""
    stmt = select(StockThreshold).order_by(StockThreshold.sku_code)
    if warehouse:
        stmt = stmt.where(StockThreshold.warehouse == warehouse)
    return (await session.execute(stmt)).scalars().all()


@router.post("/thresholds", response_model=ThresholdOut, status_code=201)
async def create_threshold(
    payload: ThresholdCreate,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.count")),
):
    """Задать/добавить порог дефицита."""
    t = StockThreshold(
        sku_code=payload.sku_code, warehouse=payload.warehouse,
        min_qty=Decimal(str(payload.min_qty)), reorder_qty=Decimal(str(payload.reorder_qty)),
        active=payload.active,
    )
    session.add(t)
    await session.commit()
    await session.refresh(t)
    return t


@router.get("/alerts", response_model=AlertsOut)
async def alerts(
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.read")),
) -> AlertsOut:
    """SKU с дефицитом: свободный остаток 1С (available − reserved) ниже min_qty.

    severity: out_of_stock (≤0) / below_min. Рекомендованный дозаказ = reorder_qty.
    Источник остатка — 1С (через шлюз); WMS в 1С не пишет. Заявку в закупку отсюда НЕ
    создаём (граница модулей) — кнопка-заглушка на фронте.
    # ponytail: N+1 по шлюзу (вызов на активный порог); bulk-чтение — согласовать с СИНК.
    """
    gw = core.services.stock
    if gw is None:
        return AlertsOut(rows=[], gateway=False)
    thresholds = (
        await session.execute(select(StockThreshold).where(StockThreshold.active.is_(True)))
    ).scalars().all()
    codes = {t.sku_code for t in thresholds}
    titles = (
        dict((await session.execute(select(Sku.code, Sku.title).where(Sku.code.in_(codes)))).all())
        if codes
        else {}
    )
    rows: list[AlertRow] = []
    for t in thresholds:
        data = await gw.stock_by_sku(session, t.sku_code)
        free = 0.0
        if data:
            for r in data["rows"]:
                if r["warehouse"] == t.warehouse:
                    free += r["qty_available"] - r["qty_reserved"]
        min_qty = float(t.min_qty)
        if free >= min_qty:
            continue  # порог не нарушен
        rows.append(
            AlertRow(
                sku_code=t.sku_code, title=titles.get(t.sku_code, ""), warehouse=t.warehouse,
                free_qty=round(free, 2), min_qty=min_qty, deficit=round(min_qty - free, 2),
                reorder_qty=float(t.reorder_qty),
                severity="out_of_stock" if free <= 0 else "below_min",
            )
        )
    rows.sort(key=lambda r: r.deficit, reverse=True)
    return AlertsOut(rows=rows, gateway=True)
