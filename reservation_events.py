"""Durable invoice-event ordering and private pick bindings, no physical movement."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from sqlalchemy import JSON, ForeignKey, String, select
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base
from modules.wms.models import Task
from modules.wms.warehouse_modes import frozen_modes


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


def _exact_event_qty(value):
    if not isinstance(value, str):
        raise ValueError("Legacy reservation quantity is not canonical")
    try:
        qty = Decimal(value)
        valid = qty.is_finite() and qty > 0 and qty == qty.quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Legacy reservation quantity is invalid") from exc
    if not valid or format(qty, ".2f") != value:
        raise ValueError("Legacy reservation quantity is invalid")
    return qty


async def validate_legacy_pick_binding(session, document_id, organization_id, task_id):
    """Prove an event-only legacy pick before allowing its former limited updates."""
    if (type(document_id) is not int or document_id <= 0
            or type(organization_id) is not int or organization_id <= 0
            or type(task_id) is not int or task_id <= 0):
        raise ValueError("Legacy reservation identity is invalid")
    state = await session.scalar(select(ReservationEventState).where(
        ReservationEventState.document_id == document_id,
    ).with_for_update().execution_options(populate_existing=True))
    if state is None or state.state != "reserved" or not isinstance(state.reserved_items, list):
        raise ValueError("Legacy reservation event is missing or incomplete")

    expected = defaultdict(Decimal)
    totals = defaultdict(Decimal)
    for item in state.reserved_items:
        if (not isinstance(item, list) or len(item) != 3
                or not isinstance(item[0], str) or not item[0].strip()
                or not isinstance(item[1], str) or not item[1].strip()):
            raise ValueError("Legacy reservation event matrix is invalid")
        qty = _exact_event_qty(item[2])
        expected[(item[0], item[1])] += qty
        totals[item[0]] += qty
    expected_quantities = {sku: format(qty, ".2f") for sku, qty in sorted(totals.items())}
    if not expected or state.quantities != expected_quantities:
        raise ValueError("Legacy reservation event quantities do not match its matrix")

    tasks = (await session.scalars(select(Task).join(
        ReservationPick, ReservationPick.task_id == Task.id,
    ).where(ReservationPick.document_id == document_id).order_by(Task.id)
        .with_for_update().execution_options(populate_existing=True))).all()
    if not tasks or task_id not in {task.id for task in tasks}:
        raise ValueError("Legacy reservation pick binding is missing")
    actual = defaultdict(Decimal)
    for task in tasks:
        if (task.organization_id != organization_id or task.kind != "pick"
                or task.doc_ref != f"sales:document:{document_id}"
                or not isinstance(task.sku_code, str) or not task.sku_code.strip()
                or not isinstance(task.warehouse, str) or not task.warehouse.strip()
                or task.status not in {"open", "in_progress", "done", "canceled"}
                or (task.status == "done") != (task.done_at is not None)):
            raise ValueError("Legacy reservation task identity requires reconciliation")
        try:
            qty = Decimal(task.qty)
            valid = qty.is_finite() and qty > 0 and qty == qty.quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("Legacy reservation task quantity is invalid") from exc
        if not valid:
            raise ValueError("Legacy reservation task quantity is invalid")
        actual[(task.sku_code, task.warehouse)] += qty
    if dict(actual) != dict(expected):
        raise ValueError("Legacy reservation tasks do not match the event matrix")
    return True


async def apply(session, document_id, items, *, organization_id, release):
    from modules.wms.invoice_reservations import InvoiceReservation

    reservation = await session.get(InvoiceReservation, document_id, populate_existing=True)
    try:
        item_warehouses = {warehouse for _, warehouse, _ in items}
        if reservation is None:
            # Existing reservation events can predate InvoiceReservation rows; NULL
            # legacy execution remains WMS-enabled for those historical invoices.
            modes = frozen_modes(None, item_warehouses)
        else:
            if reservation.organization_id != organization_id:
                raise ValueError("Reservation belongs to another organization")
            allocation_warehouses = {
                row["warehouse"] for row in reservation.snapshot["allocations"]
            }
            modes = frozen_modes(reservation.execution_modes, allocation_warehouses)
            if not item_warehouses.issubset(modes):
                raise ValueError("Reservation event contains an unknown warehouse")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invoice warehouse execution mode requires reconciliation") from exc
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
        if any(task.organization_id != organization_id or task.kind != "pick"
               or modes.get(task.warehouse) is not True for task in tasks):
            raise ValueError("Invoice picking identity or frozen mode requires reconciliation")
        if any(task.status not in {"open", "canceled"} for task in tasks):
            raise ValueError("Invoice picking already started; reconcile shipment before releasing")
        for task in tasks:
            task.status = "canceled"
        source.state = "released"
    else:
        source.reserved_items = snapshot
        for sku, warehouse, qty in items:
            if not modes[warehouse]:
                continue
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
            if reservation is None or reservation.organization_id != organization_id:
                raise ValueError("Invoice picking reservation requires reconciliation")
            allocations = sorted(reservation.snapshot["allocations"], key=lambda row: row["source"])
            modes = frozen_modes(reservation.execution_modes,
                                 {row["warehouse"] for row in allocations})
            latest = await proved_chain(session, reservation, allocations, await version_chains(session, allocations))
            tasks = (await session.scalars(select(Task).join(ReservationPick, ReservationPick.task_id == Task.id)
                .where(ReservationPick.document_id == document_id))).all()
            if any(task.organization_id != organization_id or task.kind != "pick"
                   or modes.get(task.warehouse) is not True for task in tasks):
                raise ValueError("Invoice picking mode requires reconciliation")
            from datetime import datetime, timezone
            for task in tasks:
                remaining = sum(r.qty for r in latest if r.sku_code == task.sku_code and r.warehouse == task.warehouse)
                if remaining == 0:
                    task.status = "done"
                    task.done_at = datetime.now(timezone.utc).replace(tzinfo=None)
            await session.flush()
