"""Release only unshipped reservation quantities; never cancel the invoice.

Caller holds organization -> deal -> document locks until commit. Physical acts,
original invoice and money stay unchanged. This is a local warehouse operation.
"""
from copy import deepcopy
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import Field, field_validator
from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
    event,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base
from core.runtime.deps import get_core, get_session
from core.services.auth import require_permission
from modules.wms.invoice_reservations import (
    InvoiceReservation,
    _digest,
    _locked_reservation,
    _remaining,
)
from modules.wms.invoice_shipments import (
    PhysicalShipmentAct,
    ShipmentIdentity,
    act_result,
    immutable,
    reject,
    source_facts,
)
from modules.wms.models import ReservationVersion


class RemainderRelease(Base):
    __tablename__ = "invoice_remainder_release"
    __table_args__ = (UniqueConstraint("organization_id", "source_key"),
                     UniqueConstraint("document_id"), {"schema": "wms"})
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column()
    document_id: Mapped[int] = mapped_column(ForeignKey("wms.invoice_reservation.document_id"))
    source_key: Mapped[str] = mapped_column(String(36))
    request_hash: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(200))
    snapshot: Mapped[dict] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(64))


class RemainderReleaseLine(Base):
    __tablename__ = "invoice_remainder_release_line"
    __table_args__ = (UniqueConstraint("release_id", "source"), UniqueConstraint("before_id"),
                     UniqueConstraint("after_id"), CheckConstraint("qty > 0 AND line_no > 0", name="remainder_positive"), {"schema": "wms"})
    id: Mapped[int] = mapped_column(primary_key=True)
    release_id: Mapped[int] = mapped_column(ForeignKey("wms.invoice_remainder_release.id"))
    source: Mapped[str] = mapped_column(String(128))
    line_no: Mapped[int] = mapped_column()
    qty: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    before_id: Mapped[int] = mapped_column(ForeignKey("wms.reservation_version.id"))
    after_id: Mapped[int] = mapped_column(ForeignKey("wms.reservation_version.id"))


for model in (RemainderRelease, RemainderReleaseLine):
    event.listen(model, "before_update", immutable)
    event.listen(model, "before_delete", immutable)


class ReleaseInput(ShipmentIdentity):
    source_key: UUID
    expected_basis_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence: str = Field(min_length=1, max_length=1000)

    @field_validator("evidence")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Release reason is required")
        return value


async def lookup(session, org, document, key):
    row = await session.scalar(select(RemainderRelease).where(
        RemainderRelease.organization_id == org, RemainderRelease.source_key == str(key)
    ).execution_options(populate_existing=True))
    if row and row.document_id != document:
        reject("remainder_request_conflict")
    return row


async def result(session, receipt):
    snap = receipt.snapshot
    if not isinstance(snap, dict) or _digest(snap) != receipt.digest:
        reject("remainder_evidence_corrupt")
    for key in ("organization_id", "document_id", "source_key", "request_hash", "actor"):
        if snap.get(key) != getattr(receipt, key):
            reject("remainder_identity_corrupt")
    original = await session.get(InvoiceReservation, receipt.document_id, populate_existing=True)
    if not original or original.organization_id != receipt.organization_id or original.digest != snap.get("reservation_digest"):
        reject("remainder_original_changed")
    acts = (await session.scalars(select(PhysicalShipmentAct).where(
        PhysicalShipmentAct.document_id == receipt.document_id).order_by(PhysicalShipmentAct.id)
        .execution_options(populate_existing=True))).all()
    anchors = [{"id": a.id, "digest": (await act_result(session, a))["digest"]} for a in acts]
    if not anchors or anchors != snap.get("acts"):
        reject("remainder_acts_changed")
    lines = (await session.scalars(select(RemainderReleaseLine).where(
        RemainderReleaseLine.release_id == receipt.id).order_by(RemainderReleaseLine.source)
        .execution_options(populate_existing=True))).all()
    allocations = {a["source"]: a for a in original.snapshot["allocations"]}
    verified = []
    for line in lines:
        before = await session.get(ReservationVersion, line.before_id, populate_existing=True)
        after = await session.get(ReservationVersion, line.after_id, populate_existing=True)
        allocation = allocations.get(line.source)
        if not before or not after or not allocation or allocation["line_no"] != line.line_no:
            reject("remainder_link_corrupt")
        if any(r.organization_id != receipt.organization_id or r.source != line.source
               or r.sku_code != allocation["sku_code"] or r.warehouse != allocation["warehouse"] for r in (before, after)):
            reject("remainder_link_corrupt")
        if before.qty <= 0 or line.qty != before.qty or after.qty != 0 or after.version != before.version + 1:
            reject("remainder_quantity_corrupt")
        verified.append({"source": line.source, "line_no": line.line_no, "qty": format(line.qty, ".2f"),
                         "before_id": before.id, "after_id": after.id})
    expected = {v["source"] for v in snap["before"]["versions"] if Decimal(v["qty"]) > 0}
    if not verified or verified != snap.get("lines") or {v["source"] for v in verified} != expected:
        reject("remainder_package_incomplete")
    return deepcopy({"release_id": receipt.id, "digest": receipt.digest, "snapshot": snap})


