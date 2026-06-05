"""ORM-модели модуля WMS (схема ``wms.*``)."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base


class StockMovement(Base):
    """Движение по складу: приход/расход SKU на складе (журнал операций)."""

    __tablename__ = "stock_movement"
    __table_args__ = {"schema": "wms"}

    id: Mapped[int] = mapped_column(primary_key=True)
    sku_code: Mapped[str] = mapped_column(String(64))
    warehouse: Mapped[str] = mapped_column(String(128), default="Главный", server_default="Главный")
    kind: Mapped[str] = mapped_column(String(8), default="in", server_default="in")  # in|out
    qty: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"), server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class WarehouseOp(Base):
    """Складская операция в воронке: от ожидания поступления до отгрузки.

    Стадия (`stage`) ведёт операцию по логистическому циклу (см. ``stages.py``):
    приёмка → контроль → размещение → подготовка → отгрузка.
    """

    __tablename__ = "warehouse_op"
    __table_args__ = {"schema": "wms"}

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(64), default="", server_default="")
    counterparty: Mapped[str] = mapped_column(String(255), default="", server_default="")
    title: Mapped[str] = mapped_column(String(255), default="", server_default="")
    items_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"), server_default="0")
    zone: Mapped[str] = mapped_column(String(64), default="", server_default="")
    priority: Mapped[str] = mapped_column(String(32), default="Средний", server_default="Средний")
    owner: Mapped[str] = mapped_column(String(128), default="", server_default="")
    stage: Mapped[str] = mapped_column(String(32), default="inbound", server_default="inbound")
    op_date: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
