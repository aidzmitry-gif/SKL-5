"""Addressed invoice reservations; caller supplies verified sales source facts.

Internal transactional service, not a client-facing route. Sales authorization
and immutable source validation must precede invocation under the organization lock.
"""
import hashlib
import json
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    event,
    func,
    or_,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base
from core.services.wms import (  # noqa: F401 -- preserve existing internal imports
    EvidenceReference,
    FulfillmentVerifier,
    VerifiedNoShipment,
    no_shipment_review_digest,
)
from modules.wms.models import ReservationVersion, StockMovement, Task
from modules.wms.reservation_events import ReservationEventState, ReservationPick
from modules.wms.reservations import _latest_versions
from modules.wms.warehouse_modes import current_modes, frozen_modes


class InvoiceReservation(Base):
    __tablename__ = "invoice_reservation"
    __table_args__ = {"schema": "wms"}
    document_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    organization_id: Mapped[int] = mapped_column(Integer, index=True)
    digest: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSON)
    execution_modes: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    actor: Mapped[str] = mapped_column(String(200))


def immutable(*args):
    raise ValueError("Invoice reservation identity is immutable")


event.listen(InvoiceReservation, "before_update", immutable)
event.listen(InvoiceReservation, "before_delete", immutable)


class Allocation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    line_no: int = Field(gt=0, strict=True)
    warehouse: str = Field(min_length=1, max_length=128)
    qty: Decimal = Field(gt=0, max_digits=14, decimal_places=2, allow_inf_nan=False)

    @field_validator("qty", mode="before")
    @classmethod
    def exact(cls, value):
        if isinstance(value, (float, bool)):
            raise ValueError("Quantity must be exact")
        return value


class ReserveInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    allocations: list[Allocation] = Field(min_length=1, max_length=1000)
    evidence: str = Field(min_length=1, max_length=1000)
    journal_complete: bool = Field(strict=True)


def _reserved_quantity(value):
    if (not isinstance(value, Decimal) or not value.is_finite() or value < 0
            or value >= Decimal("1000000000000") or value != value.quantize(Decimal("0.01"))):
        raise HTTPException(409, "Stored reservation quantity requires reconciliation")
    return value


async def invoice_availability(session, organization_id, sku_codes):
    """One organization-locked observation; reserve rechecks before committing."""
    from modules.wms.routes import _physical_inventory_snapshot

    if not session.in_transaction() or type(organization_id) is not int or organization_id <= 0:
        raise ValueError("Availability requires the caller's organization transaction")
    codes = set(sku_codes)
    if not codes or any(not isinstance(code, str) or not code.strip() for code in codes):
        raise ValueError("Availability requires exact SKU codes")
    latest = _latest_versions(organization_id)
    reserved = (await session.scalars(select(ReservationVersion).join(latest,
        (ReservationVersion.source == latest.c.source) & (ReservationVersion.version == latest.c.version)
    ).where(ReservationVersion.organization_id == organization_id, ReservationVersion.sku_code.in_(codes)))).all()
    physical_warehouses = set((await session.scalars(select(StockMovement.warehouse).where(
        StockMovement.organization_id == organization_id, StockMovement.sku_code.in_(codes),
        StockMovement.reason.not_in(["reserve", "release"])).distinct())).all())
    totals = defaultdict(lambda: Decimal("0"))
    for row in reserved:
        totals[(row.warehouse, row.sku_code)] += _reserved_quantity(row.qty)
    rows, basis = [], {}
    from modules.wms.stock_policy import policies
    warehouses = physical_warehouses | {row.warehouse for row in reserved}
    policy = await policies(session, organization_id, warehouses)
    for warehouse in sorted(warehouses):
        quantities, version, cutoff = ({}, None, None)
        if warehouse in physical_warehouses:
            quantities, version, cutoff = await _physical_inventory_snapshot(session, organization_id, warehouse, allow_negative=True)
        basis[warehouse] = {"version": version, "cutoff": cutoff}
        for code in sorted(codes):
            if code not in quantities and (warehouse, code) not in totals:
                continue
            physical = quantities.get(code)
            held = totals[(warehouse, code)]
            rows.append({"sku_code": code, "warehouse": warehouse,
                "prohibit_negative_stock": policy[warehouse],
                "physical": None if physical is None else format(physical, ".2f"),
                "reserved": format(held, ".2f"),
                "free": None if physical is None else format(physical - held, ".2f")})
    return {"organization_id": organization_id, "source": "wms_physical", "rows": rows,
            "basis_by_warehouse": basis}


