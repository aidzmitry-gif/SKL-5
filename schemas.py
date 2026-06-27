"""Pydantic-схемы модуля WMS."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class StockMovementCreate(BaseModel):
    sku_code: str
    warehouse: str = "Главный"
    kind: str = "in"  # in|out
    qty: float = 0


class StockMovementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sku_code: str
    warehouse: str
    kind: str
    qty: float


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
