"""Permanent identity of the accepted production receipt and its arrival movement."""
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import ForeignKey, Numeric, event, select
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base
from core.domain.models import OutboxEvent
from modules.wms.models import StockMovement


class ProductionArrival(Base):
    __tablename__ = "production_arrival"
    __table_args__ = {"schema": "wms"}
    receipt_id: Mapped[int] = mapped_column(ForeignKey("wms.receipt.id"), primary_key=True)
    line_id: Mapped[int] = mapped_column(ForeignKey("wms.receipt_line.id"), unique=True)
    organization_id: Mapped[int] = mapped_column()
    event_id: Mapped[int] = mapped_column(ForeignKey("outbox_event.id"), unique=True)
    movement_id: Mapped[int | None] = mapped_column(ForeignKey("wms.stock_movement.id"), unique=True)
    accepted_quantity: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    rejected_quantity: Mapped[Decimal] = mapped_column(Numeric(14, 2))


def immutable(mapper, connection, target):
    raise ValueError("Production arrival binding is immutable")


event.listen(ProductionArrival, "before_update", immutable)
event.listen(ProductionArrival, "before_delete", immutable)


async def record_arrival(session, receipt, lines):
    if receipt.source_event_id is None:
        return
    source = await session.get(OutboxEvent, receipt.source_event_id)
    if source is None or source.event_type != "production.output.confirmed":
        return
    if len(lines) != 1:
        raise HTTPException(409, "Production output requires exactly one bound receipt line")
    await session.flush()
    line = lines[0]
    moves = (await session.scalars(select(StockMovement).where(
        StockMovement.organization_id == receipt.organization_id,
        StockMovement.doc_ref == receipt.number, StockMovement.reason == "receipt"))).all()
    expected_count = 1 if line.accepted_qty > 0 else 0
    if len(moves) != expected_count:
        raise HTTPException(409, "Production arrival movement set differs from accepted QC")
    session.add(ProductionArrival(receipt_id=receipt.id, line_id=line.id,
        organization_id=receipt.organization_id, event_id=source.id,
        movement_id=moves[0].id if moves else None,
        accepted_quantity=line.accepted_qty, rejected_quantity=line.rejected_qty))
    await session.flush()