async def reserve(session, organization_id, source, data: ReserveInput, actor):
    """Reserve all original goods lines atomically; never commits or moves stock.

    Caller must hold the accounting organization lock followed by the sales
    document lock. A stored result is an exact replay, not new availability.
    """
    if source["organization_id"] != organization_id or source["reserve_status"] != "reserved":
        raise HTTPException(409, "Invoice source does not authorize an active reservation")
    if not data.journal_complete:
        raise HTTPException(422, "Confirm physical journal completeness")
    original = {line["line_no"]: line for line in source["lines"]}
    totals = {line_no: Decimal("0") for line_no in original}
    seen = set()
    rows = []
    for allocation in sorted(data.allocations, key=lambda row: (row.line_no, row.warehouse)):
        line = original.get(allocation.line_no)
        key = (allocation.line_no, allocation.warehouse)
        if line is None or key in seen:
            raise HTTPException(422, "Unknown or repeated invoice line allocation")
        seen.add(key)
        totals[allocation.line_no] += allocation.qty
        warehouse_key = hashlib.sha256(allocation.warehouse.encode()).hexdigest()[:32]
        rows.append({"line_no": allocation.line_no, "sku_code": line["sku_code"],
                     "warehouse": allocation.warehouse, "qty": format(allocation.qty, ".2f"),
                     "source": f"invoice:{source['document_id']}:{allocation.line_no}:{warehouse_key}"})
    if any(totals[line_no] != Decimal(line["qty"]) for line_no, line in original.items()):
        raise HTTPException(409, "Allocations must match each original invoice line exactly")
    basis = {"document_id": source["document_id"], "organization_id": organization_id,
             "version": source["version"], "content_sha256": source["content_sha256"],
             "allocations": rows, "evidence": data.evidence}
    digest = hashlib.sha256(json.dumps(basis, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    existing = await session.get(InvoiceReservation, source["document_id"])
    if existing:
        try:
            frozen_modes(existing.execution_modes, {row["warehouse"] for row in rows})
        except ValueError:
            _reject("reservation_execution_mode_corrupt")
        if existing.organization_id != organization_id or existing.digest != digest:
            raise HTTPException(409, "Invoice already has another confirmed reservation")
        return {"digest": existing.digest, "snapshot": existing.snapshot}
    try:
        execution_modes = await current_modes(session, organization_id,
            {row["warehouse"] for row in rows}, lock=True)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    # Import locally: route helpers are within WMS; no accounting/sales table reads.
    from modules.wms.routes import _physical_inventory_snapshot

    needed = {}
    for row in rows:
        key = (row["warehouse"], row["sku_code"])
        needed[key] = needed.get(key, Decimal("0")) + Decimal(row["qty"])
    latest = _latest_versions(organization_id)
    outstanding = (await session.scalars(select(ReservationVersion).join(latest,
        (ReservationVersion.source == latest.c.source) & (ReservationVersion.version == latest.c.version)
    ).where(ReservationVersion.organization_id == organization_id))).all()
    from modules.wms.stock_policy import policies, check
    policy = await policies(session, organization_id, {key[0] for key in needed})
    snapshots = {}
    for warehouse in sorted({key[0] for key in needed}):
        try:
            quantities, version, cutoff = await _physical_inventory_snapshot(session, organization_id, warehouse, allow_negative=True)
        except HTTPException as exc:
            if exc.status_code == 409 and exc.detail == "Ожидаемый остаток неизвестен: нет подтверждённых физических движений":
                sku, qty = next((sku, qty) for (place, sku), qty in needed.items() if place == warehouse)
                check(sku, warehouse, qty, None, Decimal("0"), policy[warehouse])
            raise
        snapshots[warehouse] = {"version": version, "cutoff": cutoff}
        for (place, sku), qty in needed.items():
            if place != warehouse:
                continue
            used = sum(_reserved_quantity(row.qty) for row in outstanding if row.warehouse == warehouse and row.sku_code == sku)
            check(sku, warehouse, qty, quantities.get(sku), used, policy[warehouse])
    snapshot = {**basis, "physical_basis": snapshots}
    session.add(InvoiceReservation(document_id=source["document_id"], organization_id=organization_id,
                                   digest=digest, snapshot=snapshot,
                                   execution_modes=execution_modes, actor=actor))
    for row in rows:
        session.add(ReservationVersion(organization_id=organization_id, source=row["source"], version=1,
            sku_code=row["sku_code"], warehouse=row["warehouse"], qty=Decimal(row["qty"]),
            evidence=data.evidence, actor=actor))
    await session.flush()
    return {"digest": digest, "snapshot": snapshot}


class InvoiceReservationRelease(Base):
    __tablename__ = "invoice_reservation_release"
    __table_args__ = (UniqueConstraint("organization_id", "source_key"),
                     CheckConstraint("organization_id > 0", name="invoice_release_org"), {"schema": "wms"})
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("wms.invoice_reservation.document_id"), unique=True)
    organization_id: Mapped[int] = mapped_column(Integer)
    source_key: Mapped[str] = mapped_column(String(160))
    request_hash: Mapped[str] = mapped_column(String(64))
    digest: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSON)
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InvoiceReservationReleaseLine(Base):
    __tablename__ = "invoice_reservation_release_line"
    __table_args__ = (UniqueConstraint("release_id", "source"),
        CheckConstraint("line_no > 0 AND released_qty > 0 AND before_id <> after_id", name="invoice_release_line_valid"),
        {"schema": "wms"})
    id: Mapped[int] = mapped_column(primary_key=True)
    release_id: Mapped[int] = mapped_column(ForeignKey("wms.invoice_reservation_release.id"))
    before_id: Mapped[int] = mapped_column(ForeignKey("wms.reservation_version.id"), unique=True)
    after_id: Mapped[int] = mapped_column(ForeignKey("wms.reservation_version.id"), unique=True)
    source: Mapped[str] = mapped_column(String(200))
    line_no: Mapped[int] = mapped_column(Integer)
    released_qty: Mapped[Decimal] = mapped_column(Numeric(14, 2))


