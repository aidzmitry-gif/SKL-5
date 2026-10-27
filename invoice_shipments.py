"""Physical invoice consumption. Caller owns authorization, locks and commit.

An internal stock act is neither a TN/TTN nor a verified external signature.
"""
import hashlib
import json
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from html import escape
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
    event,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base
from core.runtime.core import Core
from core.runtime.deps import get_core, get_session
from core.services.auth import get_current_user, require_permission
from modules.wms.invoice_reservations import (
    InvoiceReservation,
    _digest,
    _locked_reservation,
    _qty,
    _remaining,
    _reserved_quantity,
    invoice_availability,
)
from modules.wms.models import ReservationVersion, StockMovement
from modules.wms.warehouse_modes import frozen_modes


class PhysicalShipmentAct(Base):
    __tablename__ = "physical_shipment_act"
    __table_args__ = (UniqueConstraint("organization_id", "source_key", name="physical_act_request"), {"schema": "wms"})
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column()
    document_id: Mapped[int] = mapped_column(ForeignKey("wms.invoice_reservation.document_id"), index=True)
    document_version: Mapped[int] = mapped_column()
    content_sha256: Mapped[str] = mapped_column(String(64))
    reservation_digest: Mapped[str] = mapped_column(String(64))
    source_key: Mapped[str] = mapped_column(String(36))
    request_hash: Mapped[str] = mapped_column(String(64))
    operation_date: Mapped[date] = mapped_column(Date)
    actor: Mapped[str] = mapped_column(String(200))
    snapshot: Mapped[dict] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(64))


class PhysicalShipmentLine(Base):
    __tablename__ = "physical_shipment_line"
    __table_args__ = (
        UniqueConstraint("act_id", "source", name="physical_line_source"),
        UniqueConstraint("before_id", name="physical_line_before"),
        UniqueConstraint("after_id", name="physical_line_after"),
        UniqueConstraint("movement_id", name="physical_line_movement"),
        CheckConstraint("qty > 0 AND qty < 1000000000000 AND line_no > 0", name="physical_line_quantity"),
        {"schema": "wms"},
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    act_id: Mapped[int] = mapped_column(ForeignKey("wms.physical_shipment_act.id"))
    source: Mapped[str] = mapped_column(String(128))
    line_no: Mapped[int] = mapped_column()
    sku_code: Mapped[str] = mapped_column(String(64))
    warehouse: Mapped[str] = mapped_column(String(128))
    qty: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    before_id: Mapped[int] = mapped_column(ForeignKey("wms.reservation_version.id"))
    after_id: Mapped[int] = mapped_column(ForeignKey("wms.reservation_version.id"))
    movement_id: Mapped[int] = mapped_column(ForeignKey("wms.stock_movement.id"))


def immutable(*args):
    raise ValueError("Physical shipment evidence is immutable")


for model in (PhysicalShipmentAct, PhysicalShipmentLine):
    event.listen(model, "before_update", immutable)
    event.listen(model, "before_delete", immutable)


class ShipmentIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(gt=0, strict=True)
    expected_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ShipmentHistoryInput(ShipmentIdentity):
    after_id: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=25, ge=1, le=100, strict=True)


