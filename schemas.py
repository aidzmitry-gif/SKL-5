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