for _model in (InvoiceReservationRelease, InvoiceReservationReleaseLine):
    event.listen(_model, "before_update", immutable)
    event.listen(_model, "before_delete", immutable)


class ReleaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    source_key: str = Field(min_length=1, max_length=160)
    expected_reservation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_remaining_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    fulfillment_review_id: str = Field(min_length=1, max_length=160)
    fulfillment_review_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence: str = Field(min_length=1, max_length=1000)



def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _reject(code):
    raise HTTPException(409, code)


def _qty(value):
    if isinstance(value, (bool, float)):
        _reject("quantity_not_exact")
    try:
        qty = Decimal(str(value))
        valid = qty.is_finite() and qty > 0 and qty == qty.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        valid = False
    if not valid:
        _reject("quantity_invalid")
    return qty


def _matrix(rows):
    result = defaultdict(lambda: Decimal("0"))
    for sku, warehouse, qty in rows:
        result[(sku, warehouse)] += _qty(qty)
    return sorted([[sku, place, format(qty, ".2f")] for (sku, place), qty in result.items()])


def _reservation_identity(reservation, org, source):
    snap = reservation.snapshot
    if (type(org) is not int or org <= 0 or reservation.organization_id != org
            or source["organization_id"] != org or source["document_id"] != reservation.document_id
            or source["version"] != snap.get("version")
            or source["content_sha256"] != snap.get("content_sha256")):
        _reject("reservation_identity_mismatch")
    basis = {key: snap[key] for key in (
        "document_id", "organization_id", "version", "content_sha256", "allocations", "evidence")}
    try:
        frozen_modes(reservation.execution_modes, {row["warehouse"] for row in snap["allocations"]})
    except (KeyError, TypeError, ValueError):
        _reject("reservation_execution_mode_corrupt")
    legacy_digest = hashlib.sha256(json.dumps(basis, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if (legacy_digest != reservation.digest or snap["document_id"] != source["document_id"]
            or snap["organization_id"] != org):
        _reject("reservation_digest_mismatch")
    originals = {line["line_no"]: line for line in source["lines"]}
    if len(originals) != len(source["lines"]) or not originals:
        _reject("invoice_lines_invalid")
    totals, seen = defaultdict(lambda: Decimal("0")), set()
    for row in snap["allocations"]:
        key = (row["line_no"], row["warehouse"])
        line = originals.get(row["line_no"])
        expected = f"invoice:{source['document_id']}:{row['line_no']}:" + hashlib.sha256(row["warehouse"].encode()).hexdigest()[:32]
        if (key in seen or not row["warehouse"] or line is None
                or row["sku_code"] != line["sku_code"] or row["source"] != expected):
            _reject("allocation_identity_mismatch")
        seen.add(key)
        totals[row["line_no"]] += _qty(row["qty"])
    if any(totals[number] != _qty(line["qty"]) for number, line in originals.items()):
        _reject("allocation_line_quantity_mismatch")
    if len({row["source"] for row in snap["allocations"]}) != len(snap["allocations"]):
        _reject("allocation_source_collision")
    return sorted(snap["allocations"], key=lambda row: row["source"])


async def _locked_reservation(session, org, source):
    if not session.in_transaction():
        raise RuntimeError("Caller must hold organization/deal/document transaction locks")
    reservation = await session.scalar(select(InvoiceReservation).where(
        InvoiceReservation.document_id == source["document_id"],
    ).with_for_update().execution_options(populate_existing=True))
    if reservation is None:
        _reject("addressed_reservation_missing")
    try:
        allocations = _reservation_identity(reservation, org, source)
    except (KeyError, TypeError, ValueError, AttributeError):
        _reject("reservation_evidence_corrupt")
    return reservation, allocations


async def _remaining(session, reservation, allocations):
    keys = [row["source"] for row in allocations]
    versions = (await session.scalars(select(ReservationVersion).where(
        ReservationVersion.source.in_(keys),
    ).order_by(ReservationVersion.source, ReservationVersion.version)
        .with_for_update().execution_options(populate_existing=True))).all()
    by_source = defaultdict(list)
    for row in versions:
        by_source[row.source].append(row)
    latest = []
    for allocation in allocations:
        chain = by_source[allocation["source"]]
        if not chain:
            _reject("reservation_version_missing")
        for index, row in enumerate(chain, 1):
            if (row.organization_id != reservation.organization_id or row.version != index
                    or row.sku_code != allocation["sku_code"] or row.warehouse != allocation["warehouse"]):
                _reject("reservation_version_identity_mismatch")
    from modules.wms.invoice_shipments import proved_chain
    latest = await proved_chain(session, reservation, allocations, by_source)
    state = await session.scalar(select(ReservationEventState).where(
        ReservationEventState.document_id == reservation.document_id,
    ).with_for_update().execution_options(populate_existing=True))
    tasks = (await session.scalars(select(Task).join(ReservationPick, ReservationPick.task_id == Task.id)
        .where(ReservationPick.document_id == reservation.document_id).order_by(Task.id)
        .with_for_update().execution_options(populate_existing=True))).all()
    matrix = _matrix((a["sku_code"], a["warehouse"], a["qty"]) for a in allocations)
    modes = frozen_modes(reservation.execution_modes, {row["warehouse"] for row in allocations})
    wms_matrix = _matrix((a["sku_code"], a["warehouse"], a["qty"])
                         for a in allocations if modes[a["warehouse"]])
    quantities = defaultdict(lambda: Decimal("0"))
    for sku, _, qty in matrix:
        quantities[sku] += Decimal(qty)
    quantities = {sku: format(qty, ".2f") for sku, qty in sorted(quantities.items())}
    if state is None and tasks:
        _reject("pick_state_missing")
    if state is not None:
        if state.state not in {"new", "reserved"} or state.quantities != quantities:
            _reject("event_state_mismatch")
        if state.state == "reserved" and (
            not state.reserved_items or _matrix(state.reserved_items) != matrix
        ):
            _reject("event_allocation_mismatch")
    for task in tasks:
        if task.organization_id != reservation.organization_id or task.kind != "pick":
            _reject("pick_identity_mismatch")
        if modes.get(task.warehouse) is not True:
            _reject("pick_execution_mode_mismatch")
        exhausted = all(r.qty == 0 for r in latest if r.sku_code == task.sku_code and r.warehouse == task.warehouse)
        if task.status == "done" and task.done_at is not None and exhausted:
            pass
        elif task.status not in {"open", "in_progress", "canceled"} or task.done_at is not None:
            _reject("pick_execution_requires_reconciliation")
    if tasks or (state is not None and state.state == "reserved"):
        if _matrix((t.sku_code, t.warehouse, t.qty) for t in tasks) != wms_matrix:
            _reject("pick_allocation_mismatch")
    refs = [f"sales:document:{reservation.document_id}"] + [f"PICK-{t.id:05d}" for t in tasks]
    if await session.scalar(select(StockMovement.id).where(
        StockMovement.organization_id == reservation.organization_id,
        StockMovement.kind == "out", StockMovement.doc_ref.in_(refs),
    ).limit(1)):
        _reject("physical_issue_requires_reconciliation")
    before = {"reservation_digest": reservation.digest,
        "versions": [{"id": r.id, "source": r.source, "version": r.version,
                      "sku_code": r.sku_code, "warehouse": r.warehouse, "qty": format(r.qty, ".2f")}
                     for r in latest],
        "event_state": None if state is None else {"state": state.state,
            "quantities": state.quantities, "reserved_items": state.reserved_items},
        "picks": [{"id": t.id, "organization_id": t.organization_id, "kind": t.kind,
                   "status": t.status, "sku_code": t.sku_code, "warehouse": t.warehouse,
                   "qty": format(t.qty, ".2f"), "done_at": t.done_at.isoformat() if t.done_at else None} for t in tasks]}
    return latest, state, tasks, quantities, before


async def release_preview(session, organization_id, source):
    """Internal locked preview; not permission to cancel or evidence of no shipment."""
    from modules.wms.invoice_shipments import no_physical_act
    await no_physical_act(session, source["document_id"])
    reservation, allocations = await _locked_reservation(session, organization_id, source)
    *_, before = await _remaining(session, reservation, allocations)
    return {"reservation_digest": reservation.digest, "remaining_digest": _digest(before), "snapshot": before}


def _verified_review(review, org, source, reservation, data):
    if not isinstance(review, VerifiedNoShipment):
        _reject("verified_fulfillment_required")
    if (review.organization_id != org or review.document_id != source["document_id"]
            or review.document_version != source["version"]
            or review.content_sha256 != source["content_sha256"]
            or review.reservation_digest != reservation.digest
            or review.review_id != data.fulfillment_review_id
            or review.review_digest != data.fulfillment_review_digest or not review.reviewed_by
            or review.review_digest != no_shipment_review_digest(review)):
        _reject("fulfillment_review_mismatch")
    scopes = {"wms_issue", "wms_pick", "logistics_shipment", "accounting_issue", "legacy_fulfillment"}
    if {r.scope for r in review.references} != scopes or not review.references:
        _reject("fulfillment_coverage_incomplete")
    refs = []
    for ref in review.references:
        if (not ref.record_id or not ref.revision or len(ref.sha256) != 64
                or any(c not in "0123456789abcdef" for c in ref.sha256)):
            _reject("fulfillment_reference_invalid")
        refs.append({"scope": ref.scope, "record_id": ref.record_id,
                     "revision": ref.revision, "sha256": ref.sha256})
    return {"review_id": review.review_id, "digest": review.review_digest,
            "reviewed_by": review.reviewed_by, "references": sorted(refs, key=_canonical)}


async def _release_result(session, record, allocations):
    lines = (await session.scalars(select(InvoiceReservationReleaseLine).where(
        InvoiceReservationReleaseLine.release_id == record.id,
    ).order_by(InvoiceReservationReleaseLine.source).execution_options(populate_existing=True))).all()
    if (_digest(record.snapshot) != record.digest or len(lines) != len(record.snapshot["lines"])
            or len(lines) != len(allocations)):
        _reject("release_evidence_corrupt")
    for line, snap, allocation in zip(lines, record.snapshot["lines"], allocations, strict=True):
        if (line.source != allocation["source"] or line.line_no != allocation["line_no"]
                or line.released_qty != _qty(allocation["qty"])
                or snap["sku_code"] != allocation["sku_code"]
                or snap["warehouse"] != allocation["warehouse"]):
            _reject("release_original_allocation_mismatch")
        if (line.before_id != snap["before_id"] or line.after_id != snap["after_id"]
                or line.source != snap["source"] or line.line_no != snap["line_no"]
                or line.released_qty != Decimal(snap["released_qty"])):
            _reject("release_evidence_corrupt")
        current = await session.scalar(select(ReservationVersion).where(
            ReservationVersion.organization_id == record.organization_id,
            ReservationVersion.source == line.source,
        ).order_by(ReservationVersion.version.desc()).limit(1)
            .with_for_update().execution_options(populate_existing=True))
        if current is None or current.id != line.after_id or current.qty != 0:
            _reject("released_reservation_changed")
        prior = await session.scalar(select(ReservationVersion).where(ReservationVersion.id == line.before_id)
            .execution_options(populate_existing=True))
        if (prior is None or prior.organization_id != record.organization_id
                or current.source != line.source or prior.source != line.source
                or prior.sku_code != snap["sku_code"] or current.sku_code != snap["sku_code"]
                or prior.warehouse != snap["warehouse"] or current.warehouse != snap["warehouse"]
                or prior.version != snap["before_version"] or current.version != snap["after_version"]
                or prior.qty != line.released_qty):
            _reject("release_evidence_corrupt")
    state = await session.scalar(select(ReservationEventState).where(
        ReservationEventState.document_id == record.document_id,
    ).execution_options(populate_existing=True))
    tasks = (await session.scalars(select(Task).join(ReservationPick, ReservationPick.task_id == Task.id)
        .where(ReservationPick.document_id == record.document_id).order_by(Task.id)
        .execution_options(populate_existing=True))).all()
    if (state is None or state.state != "released"
            or [t.id for t in tasks] != [p["id"] for p in record.snapshot["before"]["picks"]]
            or any(t.status != "canceled" or t.done_at is not None for t in tasks)):
        _reject("released_pick_state_changed")
    for task, prior in zip(tasks, record.snapshot["before"]["picks"], strict=True):
        if (task.organization_id != prior["organization_id"] or task.kind != prior["kind"]
                or task.sku_code != prior["sku_code"] or task.warehouse != prior["warehouse"]
                or task.qty != Decimal(prior["qty"])):
            _reject("released_pick_identity_changed")
    return {"release_id": record.id, "digest": record.digest,
            "snapshot": json.loads(_canonical(record.snapshot))}


async def release(session, organization_id, source, data: ReleaseInput, actor,
                  fulfillment_verifier: FulfillmentVerifier):
    """Release all unshipped addressed remaining quantities; caller commits.

    Caller owns org -> deal -> document locks, sales/money authorization and
    subsequent cancellation. Paid is not a WMS blocker. No physical movements,
    notifications, legacy stock.release, or sales state changes are performed.
    """
    from modules.wms.invoice_shipments import no_physical_act
    await no_physical_act(session, source["document_id"])
    reservation, allocations = await _locked_reservation(session, organization_id, source)
    if reservation.digest != data.expected_reservation_digest:
        _reject("reservation_basis_changed")
    request_hash = _digest({"organization_id": organization_id, "document_id": source["document_id"],
        "version": source["version"], "content_sha256": source["content_sha256"],
        "data": data.model_dump()})
    existing = (await session.scalars(select(InvoiceReservationRelease).where(or_(
        InvoiceReservationRelease.document_id == source["document_id"],
        (InvoiceReservationRelease.organization_id == organization_id)
        & (InvoiceReservationRelease.source_key == data.source_key),
    )).with_for_update().execution_options(populate_existing=True))).all()
    if existing:
        if (len(existing) != 1 or existing[0].organization_id != organization_id
                or existing[0].document_id != source["document_id"] or existing[0].request_hash != request_hash):
            _reject("release_request_conflict")
        return await _release_result(session, existing[0], allocations)
    if source["reserve_status"] != "reserved":
        _reject("active_reservation_required")
    latest, state, tasks, quantities, before = await _remaining(session, reservation, allocations)
    if _digest(before) != data.expected_remaining_digest:
        _reject("remaining_basis_changed")
    if fulfillment_verifier is None:
        _reject("verified_fulfillment_required")
    review = await fulfillment_verifier.verify_no_shipment(session, organization_id, source,
        data.fulfillment_review_id, data.fulfillment_review_digest)
    evidence = _verified_review(review, organization_id, source, reservation, data)
    if not isinstance(actor, str) or not actor.strip():
        _reject("release_actor_required")
    after = []
    for row in latest:
        new = ReservationVersion(organization_id=organization_id, source=row.source,
            version=row.version + 1, sku_code=row.sku_code, warehouse=row.warehouse,
            qty=Decimal("0"), evidence=data.evidence, actor=actor)
        session.add(new)
        after.append(new)
    await session.flush()
    if state is None:
        state = ReservationEventState(document_id=source["document_id"], state="released",
                                       quantities=quantities, reserved_items=None)
        session.add(state)
    else:
        state.state = "released"
    for task in tasks:
        if task.status in {"open", "in_progress"}:
            task.status = "canceled"
    line_snapshots = [{"source": old.source, "line_no": allocation["line_no"],
        "sku_code": old.sku_code, "warehouse": old.warehouse,
        "before_id": old.id, "before_version": old.version, "before_qty": format(old.qty, ".2f"),
        "after_id": new.id, "after_version": new.version, "after_qty": "0.00",
        "released_qty": format(old.qty, ".2f")}
        for allocation, old, new in zip(allocations, latest, after, strict=True)]
    snapshot = {"document_id": source["document_id"], "organization_id": organization_id,
        "document_version": source["version"], "content_sha256": source["content_sha256"],
        "reservation_digest": reservation.digest, "before": before, "fulfillment": evidence,
        "lines": line_snapshots, "evidence": data.evidence, "actor": actor}
    record = InvoiceReservationRelease(document_id=source["document_id"], organization_id=organization_id,
        source_key=data.source_key, request_hash=request_hash, digest=_digest(snapshot), snapshot=snapshot, actor=actor)
    session.add(record)
    await session.flush()
    for line in line_snapshots:
        session.add(InvoiceReservationReleaseLine(release_id=record.id, before_id=line["before_id"],
            after_id=line["after_id"], source=line["source"], line_no=line["line_no"],
            released_qty=Decimal(line["released_qty"])))
    await session.flush()
    return await _release_result(session, record, allocations)