async def basis(session, org, source):
    reservation, allocations = await _locked_reservation(session, org, source)
    latest, state, tasks, _, before = await _remaining(session, reservation, allocations)
    acts = (await session.scalars(select(PhysicalShipmentAct).where(
        PhysicalShipmentAct.document_id == source["document_id"]).order_by(PhysicalShipmentAct.id)
        .execution_options(populate_existing=True))).all()
    anchors = [{"id": a.id, "digest": (await act_result(session, a))["digest"]} for a in acts]
    if not anchors:
        reject("remainder_requires_physical_act")
    if not any(r.qty > 0 for r in latest):
        reject("remainder_already_exhausted")
    return reservation, allocations, latest, tasks, {"before": before, "acts": anchors}


async def preview(session, org, source):
    reservation, _, latest, _, evidence = await basis(session, org, source)
    return {"reservation_digest": reservation.digest, "basis_digest": _digest(evidence),
            "remaining": [{"source": r.source, "qty": format(r.qty, ".2f")} for r in latest],
            "acts": evidence["acts"]}


async def release(session, org, source, data, actor):
    data = ReleaseInput.model_validate(data)
    request_hash = _digest({"organization_id": org, "document_id": source["document_id"],
                            "data": data.model_dump(mode="json")})
    saved = await lookup(session, org, source["document_id"], data.source_key)
    if saved:
        if saved.request_hash != request_hash:
            reject("remainder_request_conflict")
        return await result(session, saved)
    if not source.get("fulfillment_allowed") or not actor or len(actor) > 200:
        reject("remainder_not_authorized")
    if source["version"] != data.expected_version or source["content_sha256"] != data.expected_content_sha256:
        reject("remainder_source_changed")
    reservation, allocations, latest, tasks, evidence = await basis(session, org, source)
    if _digest(evidence) != data.expected_basis_digest:
        reject("remainder_basis_changed")
    positive = {r.source: r for r in latest if r.qty > 0}
    lines = []
    for allocation in allocations:
        before = positive.get(allocation["source"])
        if before is None:
            continue
        after = ReservationVersion(organization_id=org, source=before.source, version=before.version + 1,
            sku_code=before.sku_code, warehouse=before.warehouse, qty=0, evidence=data.evidence, actor=actor)
        session.add(after)
        await session.flush()
        lines.append({"source": before.source, "line_no": allocation["line_no"], "qty": format(before.qty, ".2f"),
                      "before_id": before.id, "after_id": after.id})
    for task in tasks:
        if any(r.sku_code == task.sku_code and r.warehouse == task.warehouse for r in positive.values()):
            task.status = "canceled"
            task.done_at = None
    snapshot = {"organization_id": org, "document_id": source["document_id"], "source_key": str(data.source_key),
                "request_hash": request_hash, "actor": actor, "reservation_digest": reservation.digest,
                "evidence": data.evidence, **evidence, "lines": sorted(lines, key=lambda r: r["source"])}
    receipt = RemainderRelease(organization_id=org, document_id=source["document_id"], source_key=str(data.source_key),
        request_hash=request_hash, actor=actor, snapshot=snapshot, digest=_digest(snapshot))
    session.add(receipt)
    await session.flush()
    session.add_all([RemainderReleaseLine(release_id=receipt.id, **line) for line in lines])
    await session.flush()
    return await result(session, receipt)


router = APIRouter()


@router.post("/organizations/{org}/invoices/{document}/remainder-release/preview")
async def preview_route(org: int, document: int, data: ShipmentIdentity, session=Depends(get_session),
                        core=Depends(get_core), user=Depends(require_permission("wms.count"))):
    await core.services.accounting.source_member(session, org, user)
    source = await source_facts(session, core, org, document, data, "fulfill")
    return await preview(session, org, source)


@router.post("/organizations/{org}/invoices/{document}/remainder-release", status_code=201)
async def release_route(org: int, document: int, data: ReleaseInput, session=Depends(get_session),
                        core=Depends(get_core), user=Depends(require_permission("wms.count"))):
    actor = await core.services.accounting.source_member(session, org, user)
    existing = await lookup(session, org, document, data.source_key)
    source = await source_facts(session, core, org, document, data, "historical_claim" if existing else "fulfill")
    response = await release(session, org, source, data, actor)
    if not existing:
        core.event_bus.emit(session, "wms.invoice.remainder_released", {"organization_id": org, "document_id": document,
            "release_id": response["release_id"], "digest": response["digest"]})
    await session.commit()
    return response
