"""ORM-модели модуля WMS (схема ``wms.*``)."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base


class StockMovement(Base):
    """Движение по складу: приход/расход SKU на складе."""

    __tablename__ = "stock_movement"
    __table_args__ = {"schema": "wms"}

    id: Mapped[int] = mapped_column(primary_key=True)
    sku_code: Mapped[str] = mapped_column(String(64))
    warehouse: Mapped[str] = mapped_column(String(128), default="Главный", server_default="Главный")
    kind: Mapped[str] = mapped_column(String(8), default="in", server_default="in")  # in|out
    qty: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"), server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
