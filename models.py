"""ORM-модели модуля WMS (схема ``wms.*``)."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, func
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


class InventoryCount(Base):
    """Документ инвентаризации (пересчёт склада): ожидаемое из 1С vs факт.

    WMS НЕ источник истины остатка (1С — истина склада, фаза 1). Документ читает
    ожидаемое через шлюз ``core.services.stock`` и фиксирует факт пересчёта + расхождение;
    в 1С НЕ пишет — корректировка остатков 1С заморожена до фазы 2 (решение владельца).
    """

    __tablename__ = "inventory_count"
    __table_args__ = {"schema": "wms"}

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(64), default="", server_default="")
    warehouse: Mapped[str] = mapped_column(
        String(128), default="Главный", server_default="Главный"
    )
    # open → идёт пересчёт; done → проведён (заморожен); canceled → отменён
    status: Mapped[str] = mapped_column(String(16), default="open", server_default="open")
    note: Mapped[str] = mapped_column(String(512), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class InventoryLine(Base):
    """Строка инвентаризации: SKU, ожидаемое (снимок 1С), факт, расхождение.

    ``expected_qty``/``unit_cost`` — снимок из 1С на момент добавления строки (документ
    точечен во времени). ``counted_qty`` пуст, пока не пересчитали. Расхождение и его
    денежная оценка считаются на чтение: ``(counted − expected)`` и ``× unit_cost``.
    """

    __tablename__ = "inventory_line"
    __table_args__ = {"schema": "wms"}

    id: Mapped[int] = mapped_column(primary_key=True)
    count_id: Mapped[int] = mapped_column(ForeignKey("wms.inventory_count.id"))
    sku_code: Mapped[str] = mapped_column(String(64))
    sku_title: Mapped[str] = mapped_column(String(255), default="", server_default="")
    unit: Mapped[str] = mapped_column(String(16), default="шт", server_default="шт")
    expected_qty: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=Decimal("0"), server_default="0"
    )
    counted_qty: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    note: Mapped[str] = mapped_column(String(512), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
