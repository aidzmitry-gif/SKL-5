"""Durable invoice-event ordering and private pick bindings, no physical movement."""
from decimal import Decimal

from sqlalchemy import JSON, ForeignKey, String, select
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base
from modules.wms.models import Task


class ReservationEventState(Base):
    __tablename__ = "reservation_event_state"
    __table_args__ = {"schema": "wms"}
    document_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    state: Mapped[str] = mapped_column(String(16))
    quantities: Mapped[dict] = mapped_column(JSON)
    reserved_items: Mapped[list | None] = mapped_column(JSON, nullable=True)


class ReservationPick(Base):
    __tablename__ = "reservation_pick"
    __table_args__ = {"schema": "wms"}
    task_id: Mapped[int] = mapped_column(ForeignKey("wms.task.id"), primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("wms.reservation_event_state.document_id"), index=True)


async def apply(session, document_id, items, *, organization_id, release):
    quantities = {}
    for sku, _, qty in items:
        quantities[sku] = quantities.get(sku, Decimal("0")) + qty
    quantities = {sku: format(qty, ".2f") for sku, qty in sorted(quantities.items())}
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise ValueError("Unsupported reservation event database")
    # A real source row serializes even the first two competing deliveries.
    await session.execute(insert(ReservationEventState).values(document_id=document_id, state="new", quantities=quantities).on_conflict_do_nothing(index_elements=["document_id"]))
    source = await session.scalar(select(ReservationEventState).where(ReservationEventState.document_id == document_id).with_for_update().execution_options(populate_existing=True))
    if source.quantities != quantities:
        raise ValueError("Invoice reservation quantity changed; reconciliation is required")
    snapshot = sorted([[sku, warehouse, format(qty, ".2f")] for sku, warehouse, qty in items])
    if not release and source.state == "reserved" and source.reserved_items != snapshot:
        raise ValueError("Invoice reservation allocation changed; reconciliation is required")
    if source.state == "released" or (source.state == "reserved" and not release):
        return
    if release:
        from modules.wms.invoice_shipments import no_physical_act
        await no_physical_act(session, document_id)
        tasks = (await session.scalars(select(Task).join(ReservationPick, ReservationPick.task_id == Task.id).where(ReservationPick.document_id == document_id).with_for_update().execution_options(populate_existing=True))).all()
        if any(task.organization_id != organization_id for task in tasks):
            raise ValueError("Invoice picking organization differs; reconciliation is required")
        if any(task.status not in {"open", "canceled"} for task in tasks):
            raise ValueError("Invoice picking already started; reconcile shipment before releasing")
        for task in tasks:
            task.status = "canceled"
        source.state = "released"
    else:
        source.reserved_items = snapshot
        for sku, warehouse, qty in items:
            task = Task(organization_id=organization_id, kind="pick", sku_code=sku, qty=qty, warehouse=warehouse, doc_ref=f"sales:document:{document_id}", status="open")
            session.add(task)
            await session.flush()
            session.add(ReservationPick(task_id=task.id, document_id=document_id))
        source.state = "reserved"
    await session.flush()
    if not release:
        from modules.wms.invoice_reservations import InvoiceReservation
        from modules.wms.invoice_shipments import PhysicalShipmentAct, proved_chain, version_chains
        if await session.scalar(select(PhysicalShipmentAct.id).where(PhysicalShipmentAct.document_id == document_id).limit(1)):
            reservation = await session.get(InvoiceReservation, document_id)
            allocations = sorted(reservation.snapshot["allocations"], key=lambda row: row["source"])
            latest = await proved_chain(session, reservation, allocations, await version_chains(session, allocations))
            tasks = (await session.scalars(select(Task).join(ReservationPick, ReservationPick.task_id == Task.id)
                .where(ReservationPick.document_id == document_id))).all()
            from datetime import datetime, timezone
            for task in tasks:
                remaining = sum(r.qty for r in latest if r.sku_code == task.sku_code and r.warehouse == task.warehouse)
                if remaining == 0:
                    task.status = "done"
                    task.done_at = datetime.now(timezone.utc).replace(tzinfo=None)
            await session.flush()