class ShipmentQuantity(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    line_no: int = Field(gt=0, strict=True)
    warehouse: str = Field(min_length=1, max_length=128)
    qty: str

    @field_validator("qty")
    @classmethod
    def quantity(cls, value):
        result = _qty(value)
        if result >= Decimal("1000000000000"):
            raise ValueError("Quantity outside range")
        return format(result, ".2f")


class ShipmentInput(ShipmentIdentity):
    source_key: UUID
    expected_reservation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_remaining_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_physical_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation_date: date
    evidence: str = Field(min_length=1, max_length=1000)
    lines: list[ShipmentQuantity] = Field(min_length=1, max_length=1000)


def reject(message):
    raise HTTPException(409, message)


def _act_execution_mode(act):
    snapshot = act.snapshot if isinstance(act.snapshot, dict) else {}
    mode = snapshot.get("execution_mode", "wms")
    if not isinstance(mode, str) or mode not in {"document", "wms"}:
        reject("physical_act_execution_mode_corrupt")
    return mode


def _batch_execution_mode(reservation, allocations, data):
    try:
        modes = frozen_modes(reservation.execution_modes, {row["warehouse"] for row in allocations})
    except ValueError:
        reject("reservation_execution_mode_corrupt")
    available = {(row["line_no"], row["warehouse"]): row for row in allocations}
    selected = set()
    seen = set()
    for item in data.lines:
        key = (item.line_no, item.warehouse)
        if key in seen or key not in available:
            reject("physical_line_unknown_or_duplicate")
        seen.add(key)
        selected.add(modes[item.warehouse])
    if len(selected) != 1:
        reject("shipment_batch_mixes_execution_modes")
    return "wms" if selected == {True} else "document"


async def no_physical_act(session, document_id):
    if await session.scalar(select(PhysicalShipmentAct.id).where(PhysicalShipmentAct.document_id == document_id).limit(1)):
        reject("physical_act_excludes_no_shipment_release")


async def act_result(session, act):
    """Revalidate original links, not the mutable *latest* balance, for replay."""
    snap = act.snapshot
    if not isinstance(snap, dict) or _digest(snap) != act.digest:
        reject("physical_act_digest_corrupt")
    for key in ("organization_id", "document_id", "document_version", "content_sha256", "reservation_digest", "source_key", "request_hash", "actor"):
        if snap.get(key) != getattr(act, key):
            reject("physical_act_identity_corrupt")
    if snap.get("operation_date") != act.operation_date.isoformat():
        reject("physical_act_date_corrupt")
    original = await session.get(InvoiceReservation, act.document_id, populate_existing=True)
    if original is None or original.organization_id != act.organization_id or original.digest != act.reservation_digest:
        reject("physical_act_reservation_corrupt")
    if original.snapshot.get("version") != act.document_version or original.snapshot.get("content_sha256") != act.content_sha256:
        reject("physical_act_original_changed")
    try:
        basis = {key: original.snapshot[key] for key in ("document_id", "organization_id", "version", "content_sha256", "allocations", "evidence")}
        fingerprint = hashlib.sha256(json.dumps(basis, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if fingerprint != original.digest:
            reject("physical_act_reservation_digest_corrupt")
        allocations = {a["source"]: a for a in original.snapshot["allocations"]}
        modes = frozen_modes(original.execution_modes, {a["warehouse"] for a in allocations.values()})
    except (KeyError, TypeError, ValueError):
        reject("physical_act_reservation_corrupt")
    execution_mode = _act_execution_mode(act)
    if "execution_mode" not in snap and original.execution_modes is not None:
        reject("physical_act_execution_mode_missing")
    lines = (await session.scalars(select(PhysicalShipmentLine).where(PhysicalShipmentLine.act_id == act.id)
        .order_by(PhysicalShipmentLine.source).execution_options(populate_existing=True))).all()
    if not lines or len(lines) != len(snap.get("lines", [])):
        reject("physical_act_lines_corrupt")
    for line, evidence in zip(lines, snap["lines"], strict=True):
        allocation = allocations.get(line.source)
        if not allocation or any(allocation[k] != getattr(line, k) for k in ("line_no", "sku_code", "warehouse")):
            reject("physical_line_allocation_corrupt")
        if (line.warehouse not in modes
                or ("wms" if modes[line.warehouse] else "document") != execution_mode):
            reject("physical_line_execution_mode_mismatch")
        for key in ("source", "line_no", "sku_code", "warehouse", "before_id", "after_id", "movement_id"):
            if evidence.get(key) != getattr(line, key):
                reject("physical_line_snapshot_corrupt")
        if evidence.get("qty") != format(_qty(line.qty), ".2f"):
            reject("physical_line_quantity_corrupt")
        before = await session.get(ReservationVersion, line.before_id, populate_existing=True)
        after = await session.get(ReservationVersion, line.after_id, populate_existing=True)
        movement = await session.get(StockMovement, line.movement_id, populate_existing=True)
        if before is None or after is None or movement is None:
            reject("physical_line_missing_link")
        if any(r.organization_id != act.organization_id or r.source != line.source or r.sku_code != line.sku_code
               or r.warehouse != line.warehouse for r in (before, after)):
            reject("physical_line_foreign_link")
        if after.version != before.version + 1 or _reserved_quantity(before.qty) - line.qty != _reserved_quantity(after.qty):
            reject("physical_line_bad_reduction")
        if (movement.organization_id != act.organization_id or movement.sku_code != line.sku_code
                or movement.warehouse != line.warehouse or movement.kind != "out" or movement.reason != "shipment"
                or movement.qty != line.qty or movement.doc_ref != f"physical-act:{act.id}"):
            reject("physical_movement_corrupt")
    return {"act_id": act.id, "source_key": act.source_key, "request_hash": act.request_hash,
            "digest": act.digest, "execution_mode": execution_mode, "snapshot": snap}


async def proved_chain(session, reservation, allocations, chains):
    from modules.wms.invoice_remainder import RemainderRelease
    from modules.wms.invoice_remainder import result as remainder_result
    acts = (await session.scalars(select(PhysicalShipmentAct).where(
        PhysicalShipmentAct.document_id == reservation.document_id).order_by(PhysicalShipmentAct.id)
        .execution_options(populate_existing=True))).all()
    links = {}
    for act in acts:
        result = await act_result(session, act)
        for row in result["snapshot"]["lines"]:
            if row["after_id"] in links:
                reject("physical_chain_duplicate")
            links[row["after_id"]] = row
    releases = (await session.scalars(select(RemainderRelease).where(
        RemainderRelease.document_id == reservation.document_id))).all()
    for receipt in releases:
        for row in (await remainder_result(session, receipt))["snapshot"]["lines"]:
            if row["after_id"] in links:
                reject("physical_chain_duplicate")
            links[row["after_id"]] = row
    used, latest = set(), []
    for allocation in allocations:
        chain = chains[allocation["source"]]
        if not chain or chain[0].qty != _qty(allocation["qty"]):
            reject("unexplained_reservation_history")
        if any(r.organization_id != reservation.organization_id or r.source != allocation["source"]
               or r.sku_code != allocation["sku_code"] or r.warehouse != allocation["warehouse"] or r.version != n
               for n, r in enumerate(chain, 1)):
            reject("physical_chain_identity_corrupt")
        for before, after in zip(chain, chain[1:]):
            link = links.get(after.id)
            if not link or link["before_id"] != before.id or link["source"] != after.source:
                reject("unexplained_reservation_history")
            used.add(after.id)
        latest.append(chain[-1])
    if used != set(links):
        reject("physical_chain_orphan")
    return latest


async def physical_basis(session, org, reservation, allocations):
    """Require persisted reservation journal evidence; never accept client proof."""
    from modules.wms.routes import _physical_inventory_snapshot

    saved = reservation.snapshot.get("physical_basis")
    if not reservation.snapshot.get("evidence") or not isinstance(saved, dict):
        reject("persisted_physical_basis_missing")
    basis = {}
    for warehouse in sorted({r["warehouse"] for r in allocations}):
        recorded = saved.get(warehouse)
        if not isinstance(recorded, dict) or type(recorded.get("cutoff")) is not int or not isinstance(recorded.get("version"), str):
            reject("persisted_physical_basis_missing")
        historic = (await session.scalars(select(StockMovement).where(StockMovement.organization_id == org,
            StockMovement.warehouse == warehouse, StockMovement.id <= recorded["cutoff"],
            StockMovement.reason.not_in(["reserve", "release"])).order_by(StockMovement.id)
            .execution_options(populate_existing=True))).all()
        identity = [[r.id, r.sku_code, r.kind, format(r.qty, ".2f"), r.reason] for r in historic]
        encoded = json.dumps([org, warehouse, identity], ensure_ascii=False, separators=(",", ":"))
        if not historic or historic[-1].id != recorded["cutoff"] or hashlib.sha256(encoded.encode()).hexdigest() != recorded["version"]:
            reject("persisted_physical_basis_changed")
        qty, version, cutoff = await _physical_inventory_snapshot(session, org, warehouse, allow_negative=True)
        basis[warehouse] = {"quantities": {k: format(v, ".2f") for k, v in sorted(qty.items())},
                            "version": version, "cutoff": cutoff}
    return basis


async def preview(session, org, source):
    from modules.wms.invoice_remainder import RemainderRelease
    from modules.wms.invoice_remainder import result as remainder_result
    reservation, allocations = await _locked_reservation(session, org, source)
    latest, *_, before = await _remaining(session, reservation, allocations)
    physical = await physical_basis(session, org, reservation, allocations)
    availability = await invoice_availability(session, org, sorted({row["sku_code"] for row in allocations}))
    balances = {(row["sku_code"], row["warehouse"]): row for row in availability["rows"]}
    versions = {row.source: row for row in latest}
    receipt = await session.scalar(select(RemainderRelease).where(RemainderRelease.document_id == source["document_id"]))
    released = set()
    if receipt:
        released = {row["source"] for row in (await remainder_result(session, receipt))["snapshot"]["lines"]}
    lines = []
    from modules.wms.stock_policy import policies, shortage
    policy = await policies(session, org, {row["warehouse"] for row in allocations})
    own = defaultdict(lambda: Decimal("0"))
    for allocation in allocations:
        own[(allocation["sku_code"], allocation["warehouse"])] += versions[allocation["source"]].qty
    modes = frozen_modes(reservation.execution_modes, {row["warehouse"] for row in allocations})
    for allocation in sorted(allocations, key=lambda row: (row["line_no"], row["warehouse"])):
        balance = balances.get((allocation["sku_code"], allocation["warehouse"]), {})
        remaining = versions[allocation["source"]].qty
        group_remaining = own[(allocation["sku_code"], allocation["warehouse"])]
        issue = shortage(allocation["sku_code"], allocation["warehouse"], group_remaining,
                         balance.get("physical"), balance.get("reserved", "0"),
                         own_reserved=own[(allocation["sku_code"], allocation["warehouse"])])
        blocked_stock = issue and (policy[allocation["warehouse"]] or balance.get("physical") is None
                                   or issue["reason"] == "reserved_by_other_documents")
        blocker = ("remainder_released" if allocation["source"] in released else
                   "fully_shipped" if remaining == 0 else "stock_shortage" if blocked_stock else None)
        lines.append({"line_no": allocation["line_no"], "sku_code": allocation["sku_code"],
                      "warehouse": allocation["warehouse"], "original_qty": format(_qty(allocation["qty"]), ".2f"),
                      "remaining_qty": format(remaining, ".2f"),
                      "execution_mode": "wms" if modes[allocation["warehouse"]] else "document",
                      "physical": balance.get("physical"), "reserved": balance.get("reserved"),
                      "free": balance.get("free"), "blocking_reason": blocker,
                      "stock_issue": issue if blocker == "stock_shortage" else None,
                      "prohibit_negative_stock": policy[allocation["warehouse"]]})
    return {"reservation_digest": reservation.digest, "remaining_digest": _digest(before),
            "physical_digest": _digest(physical), "remaining": before, "physical": physical,
            "identity": {"organization_id": org, "document_id": source["document_id"],
                         "document_version": source["version"], "content_sha256": source["content_sha256"]},
            "lines": lines}


async def lookup(session, org, document_id, key):
    act = await session.scalar(select(PhysicalShipmentAct).where(PhysicalShipmentAct.organization_id == org,
        PhysicalShipmentAct.source_key == str(key)).execution_options(populate_existing=True))
    if act is not None and act.document_id != document_id:
        reject("physical_request_conflict")
    return act


async def create(session, org, source, data, actor, execution_mode="wms"):
    """Internal authorized facade. Fresh source_changed is caller's responsibility."""
    if execution_mode not in {"wms", "document"}:
        reject("physical_execution_mode_invalid")
    data = ShipmentInput.model_validate(data)
    request_hash = _digest({"organization_id": org, "document_id": source["document_id"], "data": data.model_dump(mode="json")})
    existing = await lookup(session, org, source["document_id"], data.source_key)
    if existing:
        if _act_execution_mode(existing) != execution_mode:
            reject("physical_request_endpoint_origin_conflict")
        if existing.request_hash != request_hash:
            reject("physical_request_conflict")
        return await act_result(session, existing)
    if not source.get("fulfillment_allowed") or source["version"] != data.expected_version or source["content_sha256"] != data.expected_content_sha256:
        reject("physical_fulfillment_not_allowed")
    if not actor or len(actor) > 200 or data.operation_date > date.today():
        reject("physical_actor_or_date_invalid")
    reservation, allocations = await _locked_reservation(session, org, source)
    if _batch_execution_mode(reservation, allocations, data) != execution_mode:
        reject("shipment_batch_wrong_execution_mode")
    latest, state, tasks, _, before = await _remaining(session, reservation, allocations)
    physical = await physical_basis(session, org, reservation, allocations)
    if (data.expected_reservation_digest != reservation.digest or data.expected_remaining_digest != _digest(before)
            or data.expected_physical_digest != _digest(physical)):
        reject("physical_basis_changed")
    available = {(a["line_no"], a["warehouse"]): (a, r) for a, r in zip(allocations, latest, strict=True)}
    chosen, seen, totals = [], set(), defaultdict(lambda: Decimal("0"))
    for item in data.lines:
        key = (item.line_no, item.warehouse)
        if key in seen or key not in available:
            reject("physical_line_unknown_or_duplicate")
        seen.add(key)
        allocation, prior = available[key]
        qty = _qty(item.qty)
        if qty > prior.qty:
            reject("physical_quantity_exceeds_reserve")
        chosen.append((allocation, prior, qty))
        totals[(allocation["sku_code"], item.warehouse)] += qty
    availability = await invoice_availability(session, org, [r[0]["sku_code"] for r in chosen])
    from modules.wms.stock_policy import check, policies
    policy = await policies(session, org, {key[1] for key in totals})
    balances = {(r["sku_code"], r["warehouse"]): r for r in availability["rows"]}
    own = defaultdict(lambda: Decimal("0"))
    for allocation, remaining in zip(allocations, latest, strict=True):
        own[(allocation["sku_code"], allocation["warehouse"])] += remaining.qty
    for (sku, warehouse), qty in totals.items():
        row = balances.get((sku, warehouse), {})
        check(sku, warehouse, qty, row.get("physical"), row.get("reserved", "0"),
              policy[warehouse], own_reserved=own[(sku, warehouse)])
    act = PhysicalShipmentAct(organization_id=org, document_id=source["document_id"], document_version=source["version"],
        content_sha256=source["content_sha256"], reservation_digest=reservation.digest, source_key=str(data.source_key),
        request_hash=request_hash, operation_date=data.operation_date, actor=actor)
    # Insert a complete immutable header once; allocate IDs for linked rows first.
    rows = []
    for allocation, prior, qty in sorted(chosen, key=lambda x: x[0]["source"]):
        after = ReservationVersion(organization_id=org, source=prior.source, version=prior.version + 1,
            sku_code=prior.sku_code, warehouse=prior.warehouse, qty=prior.qty-qty, evidence=data.evidence, actor=actor)
        movement = StockMovement(organization_id=org, sku_code=prior.sku_code, warehouse=prior.warehouse,
            kind="out", qty=qty, reason="shipment", doc_ref="")
        session.add_all([after, movement])
        await session.flush()
        rows.append(dict(source=prior.source, line_no=allocation["line_no"], sku_code=prior.sku_code,
            warehouse=prior.warehouse, qty=format(qty, ".2f"), before_id=prior.id, after_id=after.id, movement_id=movement.id))
    act.snapshot = {k: getattr(act, k) for k in ("organization_id", "document_id", "document_version", "content_sha256",
        "reservation_digest", "source_key", "request_hash", "actor")}
    act.snapshot.update(operation_date=data.operation_date.isoformat(), evidence=data.evidence, kind="internal_physical_shipment",
        execution_mode=execution_mode, before=before, physical=physical, lines=rows)
    act.digest = _digest(act.snapshot)
    session.add(act)
    await session.flush()
    for row in rows:
        movement = await session.get(StockMovement, row["movement_id"])
        movement.doc_ref = f"physical-act:{act.id}"
        await session.flush([movement])
        session.add(PhysicalShipmentLine(act_id=act.id, **row))
    await session.flush()
    # Original event quantities/pick quantities remain identity, not balances.
    updated = await proved_chain(session, reservation, allocations, await version_chains(session, allocations))
    balances = defaultdict(lambda: Decimal("0"))
    for row in updated:
        balances[(row.sku_code, row.warehouse)] += row.qty
    for task in tasks:
        if balances[(task.sku_code, task.warehouse)] == 0:
            task.status, task.done_at = "done", datetime.now(timezone.utc).replace(tzinfo=None)
    await session.flush()
    result = await act_result(session, act)
    return result


async def version_chains(session, allocations):
    rows = (await session.scalars(select(ReservationVersion).where(ReservationVersion.source.in_([a["source"] for a in allocations]))
        .order_by(ReservationVersion.source, ReservationVersion.version).execution_options(populate_existing=True))).all()
    chains = defaultdict(list)
    for row in rows:
        chains[row.source].append(row)
    return chains


async def guard_free(session, org, decreases, doc_refs=()):
    """Whole-operation NET decreases only, under the caller's organization lock."""
    if any(ref and (ref.startswith(("invoice:", "sales:document:", "physical-act:", "PICK-"))) for ref in doc_refs):
        reject("invoice_stock_requires_physical_workflow")
    needed = {key: qty for key, qty in decreases.items() if qty > 0}
    if not needed:
        return
    result = await invoice_availability(session, org, [key[0] for key in needed])
    rows = {(r["sku_code"], r["warehouse"]): r for r in result["rows"]}
    from modules.wms.stock_policy import check, policies
    policy = await policies(session, org, {key[1] for key in needed})
    for key, qty in needed.items():
        row = rows.get(key, {})
        check(key[0], key[1], _qty(qty), row.get("physical"), row.get("reserved", "0"), policy[key[1]])


router = APIRouter()


async def source_facts(session, core, org, document, identity, operation):
    try:
        return await core.services.sales_source.invoice_shipping_source(session, document, organization_id=org,
            expected_version=identity.expected_version, expected_content_sha256=identity.expected_content_sha256, operation=operation)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/organizations/{org}/invoices/{document}/physical-shipments/preview")
async def preview_route(org: int, document: int, data: ShipmentIdentity, session=Depends(get_session),
                        core: Core = Depends(get_core), user=Depends(require_permission("wms.count"))):
    await core.services.accounting.source_member(session, org, user)
    facts = await source_facts(session, core, org, document, data, "fulfill")
    return await preview(session, org, facts)


async def _document_shipment_member(core, session, org, user):
    accounting = getattr(core.services, "accounting", None)
    authorize = getattr(accounting, "document_shipment_member", None)
    if not callable(authorize):
        raise HTTPException(503, "Accounting document shipment access is unavailable")
    return await authorize(session, org, user)


@router.post("/organizations/{org}/invoices/{document}/document-shipments/preview")
async def document_preview_route(org: int, document: int, data: ShipmentIdentity,
                                 session=Depends(get_session), core: Core = Depends(get_core),
                                 user=Depends(get_current_user)):
    await _document_shipment_member(core, session, org, user)
    facts = await source_facts(session, core, org, document, data, "fulfill")
    return await preview(session, org, facts)


@router.post("/organizations/{org}/invoices/{document}/physical-shipments", status_code=201)
async def create_route(org: int, document: int, data: ShipmentInput, session=Depends(get_session),
                       core: Core = Depends(get_core), user=Depends(require_permission("wms.count"))):
    actor = await core.services.accounting.source_member(session, org, user)
    existing = await lookup(session, org, document, data.source_key)
    facts = await source_facts(session, core, org, document, data, "historical_claim" if existing else "fulfill")
    if not existing:
        await core.services.accounting.source_changed(session, org, user,
            f"wms:physical-shipment:{org}:{data.source_key}", 1, data.operation_date.isoformat())
    result = await create(session, org, facts, data, actor, execution_mode="wms")
    if not existing:
        core.event_bus.emit(session, "wms.invoice.physical_shipped", {"organization_id": org, "document_id": document,
            "act_id": result["act_id"], "digest": result["digest"], "source_key": result["source_key"]})
    await session.commit()
    return result


@router.post("/organizations/{org}/invoices/{document}/document-shipments", status_code=201)
async def document_create_route(org: int, document: int, data: ShipmentInput,
                                session=Depends(get_session), core: Core = Depends(get_core),
                                user=Depends(get_current_user)):
    actor = await _document_shipment_member(core, session, org, user)
    existing = await lookup(session, org, document, data.source_key)
    facts = await source_facts(session, core, org, document, data,
                               "historical_claim" if existing else "fulfill")
    if not existing:
        await core.services.accounting.source_changed(session, org, user,
            f"wms:physical-shipment:{org}:{data.source_key}", 1, data.operation_date.isoformat())
    result = await create(session, org, facts, data, actor, execution_mode="document")
    if not existing:
        core.event_bus.emit(session, "wms.invoice.physical_shipped", {"organization_id": org,
            "document_id": document, "act_id": result["act_id"], "digest": result["digest"],
            "source_key": result["source_key"]})
    await session.commit()
    return result


@router.get("/organizations/{org}/invoices/{document}/document-shipments/by-key/{key}")
async def document_read_route(org: int, document: int, key: UUID, session=Depends(get_session),
                              core: Core = Depends(get_core), user=Depends(get_current_user)):
    try:
        await _document_shipment_member(core, session, org, user)
        act = await lookup(session, org, document, key)
        if act is None or _act_execution_mode(act) != "document":
            raise HTTPException(404, "Document shipment not found")
        identity = ShipmentIdentity(expected_version=act.document_version,
                                    expected_content_sha256=act.content_sha256)
        await source_facts(session, core, org, document, identity, "historical_claim")
        return await act_result(session, act)
    finally:
        await session.rollback()


@router.post("/organizations/{org}/invoices/{document}/document-shipments/history")
async def document_history_route(org: int, document: int, data: ShipmentHistoryInput,
                                 session=Depends(get_session), core: Core = Depends(get_core),
                                 user=Depends(get_current_user)):
    try:
        await _document_shipment_member(core, session, org, user)
        await source_facts(session, core, org, document, data, "historical_claim")
        rows = (await session.scalars(select(PhysicalShipmentAct).where(
            PhysicalShipmentAct.organization_id == org,
            PhysicalShipmentAct.document_id == document,
            PhysicalShipmentAct.id > data.after_id,
            PhysicalShipmentAct.snapshot["execution_mode"].as_string() == "document",
        ).order_by(PhysicalShipmentAct.id).limit(data.limit + 1)
            .execution_options(populate_existing=True))).all()
        items = []
        for row in rows[:data.limit]:
            if row.document_version != data.expected_version or row.content_sha256 != data.expected_content_sha256:
                reject("physical_act_original_changed")
            items.append(await act_result(session, row))
        return {"identity": {"organization_id": org, "document_id": document,
                             "document_version": data.expected_version,
                             "content_sha256": data.expected_content_sha256},
                "items": items,
                "next_after_id": rows[data.limit - 1].id if len(rows) > data.limit else None}
    finally:
        await session.rollback()


@router.get("/organizations/{org}/invoices/{document}/physical-shipments/by-key/{key}")
async def read_route(org: int, document: int, key: UUID, session=Depends(get_session),
                     core: Core = Depends(get_core), user=Depends(require_permission("wms.read"))):
    await core.services.accounting.source_member(session, org, user)
    act = await lookup(session, org, document, key)
    if act is None:
        raise HTTPException(404, "Physical shipment not found")
    identity = ShipmentIdentity(expected_version=act.document_version, expected_content_sha256=act.content_sha256)
    await source_facts(session, core, org, document, identity, "historical_claim")
    return await act_result(session, act)


@router.get("/organizations/{org}/physical-shipments/by-key/{key}")
async def source_snapshot_route(org: int, key: UUID, response: Response, session=Depends(get_session),
                                core: Core = Depends(get_core), user=Depends(require_permission("wms.read"))):
    """Load trusted input for the accountant's mapping form from a source reference."""
    gateway = getattr(core.services, "accounting", None)
    if gateway is None or not callable(getattr(gateway, "source_member", None)):
        raise HTTPException(503, "Organization service is unavailable")
    try:
        await gateway.source_member(session, org, user)
        act = await session.scalar(select(PhysicalShipmentAct).where(
            PhysicalShipmentAct.organization_id == org, PhysicalShipmentAct.source_key == str(key),
        ).execution_options(populate_existing=True))
        if act is None:
            raise HTTPException(404, "Physical shipment not found")
        identity = ShipmentIdentity(expected_version=act.document_version, expected_content_sha256=act.content_sha256)
        await source_facts(session, core, org, act.document_id, identity, "historical_claim")
        result = await act_result(session, act)
        response.headers["Cache-Control"] = "private, no-store"
        return result
    finally:
        await session.rollback()


@router.post("/organizations/{org}/physical-shipments/by-key/{key}/accounting-preview")
async def accounting_preview_route(org: int, key: UUID, data: dict, session=Depends(get_session),
                                   core: Core = Depends(get_core), user=Depends(require_permission("wms.read"))):
    gateway = getattr(core.services, "accounting", None)
    if gateway is None or not callable(getattr(gateway, "physical_shipment_preview", None)):
        raise HTTPException(503, "Shipment accounting service is unavailable")
    try:
        await gateway.source_member(session, org, user)
        act = await session.scalar(select(PhysicalShipmentAct).where(
            PhysicalShipmentAct.organization_id == org, PhysicalShipmentAct.source_key == str(key),
        ).execution_options(populate_existing=True))
        if act is None:
            raise HTTPException(404, "Physical shipment not found")
        identity = ShipmentIdentity(expected_version=act.document_version, expected_content_sha256=act.content_sha256)
        await source_facts(session, core, org, act.document_id, identity, "historical_claim")
        receipt = await act_result(session, act)
        return await gateway.physical_shipment_preview(session, org, user, receipt, data)
    finally:
        await session.rollback()


def render_source_document(result):
    snap = result["snapshot"]
    def safe(value):
        return escape(str(value), quote=True)
    rows = "".join("<tr>" + "".join(f"<td>{safe(line[field])}</td>" for field in
                   ("line_no", "sku_code", "warehouse", "qty")) + "</tr>" for line in snap["lines"])
    html = f'''<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Внутренний акт № {result["act_id"]}</title>
<style>body{{font:16px system-ui;margin:24px;max-width:1000px;overflow-wrap:anywhere}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #aaa;padding:8px;text-align:left}}p{{white-space:pre-wrap}}</style>
<h1>Внутренний акт отгрузки № {result["act_id"]}</h1>
<p>Внутренний документ WMS. Не является ТН или ТТН.</p>
<p>Юрлицо № {safe(snap["organization_id"])} · Счёт № {safe(snap["document_id"])} · Версия {safe(snap["document_version"])}</p>
<p>Дата отгрузки: {safe(snap["operation_date"])}<br>Автор: {safe(snap["actor"])}</p>
<p>Основание: {safe(snap["evidence"])}</p>
<table><thead><tr><th>Строка счёта</th><th>Номенклатура</th><th>Склад</th><th>Количество</th></tr></thead><tbody>{rows}</tbody></table>
</html>'''
    return html


@router.get("/organizations/{org}/physical-shipments/by-key/{key}/document", response_class=HTMLResponse)
async def source_document_route(org: int, key: UUID, session=Depends(get_session),
                                core: Core = Depends(get_core), user=Depends(require_permission("wms.read"))):
    """Read an existing verified internal act from its accounting source key."""
    gateway = getattr(core.services, "accounting", None)
    if gateway is None or not callable(getattr(gateway, "source_member", None)):
        raise HTTPException(503, "Organization service is unavailable")
    try:
        await gateway.source_member(session, org, user)
        act = await session.scalar(select(PhysicalShipmentAct).where(
            PhysicalShipmentAct.organization_id == org, PhysicalShipmentAct.source_key == str(key),
        ).execution_options(populate_existing=True))
        if act is None:
            raise HTTPException(404, "Physical shipment not found")
        identity = ShipmentIdentity(expected_version=act.document_version, expected_content_sha256=act.content_sha256)
        await source_facts(session, core, org, act.document_id, identity, "historical_claim")
        result = await act_result(session, act)
        html = render_source_document(result)
        return HTMLResponse(html, headers={"Cache-Control": "private, no-store",
                                          "X-Content-Type-Options": "nosniff",
                                          "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"})
    finally:
        await session.rollback()


@router.post("/organizations/{org}/invoices/{document}/physical-shipments/history")
async def history_route(org: int, document: int, data: ShipmentHistoryInput, session=Depends(get_session),
                        core: Core = Depends(get_core), user=Depends(require_permission("wms.read"))):
    await core.services.accounting.source_member(session, org, user)
    await source_facts(session, core, org, document, data, "historical_claim")
    rows = (await session.scalars(select(PhysicalShipmentAct).where(
        PhysicalShipmentAct.organization_id == org, PhysicalShipmentAct.document_id == document,
        PhysicalShipmentAct.id > data.after_id,
    ).order_by(PhysicalShipmentAct.id).limit(data.limit + 1)
        .execution_options(populate_existing=True))).all()
    items = []
    for row in rows[:data.limit]:
        if row.document_version != data.expected_version or row.content_sha256 != data.expected_content_sha256:
            reject("physical_act_original_changed")
        items.append(await act_result(session, row))
    return {"identity": {"organization_id": org, "document_id": document,
                         "document_version": data.expected_version, "content_sha256": data.expected_content_sha256},
            "items": items, "next_after_id": rows[data.limit - 1].id if len(rows) > data.limit else None}
