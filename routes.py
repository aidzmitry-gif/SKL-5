"""HTTP-API модуля WMS. Монтируется под префиксом ``/wms``."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.domain.models import Sku
from core.runtime.core import Core
from core.runtime.deps import get_core, get_session
from core.runtime.funnel import FunnelBoardOut, FunnelCard, build_board
from core.services.auth import require_permission
from modules.wms.models import (
    CycleCountPlan,
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
from modules.wms.organization import (
    ReceiptOrganizationInput,
    authorize_row,
    qc_revision,
    visible_organization_ids,
)
from modules.wms.primary_receipts import PrimaryReceiptBinding, validate_primary_acceptance
from modules.wms.production_arrivals import record_arrival
from modules.wms.reservations import router as reservations_router
from modules.wms.schemas import (
    AdjustmentIn,
    AlertRow,
    AlertsEmitOut,
    AlertsOut,
    BalanceRow,
    BalancesOut,
    CyclePlanCreate,
    CyclePlanOut,
    CyclePlanUpdate,
    DashboardOut,
    InventoryCountCreate,
    InventoryCountOut,
    InventoryDetailOut,
    InventoryLineCreate,
    InventoryLineOut,
    InventoryLineUpdate,
    InventorySourceInput,
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
    ValuedBalancesOut,
    ValuedRow,
    WarehouseOpCreate,
    WarehouseOpOut,
)
from modules.wms.stages import STAGES

router = APIRouter(tags=["wms"])
router.include_router(reservations_router)


async def _validate_movement_locations(session: AsyncSession, warehouse: str, *location_ids: int | None) -> None:
    ids = sorted({value for value in location_ids if value is not None})
    if not ids:
        return
    locations = (await session.scalars(
        select(Location).where(Location.id.in_(ids)).order_by(Location.id)
        .with_for_update().execution_options(populate_existing=True)
    )).all()
    if len(locations) != len(ids) or any(not row.is_active or row.warehouse != warehouse for row in locations):
        raise HTTPException(422, "Ячейка должна быть активной и принадлежать указанному складу")


# --- Журнал движений (приход/расход; наполняется и событиями других модулей) ---


@router.get("/movements", response_model=list[StockMovementOut])
async def list_movements(
    sku: str | None = None,
    warehouse: str | None = None,
    reason: str | None = None,
    limit: int = 200,
    organization_id: int | None = Query(default=None, gt=0),
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.read")),
):
    """Движения по складу (приход/расход), новые первыми; опц. фильтры."""
    visible = await visible_organization_ids(session, core, _, organization_id)
    stmt = select(StockMovement).where(StockMovement.organization_id.in_(visible)).order_by(StockMovement.id.desc())
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
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.count")),
):
    """Зафиксировать движение по складу (низкоуровневое; обычно — операции ниже)."""
    await visible_organization_ids(session, core, _, payload.organization_id)
    # A supplier receipt must have the procurement-owned primary document and
    # the linked QC receipt.  A generic stock movement has neither, so it must
    # never be presented as a substitute for an incoming invoice.
    if payload.kind == "in" and payload.reason == "receipt":
        raise HTTPException(409, "Supplier receipts are created in Procurement; prepare warehouse QC from the primary receipt")
    await _validate_movement_locations(session, payload.warehouse, payload.location_id)
    from modules.wms.invoice_shipments import guard_free
    await guard_free(session, payload.organization_id, {(payload.sku_code, payload.warehouse): Decimal(str(payload.qty)) if payload.kind == "out" else Decimal("0")}, [payload.doc_ref])
    obj = StockMovement(
        organization_id=payload.organization_id,
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


# --- Складские операции. Supplier primary receipts belong to Procurement. ---


@router.post("/receipt", response_model=StockMovementOut, status_code=201)
async def receipt(
    payload: MovementOpIn,
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.count")),
):
    """Retired supplier-receipt shortcut; keep the URL fail-closed for clients."""
    await visible_organization_ids(session, core, _, payload.organization_id)
    raise HTTPException(409, "Supplier receipts are created in Procurement; prepare warehouse QC from the primary receipt")


@router.post("/shipment", response_model=StockMovementOut, status_code=201)
async def shipment(
    payload: MovementOpIn,
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.count")),
):
    """Отгрузка: расходное движение (reason=shipment).

    При наличии ``doc_ref`` публикует `wms.shipment.completed` → Офис двигает документ в
    «Отгружено». Офис матчит документ по shipment_ref/sales_ref/doc_number — кладём ``doc_ref``
    во все три ключа. Без ``doc_ref`` событие НЕ эмитим: офис не сможет сопоставить документ.
    """
    await visible_organization_ids(session, core, _, payload.organization_id)
    await _validate_movement_locations(session, payload.warehouse, payload.location_id)
    from modules.wms.invoice_shipments import guard_free
    await guard_free(session, payload.organization_id, {(payload.sku_code, payload.warehouse): Decimal(str(payload.qty))}, [payload.doc_ref])
    obj = StockMovement(
        organization_id=payload.organization_id,
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
    await session.flush()
    if payload.doc_ref:
        core.event_bus.emit(
            session,
            "wms.shipment.completed",
            {
                "organization_id": payload.organization_id,
                "shipment_ref": payload.doc_ref, "sales_ref": payload.doc_ref,
                "doc_number": payload.doc_ref, "sku_code": payload.sku_code,
                "warehouse": payload.warehouse, "qty": float(payload.qty),
                "entity_ref": f"shipment:{obj.id}",
            },
        )
    await session.commit()
    await session.refresh(obj)
    return obj


@router.post("/transfer", response_model=list[StockMovementOut], status_code=201)
async def transfer(
    payload: TransferIn,
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.count")),
) -> list[StockMovement]:
    """Перемещение: пара движений out@from + in@to, связанных одним doc_ref (TRF-…)."""
    await visible_organization_ids(session, core, _, payload.organization_id)
    await _validate_movement_locations(session, payload.warehouse, payload.from_location_id, payload.to_location_id)
    qty = Decimal(str(payload.qty))
    if qty <= 0:
        raise HTTPException(status_code=400, detail="Количество должно быть больше нуля")
    if payload.from_location_id == payload.to_location_id:
        raise HTTPException(status_code=400, detail="Источник и назначение совпадают")
    out = StockMovement(
        organization_id=payload.organization_id,
        sku_code=payload.sku_code, warehouse=payload.warehouse, kind="out", qty=qty,
        reason="transfer", location_id=payload.from_location_id,
        batch_ref=payload.batch_ref, note=payload.note,
    )
    inn = StockMovement(
        organization_id=payload.organization_id,
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
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.count")),
):
    """Ручная коррекция остатка WMS: qty знаковая (+ излишек → in, − недостача → out).

    Правит только теневой журнал WMS — остатки 1С не меняются (1С = истина, фаза 1).
    """
    await visible_organization_ids(session, core, _, payload.organization_id)
    await _validate_movement_locations(session, payload.warehouse, payload.location_id)
    qty = Decimal(str(payload.qty))
    if qty == 0:
        raise HTTPException(status_code=400, detail="Коррекция на ноль не имеет смысла")
    from modules.wms.invoice_shipments import guard_free
    await guard_free(session, payload.organization_id, {(payload.sku_code, payload.warehouse): -qty})
    obj = StockMovement(
        organization_id=payload.organization_id,
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
    organization_id: int | None = Query(default=None, gt=0),
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.read")),
) -> BalancesOut:
    """Оперативный остаток WMS из движений: in − out по (SKU, склад, ячейка, партия).

    Это ТЕНЕВОЙ остаток (дубль движений), его положено сверять с 1С (истина остатка).
    Отрицательные значения = аномалия журнала (сигнал к разбору).
    """
    visible = await visible_organization_ids(session, core, _, organization_id)
    signed = case((StockMovement.kind == "in", StockMovement.qty), else_=-StockMovement.qty)
    stmt = (
        select(
            StockMovement.sku_code,
            StockMovement.warehouse,
            StockMovement.location_id,
            StockMovement.batch_ref,
            func.coalesce(func.sum(signed), 0),
            StockMovement.organization_id,
        )
        .where(StockMovement.organization_id.in_(visible))
        .group_by(
            StockMovement.organization_id,
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
            organization_id=owner,
            sku_code=code,
            sku_title=titles.get(code, ""),
            warehouse=wh,
            location_id=loc_id,
            location_code=loc_codes.get(loc_id, "") if loc_id is not None else "",
            batch_ref=batch or "",
            qty=float(qty),
        )
        for code, wh, loc_id, batch, qty, owner in rows
    ]
    out_rows.sort(key=lambda r: (r.organization_id, r.sku_code, r.warehouse, r.location_code, r.batch_ref))
    return BalancesOut(rows=out_rows, sku_count=len({r.sku_code for r in out_rows}))


@dataclass
class _Valued:
    """Оценка остатка WMS в деньгах по (sku,warehouse): qty × себес 1С."""

    sku_code: str
    title: str
    warehouse: str
    qty: float
    unit_cost: float | None
    value: float | None


async def _valued_rows(session: AsyncSession, core: Core) -> tuple[list[_Valued], float, bool]:
    """Оперативный остаток WMS (in−out) по (sku,warehouse) × себес 1С → оценка в деньгах.

    Источник себеса — 1С (`stock_by_sku`), тот же, что reconciliation; в 1С не пишем.
    ``value=None``, если себеса нет в зеркале (честно). Возвращает (строки, total_value,
    gateway_ok); при отключённом шлюзе — ([], 0.0, False).
    # ponytail: N+1 по шлюзу (себес на пару sku/склад); bulk-чтение — согласовать с СИНК.
    """
    gw = core.services.stock
    if gw is None:
        return [], 0.0, False
    signed = case((StockMovement.kind == "in", StockMovement.qty), else_=-StockMovement.qty)
    agg = (
        await session.execute(
            select(
                StockMovement.sku_code,
                StockMovement.warehouse,
                func.coalesce(func.sum(signed), 0),
            ).group_by(StockMovement.sku_code, StockMovement.warehouse)
        )
    ).all()
    codes = {r[0] for r in agg}
    titles = (
        dict((await session.execute(select(Sku.code, Sku.title).where(Sku.code.in_(codes)))).all())
        if codes
        else {}
    )
    rows: list[_Valued] = []
    total = 0.0
    for sku_code, wh, qty_raw in agg:
        qty = float(qty_raw or 0)
        _, cost = await _snapshot_from_1c(core, session, sku_code, wh)
        unit_cost = float(cost) if cost is not None else None
        value = round(qty * unit_cost, 2) if unit_cost is not None else None
        if value is not None:
            total += value
        rows.append(
            _Valued(sku_code=sku_code, title=titles.get(sku_code, ""), warehouse=wh,
                    qty=qty, unit_cost=unit_cost, value=value)
        )
    rows.sort(key=lambda v: abs(v.value) if v.value is not None else 0.0, reverse=True)
    return rows, round(total, 2), True


@router.get("/balances/valued", response_model=ValuedBalancesOut)
async def balances_valued(
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.read")),
) -> ValuedBalancesOut:
    """Оценка оперативного остатка WMS в деньгах: in−out по (sku,warehouse) × себес 1С.

    Себестоимость — из зеркала 1С (как reconciliation), в 1С не пишем. Сорт по |value|
    убыв. Шлюз не подключён → честный пустой ответ (gateway=false, rows=[]).
    """
    rows, total, gateway = await _valued_rows(session, core)
    return ValuedBalancesOut(
        rows=[
            ValuedRow(sku_code=v.sku_code, title=v.title, warehouse=v.warehouse, qty=v.qty,
                      unit_cost=v.unit_cost, value=v.value)
            for v in rows
        ],
        total_value=total,
        gateway=gateway,
    )


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
async def list_ops(
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.read")),
):
    """Складские операции (плоский список)."""
    return (
        await session.execute(select(WarehouseOp).order_by(WarehouseOp.id.desc()))
    ).scalars().all()


@router.get("/board", response_model=FunnelBoardOut)
async def board(
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.read")),
) -> FunnelBoardOut:
    """Воронка операций: складские операции сгруппированы по стадиям цикла."""
    rows = (await session.execute(select(WarehouseOp))).scalars().all()
    return build_board(STAGES, rows, _to_card)


@router.post("/ops", response_model=WarehouseOpOut, status_code=201)
async def create_op(
    payload: WarehouseOpCreate,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.count")),
):
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
    op_id: int,
    payload: StageUpdate,
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.count")),
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
    def valued_total(rows):
        if not outs or any(row.unit_cost is None for row in outs) or any(row.variance_value is None for row in rows):
            return None
        return round(sum(row.variance_value for row in rows), 2)

    shortage_value = valued_total(shortages)
    surplus_value = valued_total(surpluses)
    return InventorySummary(
        lines=len(outs),
        counted=len(counted),
        shortages=len(shortages),
        surpluses=len(surpluses),
        shortage_value=shortage_value,
        surplus_value=surplus_value,
        net_value=(round(shortage_value + surplus_value, 2)
                   if shortage_value is not None and surplus_value is not None else None),
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
        expected_source=doc.expected_source,
        source_evidence=doc.source_evidence,
        journal_confirmed_by=doc.journal_confirmed_by,
        journal_confirmed_at=doc.journal_confirmed_at,
        snapshot_version=doc.snapshot_version,
        snapshot_cutoff=doc.snapshot_cutoff,
        snapshot_at=doc.snapshot_at,
        organization_id=doc.organization_id,
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


def _inventory_access_gateway(core, *, listing=False):
    gateway = getattr(core.services, "accounting", None)
    methods = ["source_member"] + (["source_organizations"] if listing else [])
    if gateway is None or any(not callable(getattr(gateway, name, None)) for name in methods):
        raise HTTPException(503, "Сервис доступа к юрлицам недоступен")
    return gateway


async def _inventory_visible(session, core, user, organization_id=None):
    _inventory_access_gateway(core, listing=organization_id is None)
    return await visible_organization_ids(session, core, user, organization_id)


async def _owned_inventory_row(session, core, user, model, row_id):
    row = await session.get(model, row_id)
    if row is None:
        raise HTTPException(404, "Складской документ не найден")
    _inventory_access_gateway(core)
    return await authorize_row(session, core, user, row)


async def _open_count(session: AsyncSession, count_id: int, core: Core, user) -> InventoryCount:
    doc = await _owned_inventory_row(session, core, user, InventoryCount, count_id)
    if doc.status != "open":
        raise HTTPException(409, "Инвентаризация уже проведена или отменена")
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


# Only physical movements participate. Legacy reservation mirrors are never stock.
_INVENTORY_PHYSICAL_REASONS = {"", "receipt", "shipment", "transfer", "adjustment", "pick", "pack", "production_issue"}


async def _inventory_confirmation(session, core, user, organization_id, payload):
    if not payload.journal_complete:
        raise HTTPException(422, "Подтвердите полноту физического журнала и основание пересчёта")
    actor = await _inventory_access_gateway(core).source_member(session, organization_id, user)
    return {"expected_source": payload.expected_source, "source_evidence": payload.source_evidence,
            "journal_confirmed_by": actor, "journal_confirmed_at": datetime.utcnow()}


def _confirmed_inventory_source(doc):
    if (doc.expected_source != "wms_physical" or not doc.source_evidence
            or not doc.journal_confirmed_by or doc.journal_confirmed_at is None):
        raise HTTPException(409, "Источник и полнота журнала не подтверждены. Создайте новый пересчёт")


async def _physical_inventory_snapshot(session, organization_id, warehouse):
    # Caller holds the same organization lock as manual movements, acceptance and task writers.
    rows = (await session.scalars(select(StockMovement).where(
        StockMovement.organization_id == organization_id, StockMovement.warehouse == warehouse,
        StockMovement.reason.not_in(["reserve", "release"]),
    ).order_by(StockMovement.id).execution_options(populate_existing=True))).all()
    if not rows:
        raise HTTPException(409, "Ожидаемый остаток неизвестен: нет подтверждённых физических движений")
    quantities = {}
    identity = []
    for row in rows:
        if (row.reason not in _INVENTORY_PHYSICAL_REASONS or row.kind not in {"in", "out"}
                or not row.sku_code or not row.qty.is_finite() or row.qty <= 0):
            raise HTTPException(409, "Журнал содержит неразобранное движение. Требуется сверка")
        quantities[row.sku_code] = quantities.get(row.sku_code, Decimal("0")) + (row.qty if row.kind == "in" else -row.qty)
        identity.append([row.id, row.sku_code, row.kind, format(row.qty, ".2f"), row.reason])
    if any(qty < 0 or qty >= Decimal("1000000000000") for qty in quantities.values()):
        raise HTTPException(409, "Физический остаток требует сверки перед пересчётом")
    serialized = json.dumps([organization_id, warehouse, identity], ensure_ascii=False, separators=(",", ":"))
    return quantities, hashlib.sha256(serialized.encode()).hexdigest(), rows[-1].id


def _check_inventory_version(doc, version, cutoff):
    if doc.snapshot_version != version or doc.snapshot_cutoff != cutoff:
        raise HTTPException(409, "Физический журнал изменился. Создайте новый пересчёт")


async def _inventory_snapshot(session, doc):
    _confirmed_inventory_source(doc)
    try:
        quantities, version, cutoff = await _physical_inventory_snapshot(session, doc.organization_id, doc.warehouse)
    except HTTPException as exc:
        if exc.status_code == 409 and doc.snapshot_version is not None:
            raise HTTPException(409, "Физический журнал изменился. Создайте новый пересчёт") from exc
        raise
    if doc.snapshot_version is not None:
        _check_inventory_version(doc, version, cutoff)
    return quantities, version, cutoff


def _record_inventory_snapshot(doc, version, cutoff):
    if doc.snapshot_version is None:
        doc.snapshot_version = version
        doc.snapshot_cutoff = cutoff
        doc.snapshot_at = datetime.utcnow()


async def _fill_inventory_from_physical(session, doc):
    quantities, version, cutoff = await _inventory_snapshot(session, doc)
    existing = set((await session.scalars(select(InventoryLine.sku_code).where(
        InventoryLine.count_id == doc.id))).all())
    skus = {row.code: row for row in (await session.scalars(select(Sku).where(Sku.code.in_(quantities)))).all()}
    for code, qty in sorted(quantities.items()):
        if code in existing:
            continue
        sku = skus.get(code)
        session.add(InventoryLine(count_id=doc.id, sku_code=code,
            sku_title=sku.title if sku else "", unit=sku.unit if sku else "шт",
            expected_qty=qty, unit_cost=None))
    _record_inventory_snapshot(doc, version, cutoff)


@router.get("/inventory", response_model=list[InventoryCountOut])
async def list_inventory(
    status: str | None = None,
    organization_id: int | None = Query(default=None, gt=0),
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.read")),
):
    """Список документов инвентаризации (DESC по id), опц. фильтр по статусу."""
    visible = await _inventory_visible(session, core, _, organization_id)
    stmt = select(InventoryCount).where(InventoryCount.organization_id.in_(visible)).order_by(InventoryCount.id.desc())
    if status:
        stmt = stmt.where(InventoryCount.status == status)
    return (await session.execute(stmt)).scalars().all()


@router.post("/inventory", response_model=InventoryCountOut, status_code=201)
async def create_inventory(
    payload: InventoryCountCreate,
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.count")),
):
    """Создать документ инвентаризации. Номер генерируется (`ИНВ-2026-{id:04d}`)."""
    confirmation = await _inventory_confirmation(session, core, _, payload.organization_id, payload)
    doc = InventoryCount(organization_id=payload.organization_id, warehouse=payload.warehouse,
                         note=payload.note, **confirmation)
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
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.read")),
) -> InventoryDetailOut:
    """Документ инвентаризации со строками, расхождениями и денежной сводкой."""
    doc = await _owned_inventory_row(session, core, _, InventoryCount, count_id)
    return await _inventory_detail(session, doc)


@router.post("/inventory/{count_id}/populate", response_model=InventoryDetailOut)
async def populate_inventory(
    count_id: int,
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.count")),
) -> InventoryDetailOut:
    """Зафиксировать подтверждённый физический журнал юрлица; повтор не обновляет снимок."""
    doc = await _open_count(session, count_id, core, _)
    await _fill_inventory_from_physical(session, doc)
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
    """Добавить SKU с известным физическим остатком выбранного юрлица; неизвестное не ноль."""
    doc = await _open_count(session, count_id, core, _)
    sku = (
        await session.execute(select(Sku).where(Sku.code == payload.sku_code))
    ).scalars().first()
    quantities, version, cutoff = await _inventory_snapshot(session, doc)
    if payload.sku_code not in quantities:
        raise HTTPException(409, "Ожидаемый остаток SKU неизвестен. Требуется подтверждённый источник")
    if await session.scalar(select(InventoryLine.id).where(
            InventoryLine.count_id == count_id, InventoryLine.sku_code == payload.sku_code)):
        raise HTTPException(409, "SKU уже включён в пересчёт")
    expected, cost = quantities[payload.sku_code], None
    _record_inventory_snapshot(doc, version, cutoff)
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
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.count")),
) -> InventoryLineOut:
    """Внести факт пересчёта / заметку по строке. Документ должен быть открыт."""
    line = await session.get(InventoryLine, line_id)
    if line is None:
        raise HTTPException(status_code=404, detail="Строка инвентаризации не найдена")
    await _open_count(session, line.count_id, core, _)  # 409, если документ уже проведён
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
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.count")),
):
    """Зафиксировать пересчёт и корректировки физического журнала юрлица атомарно.

    Требуется актуальный подтверждённый снимок. Бухгалтерская оценка расхождений
    и проводки оформляются отдельным документом на подтверждённых основаниях.
    """
    doc = await _owned_inventory_row(session, core, _, InventoryCount, count_id)
    if doc.status == "done":
        return doc
    if doc.status != "open":
        raise HTTPException(409, "Инвентаризация отменена")
    lines = (
        await session.execute(
            select(InventoryLine).where(InventoryLine.count_id == count_id)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    if not lines or any(line.counted_qty is None for line in lines):
        raise HTTPException(409, "Пересчёт всех строк должен быть завершён")
    quantities, version, cutoff = await _inventory_snapshot(session, doc)
    if doc.snapshot_version is None:
        raise HTTPException(409, "Снимок исходного журнала не зафиксирован. Создайте новый пересчёт")
    if len({line.sku_code for line in lines}) != len(lines) or any(
            line.sku_code not in quantities or line.expected_qty != quantities[line.sku_code]
            for line in lines):
        raise HTTPException(409, "Строки не соответствуют снимку журнала. Создайте новый пересчёт")
    # Confirmed counts record physical fact even when existing reservations exceed it.
    # Fulfillment and discretionary stock issues still enforce available stock.
    # Conditional claim protects repeated deliveries, including SQLite where FOR UPDATE is ignored.
    claimed = await session.scalar(update(InventoryCount).where(
        InventoryCount.id == count_id, InventoryCount.organization_id == doc.organization_id,
        InventoryCount.status == "open",
    ).values(status="done", completed_at=datetime.utcnow()).returning(InventoryCount.id)
      .execution_options(synchronize_session=False))
    if claimed is None:
        await session.refresh(doc)
        if doc.status == "done":
            return doc
        raise HTTPException(409, "Состояние инвентаризации изменилось")
    for line in lines:
        if line.counted_qty is None:
            continue
        variance = line.counted_qty - line.expected_qty
        if variance == 0:
            continue
        session.add(
            StockMovement(
                organization_id=doc.organization_id,
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
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise
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
            select(ReceiptLine).where(ReceiptLine.receipt_id == r.id).order_by(ReceiptLine.id).execution_options(populate_existing=True)
        )
    ).scalars().all()
    return ReceiptDetailOut(
        qc_revision=qc_revision(r, lines),
        organization_id=r.organization_id,
        id=r.id, number=r.number, source=r.source, entity_ref=r.entity_ref,
        warehouse=r.warehouse, status=r.status, counterparty=r.counterparty,
        created_at=r.created_at, decided_at=r.decided_at, decided_by=r.decided_by,
        lines=[ReceiptLineOut.model_validate(line) for line in lines],
    )


@router.get("/receipts", response_model=list[ReceiptOut])
async def list_receipts(
    status: str | None = None,
    warehouse: str | None = None,
    organization_id: int | None = None,
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.read")),
):
    """Приёмки доступных юрлиц; несопоставленные документы не входят в рабочий список."""
    organization_ids = await visible_organization_ids(session, core, _, organization_id)
    stmt = select(Receipt).where(Receipt.organization_id.in_(organization_ids)).order_by(Receipt.id.desc())
    if status:
        stmt = stmt.where(Receipt.status == status)
    if warehouse:
        stmt = stmt.where(Receipt.warehouse == warehouse)
    return (await session.execute(stmt)).scalars().all()


@router.get("/receipt-organizations")
async def receipt_organizations(
    session: AsyncSession = Depends(get_session), core: Core = Depends(get_core),
    user: object = Depends(require_permission("wms.read")),
):
    gateway = getattr(core.services, "accounting", None)
    if gateway is None:
        raise HTTPException(503, "Сервис доступа к юрлицам недоступен")
    return await gateway.source_organizations(session, user)


@router.get("/receipts-unassigned")
async def unassigned_receipts(
    organization_id: int = Query(gt=0),
    after_id: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
    user: object = Depends(require_permission("wms.read")),
):
    """Кандидаты для явного сопоставления главбухом; чтение не назначает владельца."""
    gateway = getattr(core.services, "accounting", None)
    if gateway is None:
        raise HTTPException(503, "Сервис доступа к юрлицам недоступен")
    await gateway.source_owner_authority(session, organization_id, user)
    rows = (await session.scalars(select(Receipt).where(
        Receipt.organization_id.is_(None), Receipt.status == "pending_qc", Receipt.id > after_id,
    ).order_by(Receipt.id).limit(51))).all()
    items = rows[:50]
    return {"items": [ReceiptOut.model_validate(row) for row in items],
            "next_after_id": items[-1].id if len(rows) > 50 else None}


@router.get("/receipts-unassigned/{receipt_id}", response_model=ReceiptDetailOut)
async def unassigned_receipt_detail(
    receipt_id: int,
    organization_id: int = Query(gt=0),
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
    user: object = Depends(require_permission("wms.read")),
):
    gateway = getattr(core.services, "accounting", None)
    if gateway is None:
        raise HTTPException(503, "Сервис доступа к юрлицам недоступен")
    await gateway.source_owner_authority(session, organization_id, user)
    receipt = await session.scalar(select(Receipt).where(
        Receipt.id == receipt_id, Receipt.organization_id.is_(None), Receipt.status == "pending_qc",
    ).with_for_update().execution_options(populate_existing=True))
    if receipt is None:
        raise HTTPException(404, "Приёмка не найдена в очереди сопоставления")
    return await _receipt_detail(session, receipt)


@router.get("/receipts/{receipt_id}", response_model=ReceiptDetailOut)
async def get_receipt(
    receipt_id: int,
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.read")),
) -> ReceiptDetailOut:
    """Документ приёмки со строками."""
    r = await session.get(Receipt, receipt_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Приёмка не найдена")
    r = await authorize_row(session, core, _, r)
    return await _receipt_detail(session, r)


@router.post("/receipts", response_model=ReceiptDetailOut, status_code=201)
async def create_receipt(
    payload: ReceiptCreate,
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.count")),
) -> ReceiptDetailOut:
    """Retired manual supplier-receipt creation; WMS accepts linked physical documents only."""
    await visible_organization_ids(session, core, _, payload.organization_id)
    raise HTTPException(409, "Create the supplier primary receipt in Procurement, then prepare its warehouse QC receipt")


@router.post("/receipts/{receipt_id}/organization", response_model=ReceiptDetailOut)
async def assign_receipt_organization(
    receipt_id: int, payload: ReceiptOrganizationInput, core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    user: object = Depends(require_permission("wms.count")),
):
    gateway = getattr(core.services, "accounting", None)
    if gateway is None:
        raise HTTPException(503, "Сервис доступа к юрлицам недоступен")
    actor = await gateway.source_owner_authority(session, payload.organization_id, user)
    receipt = await session.scalar(select(Receipt).where(Receipt.id == receipt_id).with_for_update())
    if receipt is None:
        raise HTTPException(404, "Приёмка не найдена")
    if receipt.organization_id is not None:
        if receipt.organization_id != payload.organization_id:
            raise HTTPException(409, "Юрлицо уже подтверждено; требуется отдельное исправление")
        return await _receipt_detail(session, receipt)
    if receipt.status != "pending_qc":
        raise HTTPException(409, "Сопоставление принятой приёмки требует сверки существующих движений")
    receipt.organization_id = payload.organization_id
    core.event_bus.emit(session, "wms.receipt.organization_assigned", {
        "receipt_id": receipt.id, "organization_id": payload.organization_id,
        "actor": actor, "evidence": payload.evidence,
    })
    await session.commit()
    await session.refresh(receipt)
    return await _receipt_detail(session, receipt)


@router.post("/receipts/{receipt_id}/qc", response_model=ReceiptDetailOut)
async def qc_receipt(
    receipt_id: int,
    payload: QcDecisionIn,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.count")),
) -> ReceiptDetailOut:
    """Зафиксировать решение QC по строкам (принято/брак/причина/ячейка). Без движений —
    приход пишет accept (D2). Документ остаётся pending_qc до проведения."""
    r = await session.get(Receipt, receipt_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Приёмка не найдена")
    r = await authorize_row(session, core, _, r)
    if r.status != "pending_qc":
        raise HTTPException(status_code=409, detail="Приёмка уже обработана")
    lines = {
        line.id: line
        for line in (
            await session.execute(
                select(ReceiptLine).where(ReceiptLine.receipt_id == receipt_id).execution_options(populate_existing=True)
            )
        ).scalars().all()
    }
    if payload.expected_revision != qc_revision(r, lines.values()):
        raise HTTPException(409, "Результаты контроля изменились. Обновите документ и проверьте ввод заново")
    decision_ids = [d.line_id for d in payload.decisions]
    if len(decision_ids) != len(set(decision_ids)) or any(line_id not in lines for line_id in decision_ids):
        raise HTTPException(422, "Строки контроля качества должны принадлежать приёмке и не повторяться")
    await _validate_movement_locations(session, r.warehouse, *(d.location_id for d in payload.decisions))
    for d in payload.decisions:
        line = lines.get(d.line_id)
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
    core: Core = Depends(get_core),
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
    r = await authorize_row(session, core, _, r)
    if r.status != "pending_qc":
        if r.status == "accepted":
            return await _receipt_detail(session, r)  # идемпотентно
        raise HTTPException(status_code=409, detail=f"Нельзя провести приёмку в статусе {r.status}")
    lines = (
        await session.execute(
            select(ReceiptLine).where(ReceiptLine.receipt_id == receipt_id).execution_options(populate_existing=True)
        )
    ).scalars().all()
    await validate_primary_acceptance(session, core, r, lines)
    await _validate_movement_locations(session, r.warehouse, *(
        line.location_id for line in lines
        if (line.accepted_qty if line.accepted_qty is not None else line.expected_qty) > 0
    ))
    for line in lines:
        accepted = line.accepted_qty if line.accepted_qty is not None else line.expected_qty
        if accepted and accepted > 0:
            session.add(
                StockMovement(
                    organization_id=r.organization_id,
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
                    organization_id=r.organization_id,
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
    await record_arrival(session, r, lines)
    binding = await session.get(PrimaryReceiptBinding, r.id)
    if binding is not None and getattr(core, "event_bus", None) is not None:
        binding_lines = {item["receipt_line_id"]: item for item in binding.line_bindings}
        accepted_lines = []
        for line in lines:
            source = binding_lines.get(line.id)
            if source is None:
                raise HTTPException(409, "Связь первичной и физической приёмки неполна")
            accepted = line.accepted_qty if line.accepted_qty is not None else line.expected_qty
            accepted_lines.append({
                "position": source["position"],
                "source_line": source["source_line"],
                "sku_code": line.sku_code,
                "accepted_qty": format(accepted, ".2f"),
            })
        core.event_bus.emit(session, "wms.receipt.accepted", {
            "schema_version": 1,
            "organization_id": binding.organization_id,
            "receipt_id": r.id,
            "source_receipt_id": binding.source_receipt_id,
            "source_version": binding.source_version,
            "warehouse": r.warehouse,
            "lines": sorted(accepted_lines, key=lambda item: item["position"]),
            "evidence": f"QC accepted {r.number}",
            "by": r.decided_by or "wms",
            "entity_ref": f"procurement:receipt:{binding.source_receipt_id}",
        })
    await session.commit()
    await session.refresh(r)
    return await _receipt_detail(session, r)


# --- Задачи кладовщику: размещение (put-away) и подбор (pick) ---


@router.get("/tasks", response_model=list[TaskOut])
async def list_tasks(
    kind: str | None = None,
    status: str | None = None,
    assignee: str | None = None,
    organization_id: int | None = None,
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.read")),
):
    """Задачи склада (DESC по id); фильтры kind/status/assignee."""
    organization_ids = await visible_organization_ids(session, core, _, organization_id)
    stmt = select(Task).where(Task.organization_id.in_(organization_ids)).order_by(Task.id.desc())
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
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.count")),
):
    """Создать задачу (put-away/pick) вручную."""
    await visible_organization_ids(session, core, _, payload.organization_id)
    await _validate_movement_locations(session, payload.warehouse, payload.from_location_id, payload.to_location_id)
    t = Task(**payload.model_dump())
    session.add(t)
    await session.commit()
    await session.refresh(t)
    return t


@router.patch("/tasks/{task_id}", response_model=TaskOut)
async def update_task(
    task_id: int,
    payload: TaskUpdate,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    _: object = Depends(require_permission("wms.count")),
):
    """Взять в работу / завершить / отменить. Завершение пишет движение:
    put-away → transfer приёмная→постоянная (нужна to_location), pick → out reason=pick."""
    t = await session.get(Task, task_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    t = await authorize_row(session, core, _, t)
    from modules.wms.invoice_shipments import guard_free
    from modules.wms.reservation_events import ReservationPick
    if t.kind == "pick" and await session.get(ReservationPick, t.id) is not None:
        if payload.status not in (None, "in_progress") or payload.to_location_id is not None:
            raise HTTPException(409, "Invoice-owned picks allow assignment, instructions and in_progress only")
    if t.status in ("done", "canceled"):
        raise HTTPException(status_code=409, detail="Задача уже закрыта")
    if payload.to_location_id is not None or payload.status == "done":
        destination = payload.to_location_id if payload.to_location_id is not None else t.to_location_id
        await _validate_movement_locations(session, t.warehouse, t.from_location_id, destination)
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
                StockMovement(organization_id=t.organization_id, sku_code=t.sku_code, warehouse=t.warehouse, kind="out", qty=t.qty,
                              reason="transfer", location_id=t.from_location_id, doc_ref=ref),
                StockMovement(organization_id=t.organization_id, sku_code=t.sku_code, warehouse=t.warehouse, kind="in", qty=t.qty,
                              reason="transfer", location_id=t.to_location_id, doc_ref=ref),
            ])
        elif t.kind == "pick":
            await guard_free(session, t.organization_id, {(t.sku_code, t.warehouse): t.qty}, [t.doc_ref])
            session.add(
                StockMovement(organization_id=t.organization_id, sku_code=t.sku_code, warehouse=t.warehouse, kind="out", qty=t.qty,
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
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.count")),
) -> list[StockMovement]:
    """Упаковка подобранного: balance-нейтральная пара движений reason=pack (перевод в зону
    «готово к отгрузке», ``location_id`` — упаковочная ячейка). Связь с волной — по ``doc_ref``.

    Нейтральна для оперативного остатка (out из подбора + in в упаковку) — физический расход
    даёт отгрузка (/shipment reason=shipment).
    """
    await visible_organization_ids(session, core, _, payload.organization_id)
    await _validate_movement_locations(session, payload.warehouse, payload.location_id)
    qty = Decimal(str(payload.qty))
    if qty <= 0:
        raise HTTPException(status_code=400, detail="Количество должно быть больше нуля")
    ref = payload.doc_ref or ""
    out = StockMovement(
        organization_id=payload.organization_id,
        sku_code=payload.sku_code, warehouse=payload.warehouse, kind="out", qty=qty,
        reason="pack", batch_ref=payload.batch_ref, doc_ref=ref, note=payload.note,
    )
    inn = StockMovement(
        organization_id=payload.organization_id,
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


@dataclass
class _Deficit:
    """Внутренняя строка дефицита: данные для /alerts, эмита `wms.stock.low` и денег дашборда."""

    threshold_id: int
    sku_code: str
    sku_title: str
    warehouse: str
    free_qty: float
    min_qty: float
    deficit: float
    reorder_qty: float
    severity: str  # out_of_stock | below_min
    unit_cost: float | None  # себес из 1С (для дефицита в деньгах); None — нет в зеркале


async def _deficit_rows(session: AsyncSession, gw) -> list[_Deficit]:
    """Активные пороги, где свободный остаток 1С (available−reserved, клампим ≥0) ниже min.

    Дедуп по (sku,warehouse): на пару берём ПЕРВЫЙ порог (сорт. sku_code, id), прочие
    пропускаем — иначе дублирующие пороги плодят дубли сигнала дозаказа (WMS-R3-5).
    ``reorder_qty`` клампится в ≥0 (защита от мусора в данных). ``unit_cost`` — себес из той
    же строки 1С, что и свободный остаток (для оценки дефицита в деньгах). Сорт по дефициту.
    # ponytail: N+1 по шлюзу (вызов на активный порог); bulk-чтение — согласовать с СИНК.
    """
    thresholds = (
        await session.execute(
            select(StockThreshold)
            .where(StockThreshold.active.is_(True))
            .order_by(StockThreshold.sku_code, StockThreshold.id)
        )
    ).scalars().all()
    by_pair: dict[tuple[str, str], StockThreshold] = {}
    for t in thresholds:  # первый по (sku_code, id) выигрывает пару
        by_pair.setdefault((t.sku_code, t.warehouse), t)
    codes = {t.sku_code for t in by_pair.values()}
    titles = (
        dict((await session.execute(select(Sku.code, Sku.title).where(Sku.code.in_(codes)))).all())
        if codes
        else {}
    )
    rows: list[_Deficit] = []
    for t in by_pair.values():
        data = await gw.stock_by_sku(session, t.sku_code)
        free = 0.0
        cost: float | None = None
        if data:
            for r in data["rows"]:
                if r["warehouse"] == t.warehouse:
                    # клампим по строке (≥0) как в /wms/stock: oversell в 1С (reserved>available)
                    # не должен раздувать дефицит отрицательным свободным остатком
                    free += max(r["qty_available"] - r["qty_reserved"], 0.0)
                    if r["cost"] is not None:
                        cost = float(r["cost"])
        min_qty = float(t.min_qty)
        if free >= min_qty:
            continue  # порог не нарушен
        rows.append(
            _Deficit(
                threshold_id=t.id, sku_code=t.sku_code, sku_title=titles.get(t.sku_code, ""),
                warehouse=t.warehouse, free_qty=round(free, 2), min_qty=min_qty,
                deficit=round(min_qty - free, 2), reorder_qty=max(float(t.reorder_qty or 0), 0.0),
                severity="out_of_stock" if free <= 0 else "below_min", unit_cost=cost,
            )
        )
    rows.sort(key=lambda d: d.deficit, reverse=True)
    return rows


@router.get("/alerts", response_model=AlertsOut)
async def alerts(
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.read")),
) -> AlertsOut:
    """SKU с дефицитом: свободный остаток 1С (available − reserved) ниже min_qty.

    severity: out_of_stock (≤0) / below_min. Рекомендованный дозаказ = reorder_qty.
    Источник остатка — 1С (через шлюз); WMS в 1С не пишет. Сигнал дозаказа публикуется
    отдельной ручкой ``POST /alerts/emit`` (событие `wms.stock.low` → Закупки).
    """
    gw = core.services.stock
    if gw is None:
        return AlertsOut(rows=[], gateway=False)
    rows = [
        AlertRow(
            sku_code=d.sku_code, title=d.sku_title, warehouse=d.warehouse, free_qty=d.free_qty,
            min_qty=d.min_qty, deficit=d.deficit, reorder_qty=d.reorder_qty, severity=d.severity,
        )
        for d in await _deficit_rows(session, gw)
    ]
    return AlertsOut(rows=rows, gateway=True)


@router.post("/alerts/emit", response_model=AlertsEmitOut)
async def emit_alerts(
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.count")),
) -> AlertsEmitOut:
    """Опубликовать сигнал дозаказа `wms.stock.low` по каждому нарушенному порогу.

    Одно плоское событие на (sku,warehouse) → Закупки подписаны и заводят черновик заявки.
    Источник остатка — 1С через шлюз; шлюз не подключён → 503 (не эмитим пустоту). Дедуп —
    в пределах вызова по (sku,warehouse) (см. ``_deficit_rows``). Кросс-вызовную защиту от
    повторных кликов НЕ строим — это ответственность подписчика-закупок (дедуп черновика).
    """
    gw = core.services.stock
    if gw is None:
        raise HTTPException(status_code=503, detail="Шлюз остатков (1С/integrations) не подключён")
    deficits = await _deficit_rows(session, gw)
    for d in deficits:
        core.event_bus.emit(
            session,
            "wms.stock.low",
            {
                "sku_code": d.sku_code, "sku_title": d.sku_title, "warehouse": d.warehouse,
                "free_qty": d.free_qty, "min_qty": d.min_qty, "deficit": d.deficit,
                "reorder_qty": d.reorder_qty, "severity": d.severity,
                "source": "wms.threshold", "entity_ref": f"threshold:{d.threshold_id}",
            },
        )
    await session.commit()
    return AlertsEmitOut(emitted=len(deficits), gateway=True)


# --- Цикл-каунт: расписание периодического пересчёта (дисциплина инвентаризации) ---


@router.get("/cycle-plans", response_model=list[CyclePlanOut])
async def list_cycle_plans(
    organization_id: int | None = Query(default=None, gt=0),
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.read")),
):
    """Планы циклического пересчёта (по дате срока, раньше — выше)."""
    visible = await _inventory_visible(session, core, _, organization_id)
    return (
        await session.execute(select(CycleCountPlan).where(CycleCountPlan.organization_id.in_(visible)).order_by(CycleCountPlan.next_due_date))
    ).scalars().all()


@router.post("/cycle-plans", response_model=CyclePlanOut, status_code=201)
async def create_cycle_plan(
    payload: CyclePlanCreate,
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.count")),
):
    """Создать план цикл-каунта."""
    confirmation = await _inventory_confirmation(session, core, _, payload.organization_id, payload)
    data = payload.model_dump(exclude={"expected_source", "source_evidence", "journal_complete"})
    plan = CycleCountPlan(**data, **confirmation)
    session.add(plan)
    await session.commit()
    await session.refresh(plan)
    return plan


@router.patch("/cycle-plans/{plan_id}", response_model=CyclePlanOut)
async def update_cycle_plan(
    plan_id: int,
    payload: CyclePlanUpdate,
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.count")),
):
    """Изменить план (периодичность/дата/зона/активность)."""
    plan = await _owned_inventory_row(session, core, _, CycleCountPlan, plan_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(plan, field, value)
    await session.commit()
    await session.refresh(plan)
    return plan


@router.post("/cycle-plans/{plan_id}/run", response_model=InventoryDetailOut)
async def run_cycle_plan(
    plan_id: int,
    payload: InventorySourceInput,
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.count")),
) -> InventoryDetailOut:
    """Новый пересчёт физического журнала с повторным подтверждением его полноты пользователем."""
    plan = await _owned_inventory_row(session, core, _, CycleCountPlan, plan_id)
    _confirmed_inventory_source(plan)
    confirmation = await _inventory_confirmation(session, core, _, plan.organization_id, payload)
    note = f"Цикл-каунт {plan.warehouse}" + (f" / зона {plan.zone}" if plan.zone else "")
    doc = InventoryCount(organization_id=plan.organization_id, warehouse=plan.warehouse, note=note, status="open", **confirmation)
    session.add(doc)
    await session.flush()
    doc.number = f"ИНВ-2026-{doc.id:04d}"
    try:
        await _fill_inventory_from_physical(session, doc)
        plan.last_run_at = datetime.utcnow()
        plan.next_due_date = date.today() + timedelta(days=plan.cadence_days)
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    await session.refresh(doc)
    return await _inventory_detail(session, doc)


# --- Дашборд склада: живая сводка для главного экрана ---


@router.get("/dashboard", response_model=DashboardOut)
async def dashboard(
    session: AsyncSession = Depends(get_session),
    core: Core = Depends(get_core),
    _: object = Depends(require_permission("wms.read")),
) -> DashboardOut:
    """Сводка одним набором запросов: очередь QC, открытые задачи/инвентаризации, low-stock,
    последняя сверка с 1С (макс/сумма расхождения в деньгах), движения за сегодня."""
    async def _count(model, *conds) -> int:
        stmt = select(func.count()).select_from(model)
        for c in conds:
            stmt = stmt.where(c)
        return int((await session.execute(stmt)).scalar() or 0)

    pending_qc = await _count(Receipt, Receipt.status == "pending_qc")
    putaway_open = await _count(Task, Task.kind == "putaway", Task.status.in_(("open", "in_progress")))
    pick_open = await _count(Task, Task.kind == "pick", Task.status.in_(("open", "in_progress")))
    inv_open = await _count(InventoryCount, InventoryCount.status == "open")

    # 1С-зависимые метрики — переиспользуем готовую логику (gateway внутри)
    gw = core.services.stock
    deficits = await _deficit_rows(session, gw) if gw is not None else []
    alerts_deficit_value = round(
        sum(d.deficit * d.unit_cost for d in deficits if d.unit_cost is not None), 2
    )
    _, inventory_value, _ = await _valued_rows(session, core)
    rec = await reconciliation(warehouse=None, session=session, core=core, _=None)
    recon_max = max((abs(r.diff_value) for r in rec.rows if r.diff_value is not None), default=0.0)

    # движения за сегодня (приход/расход)
    today = date.today()
    start = datetime(today.year, today.month, today.day)
    in_today = (
        await session.execute(
            select(func.coalesce(func.sum(StockMovement.qty), 0)).where(
                StockMovement.kind == "in", StockMovement.created_at >= start
            )
        )
    ).scalar()
    out_today = (
        await session.execute(
            select(func.coalesce(func.sum(StockMovement.qty), 0)).where(
                StockMovement.kind == "out", StockMovement.created_at >= start
            )
        )
    ).scalar()

    return DashboardOut(
        receipts_pending_qc=pending_qc,
        tasks_putaway_open=putaway_open,
        tasks_pick_open=pick_open,
        alerts_count=len(deficits),
        alerts_deficit_value=alerts_deficit_value,
        inventory_value=inventory_value,
        inventories_open=inv_open,
        recon_max_diff_value=round(recon_max, 2),
        recon_total_diff_value=rec.total_abs_diff_value,
        movements_today_in=float(in_today or 0),
        movements_today_out=float(out_today or 0),
        gateway=core.services.stock is not None,
    )
