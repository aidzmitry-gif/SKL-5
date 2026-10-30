"""Pydantic-схемы модуля WMS."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StockMovementCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    organization_id: int = Field(gt=0, strict=True)
    sku_code: str = Field(min_length=1, max_length=64)
    warehouse: str = Field(default="Главный", min_length=1, max_length=128)
    kind: Literal["in", "out"] = "in"  # in|out
    qty: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    reason: str = ""
    location_id: int | None = None
    batch_ref: str = ""
    doc_ref: str = ""
    note: str = ""


class StockMovementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    organization_id: int | None = None
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

    model_config = ConfigDict(str_strip_whitespace=True)
    organization_id: int = Field(gt=0, strict=True)
    sku_code: str = Field(min_length=1, max_length=64)
    qty: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    warehouse: str = Field(default="Главный", min_length=1, max_length=128)
    location_id: int | None = None
    batch_ref: str = ""
    doc_ref: str = ""
    note: str = ""


class TransferIn(BaseModel):
    """Перемещение: пара движений out@from + in@to, связаны doc_ref."""

    model_config = ConfigDict(str_strip_whitespace=True)
    organization_id: int = Field(gt=0, strict=True)
    sku_code: str = Field(min_length=1, max_length=64)
    qty: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    warehouse: str = Field(default="Главный", min_length=1, max_length=128)
    from_location_id: int | None = None
    to_location_id: int | None = None
    batch_ref: str = ""
    note: str = ""


class AdjustmentIn(BaseModel):
    """Ручная коррекция: qty знаковая (+ излишек → in, − недостача → out)."""

    model_config = ConfigDict(str_strip_whitespace=True)
    organization_id: int = Field(gt=0, strict=True)
    sku_code: str = Field(min_length=1, max_length=64)
    qty: Decimal = Field(max_digits=14, decimal_places=2)
    warehouse: str = Field(default="Главный", min_length=1, max_length=128)
    location_id: int | None = None
    batch_ref: str = ""
    note: str = ""


# --- Топология склада (зоны/ячейки) ---


class LocationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    organization_id: int = Field(gt=0, strict=True)
    warehouse: str = Field(min_length=1, max_length=128)
    zone: str = Field(default="", max_length=64)
    code: str = Field(min_length=1, max_length=64)
    title: str = Field(default="", max_length=255)


class LocationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    organization_id: int = Field(gt=0, strict=True)
    title: str | None = None
    is_active: bool | None = None


class LocationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    organization_id: int
    warehouse: str
    zone: str
    code: str
    title: str
    is_active: bool


# --- Оперативный остаток (из движений; СВЕРЯТЬ с 1С, не истина) ---


class BalanceRow(BaseModel):
    organization_id: int
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


class ValuedRow(BaseModel):
    """Оперативный остаток (in−out) по (SKU, склад) × себестоимость 1С → деньги."""

    sku_code: str
    title: str
    warehouse: str
    qty: float  # знаковая сумма движений: in − out
    unit_cost: float | None  # себес единицы из 1С (None — нет в зеркале)
    value: float | None  # qty × unit_cost (None без себеса)


class ValuedBalancesOut(BaseModel):
    rows: list[ValuedRow]  # сорт. по |value| убыв.
    total_value: float  # суммарная оценка остатка в деньгах
    gateway: bool  # шлюз 1С подключён; False → себес недоступен, rows=[]


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


class InventorySourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_source: Literal["wms_physical"]
    source_evidence: str = Field(min_length=1, max_length=1000)
    journal_complete: bool = Field(strict=True)


class InventoryCountCreate(InventorySourceInput):
    organization_id: int = Field(gt=0, strict=True)
    warehouse: str = "Главный"
    note: str = ""


class InventoryCountOut(BaseModel):
    snapshot_version: str | None
    snapshot_cutoff: int | None
    snapshot_at: datetime | None
    expected_source: str | None
    source_evidence: str | None
    journal_confirmed_by: str | None
    journal_confirmed_at: datetime | None
    organization_id: int | None
    model_config = ConfigDict(from_attributes=True)

    id: int
    number: str
    warehouse: str
    status: str  # open | done | canceled
    note: str
    created_at: datetime | None = None
    completed_at: datetime | None = None


class InventoryLineCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku_code: str
    counted_qty: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)  # можно сразу внести факт


class InventoryLineUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    counted_qty: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    note: str | None = None


class InventoryLineOut(BaseModel):
    id: int
    sku_code: str
    sku_title: str
    unit: str
    expected_qty: float  # снимок подтверждённого физического журнала юрлица
    counted_qty: float | None  # факт пересчёта (None — ещё не считали)
    unit_cost: float | None  # неизвестна без подтверждённого источника оценки
    variance: float | None  # counted − expected (None пока не считали)
    variance_value: float | None  # variance × unit_cost (деньги; None без себеса/факта)
    note: str


class InventorySummary(BaseModel):
    lines: int  # всего строк
    counted: int  # из них с внесённым фактом
    shortages: int  # строк с недостачей (variance < 0)
    surpluses: int  # строк с излишком (variance > 0)
    shortage_value: float | None  # None, если оценка хотя бы одной недостачи неизвестна
    surplus_value: float | None  # None, если оценка хотя бы одного излишка неизвестна
    net_value: float | None  # неизвестная оценка не подменяется нулём


class InventoryDetailOut(InventoryCountOut):
    lines: list[InventoryLineOut]
    summary: InventorySummary


# --- Приёмка с QC-гейтом ---


class ReceiptLineIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    sku_code: str = Field(min_length=1, max_length=64)
    expected_qty: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    batch_ref: str = ""
    location_id: int | None = None


class ReceiptCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    organization_id: int = Field(gt=0, strict=True)
    warehouse: str = Field(default="Главный", min_length=1, max_length=128)
    counterparty: str = ""
    source: str = "manual"
    entity_ref: str = ""
    lines: list[ReceiptLineIn] = Field(min_length=1)


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

    organization_id: int | None = None
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
    qc_revision: str


class QcLineDecision(BaseModel):
    line_id: int
    accepted_qty: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    rejected_qty: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=2)
    reject_reason: str = ""
    location_id: int | None = None


class QcDecisionIn(BaseModel):
    expected_revision: str = Field(min_length=64, max_length=64)
    decisions: list[QcLineDecision] = []
    decided_by: str = ""


# --- Задачи (put-away / pick) ---


class TaskCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    organization_id: int = Field(gt=0, strict=True)
    kind: Literal["putaway", "pick"]
    sku_code: str = Field(min_length=1, max_length=64)
    qty: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    warehouse: str = Field(default="Главный", min_length=1, max_length=128)
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

    organization_id: int | None = None
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


class AlertsEmitOut(BaseModel):
    """Результат публикации сигналов дозаказа `wms.stock.low` (одно событие на нарушенный порог)."""

    emitted: int  # сколько событий опубликовано (= нарушенных порогов после дедупа)
    gateway: bool  # шлюз 1С подключён (иначе роут отвечает 503, не эмитит)


# --- Цикл-каунт (расписание периодического пересчёта) ---


class CyclePlanCreate(InventorySourceInput):
    organization_id: int = Field(gt=0, strict=True)
    warehouse: str = "Главный"
    zone: str | None = None
    cadence_days: int = 30
    next_due_date: date | None = None
    active: bool = True
    abc_class: str | None = None


class CyclePlanUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cadence_days: int | None = None
    next_due_date: date | None = None
    active: bool | None = None
    zone: str | None = None
    abc_class: str | None = None


class CyclePlanOut(BaseModel):
    expected_source: str | None
    source_evidence: str | None
    journal_confirmed_by: str | None
    journal_confirmed_at: datetime | None
    organization_id: int | None
    model_config = ConfigDict(from_attributes=True)

    id: int
    warehouse: str
    zone: str | None
    cadence_days: int
    next_due_date: date | None
    last_run_at: datetime | None
    active: bool
    abc_class: str | None


# --- Дашборд склада ---


class DashboardOut(BaseModel):
    receipts_pending_qc: int  # приёмки в очереди QC
    tasks_putaway_open: int  # открытые задачи размещения
    tasks_pick_open: int  # открытые задачи подбора
    alerts_count: int  # SKU ниже порога (low-stock)
    alerts_deficit_value: float  # Σ дефицит×себес (1С) по нарушенным порогам; 0 без шлюза
    inventory_value: float  # оценка остатка WMS в деньгах (in−out × себес 1С); 0 без шлюза
    inventories_open: int  # открытые инвентаризации
    recon_max_diff_value: float  # макс |расхождение в деньгах| (сверка с 1С)
    recon_total_diff_value: float  # суммарное расхождение в деньгах
    movements_today_in: float  # приход за сегодня
    movements_today_out: float  # расход за сегодня
    gateway: bool  # 1С-зависимые метрики (alerts/recon) валидны
