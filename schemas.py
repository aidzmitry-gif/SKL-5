"""Pydantic-схемы модуля WMS."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class StockMovementCreate(BaseModel):
    sku_code: str
    warehouse: str = "Главный"
    kind: str = "in"  # in|out
    qty: float = 0
    reason: str = ""
    location_id: int | None = None
    batch_ref: str = ""
    doc_ref: str = ""
    note: str = ""


class StockMovementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sku_code: str
    warehouse: str
    kind: str
    qty: float
    reason: str
    location_id: int | None
    batch_ref: str
    doc_ref: str
    note: str
    created_at: datetime | None = None


# --- Складские операции (приёмка/отгрузка/перемещение/коррекция) ---


class MovementOpIn(BaseModel):
    """Приёмка/отгрузка: kind и reason задаёт роут, qty — положительная величина."""

    sku_code: str
    qty: float
    warehouse: str = "Главный"
    location_id: int | None = None
    batch_ref: str = ""
    doc_ref: str = ""
    note: str = ""


class TransferIn(BaseModel):
    """Перемещение: пара движений out@from + in@to, связаны doc_ref."""

    sku_code: str
    qty: float
    warehouse: str = "Главный"
    from_location_id: int | None = None
    to_location_id: int | None = None
    batch_ref: str = ""
    note: str = ""


class AdjustmentIn(BaseModel):
    """Ручная коррекция: qty знаковая (+ излишек → in, − недостача → out)."""

    sku_code: str
    qty: float
    warehouse: str = "Главный"
    location_id: int | None = None
    batch_ref: str = ""
    note: str = ""


# --- Топология склада (зоны/ячейки) ---


class LocationCreate(BaseModel):
    warehouse: str = "Главный"
    zone: str = ""
    code: str
    title: str = ""


class LocationUpdate(BaseModel):
    title: str | None = None
    is_active: bool | None = None


class LocationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    warehouse: str
    zone: str
    code: str
    title: str
    is_active: bool


# --- Оперативный остаток (из движений; СВЕРЯТЬ с 1С, не истина) ---


class BalanceRow(BaseModel):
    sku_code: str
    sku_title: str
    warehouse: str
    location_id: int | None
    location_code: str  # "" — без ячейки
    batch_ref: str
    qty: float  # знаковая сумма движений: in − out


class BalancesOut(BaseModel):
    rows: list[BalanceRow]
    sku_count: int


class WarehouseOpCreate(BaseModel):
    counterparty: str = ""
    title: str = ""
    items_count: int = 0
    amount: float = 0
    zone: str = ""
    priority: str = "Средний"
    owner: str = ""
    stage: str = "inbound"
    number: str = ""
    op_date: str | None = None


class WarehouseOpOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    number: str
    counterparty: str
    title: str
    items_count: int
    amount: float
    zone: str
    priority: str
    owner: str
    stage: str
    op_date: str | None = None


class StageUpdate(BaseModel):
    stage: str


class StockMirrorRow(BaseModel):
    """Строка остатка по (SKU, склад) — зеркало 1С (read-only)."""

    sku_code: str
    title: str
    unit: str
    warehouse: str
    qty_available: float  # всего на складе
    qty_reserved: float  # из них зарезервировано
    qty_free: float  # свободно к продаже = available − reserved (≥0)
    qty_forecast: float  # ожидается (в пути / прогноз прихода)
    updated_at: str | None = None


class StockMirror(BaseModel):
    """Сводка остатков по складам — экран «Остатки (зеркало 1С)»."""

    rows: list[StockMirrorRow]
    warehouses: list[str]  # склады, встретившиеся в выборке (для фильтра)
    total_available: float
    total_reserved: float
    total_free: float
    sku_count: int  # SKU с остатком в выборке
    gateway: bool  # шлюз остатков (integrations) доступен; False → источник не подключён
    truncated: bool  # упёрлись в limit — есть ещё SKU за страницей


# --- Инвентаризация (пересчёт склада, сверка с 1С) ---


class InventoryCountCreate(BaseModel):
    warehouse: str = "Главный"
    note: str = ""


class InventoryCountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    number: str
    warehouse: str
    status: str  # open | done | canceled
    note: str
    created_at: datetime | None = None
    completed_at: datetime | None = None


class InventoryLineCreate(BaseModel):
    sku_code: str
    counted_qty: float | None = None  # можно сразу внести факт


class InventoryLineUpdate(BaseModel):
    counted_qty: float | None = None
    note: str | None = None


class InventoryLineOut(BaseModel):
    id: int
    sku_code: str
    sku_title: str
    unit: str
    expected_qty: float  # снимок ожидаемого из 1С
    counted_qty: float | None  # факт пересчёта (None — ещё не считали)
    unit_cost: float | None  # себес единицы из 1С (для денежной оценки расхождения)
    variance: float | None  # counted − expected (None пока не считали)
    variance_value: float | None  # variance × unit_cost (деньги; None без себеса/факта)
    note: str


class InventorySummary(BaseModel):
    lines: int  # всего строк
    counted: int  # из них с внесённым фактом
    shortages: int  # строк с недостачей (variance < 0)
    surpluses: int  # строк с излишком (variance > 0)
    shortage_value: float  # суммарная стоимость недостач (≤0)
    surplus_value: float  # суммарная стоимость излишков (≥0)
    net_value: float  # итог по деньгам (недостача + излишек)


class InventoryDetailOut(InventoryCountOut):
    lines: list[InventoryLineOut]
    summary: InventorySummary


# --- Приёмка с QC-гейтом ---


class ReceiptLineIn(BaseModel):
    sku_code: str
    expected_qty: float = 0
    batch_ref: str = ""
    location_id: int | None = None


class ReceiptCreate(BaseModel):
    warehouse: str = "Главный"
    counterparty: str = ""
    source: str = "manual"
    entity_ref: str = ""
    lines: list[ReceiptLineIn] = []


class ReceiptLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sku_code: str
    sku_title: str
    expected_qty: float
    accepted_qty: float | None
    rejected_qty: float | None
    reject_reason: str
    location_id: int | None
    batch_ref: str


class ReceiptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    number: str
    source: str
    entity_ref: str
    warehouse: str
    status: str  # pending_qc | accepted | rejected | putaway_done
    counterparty: str
    created_at: datetime | None = None
    decided_at: datetime | None = None
    decided_by: str


class ReceiptDetailOut(ReceiptOut):
    lines: list[ReceiptLineOut]


class QcLineDecision(BaseModel):
    line_id: int
    accepted_qty: float = 0
    rejected_qty: float = 0
    reject_reason: str = ""
    location_id: int | None = None


class QcDecisionIn(BaseModel):
    decisions: list[QcLineDecision] = []
    decided_by: str = ""


# --- Задачи (put-away / pick) ---


class TaskCreate(BaseModel):
    kind: str  # putaway | pick
    sku_code: str
    qty: float = 0
    warehouse: str = "Главный"
    from_location_id: int | None = None
    to_location_id: int | None = None
    doc_ref: str = ""
    assignee: str = ""
    priority: str = "normal"
    note: str = ""


class TaskUpdate(BaseModel):
    status: str | None = None  # in_progress | done | canceled
    assignee: str | None = None
    to_location_id: int | None = None
    note: str | None = None


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    status: str
    sku_code: str
    qty: float
    warehouse: str
    from_location_id: int | None
    to_location_id: int | None
    doc_ref: str
    assignee: str
    priority: str
    note: str
    created_at: datetime | None = None
    done_at: datetime | None = None


# --- Сверка теневого остатка WMS с 1С (деньго-защита) ---


class ReconRow(BaseModel):
    sku_code: str
    title: str
    warehouse: str
    wms_qty: float  # оперативный остаток WMS (из движений)
    onec_qty: float  # зеркало 1С (qty_available)
    diff: float  # wms − onec
    diff_value: float | None  # diff × себес из 1С (деньги); None без себеса


class ReconOut(BaseModel):
    rows: list[ReconRow]  # сорт. по |diff_value| убыв. (где деньги расходятся — сверху)
    gateway: bool  # шлюз 1С подключён; False → источник не доступен
    total_abs_diff_value: float  # суммарное расхождение в деньгах (по модулю)


# --- Low-stock пороги и алерты ---


class ThresholdCreate(BaseModel):
    sku_code: str
    warehouse: str = "Главный"
    min_qty: float = 0
    reorder_qty: float = 0
    active: bool = True


class ThresholdOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sku_code: str
    warehouse: str
    min_qty: float
    reorder_qty: float
    active: bool


class AlertRow(BaseModel):
    sku_code: str
    title: str
    warehouse: str
    free_qty: float  # свободный остаток 1С (available − reserved)
    min_qty: float
    deficit: float  # min − free (>0)
    reorder_qty: float  # рекомендованный дозаказ
    severity: str  # out_of_stock | below_min


class AlertsOut(BaseModel):
    rows: list[AlertRow]  # сорт. по дефициту убыв.
    gateway: bool
