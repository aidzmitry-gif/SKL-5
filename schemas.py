"""Pydantic-схемы модуля WMS."""
from __future__ import annotations

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
