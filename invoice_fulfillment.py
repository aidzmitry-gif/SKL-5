"""Addressed WMS observations; never a complete no-shipment authorization.

Caller authorizes and holds org -> deal -> document locks until its commit.
Legacy coverage belongs to the encompassing Sales review, not this collector.
"""
from copy import deepcopy

from sqlalchemy import select

from modules.wms.invoice_reservations import (
    _digest,
    _locked_reservation,
    _remaining,
    release_preview,
)
from modules.wms.invoice_shipments import PhysicalShipmentAct, act_result, physical_basis
from modules.wms.models import ReservationVersion, StockMovement


async def invoice_fulfillment_snapshot(session, organization_id, source):
    """Return verified local observations or raise on unknown/corrupt history.

    Successful states are no_shipment/shipped, always with incomplete coverage.
    A caller must treat an exception as unresolved, never as an empty scope.
    This function neither commits nor creates evidence registry records.
    """
    reservation, allocations = await _locked_reservation(session, organization_id, source)
    *_, tasks, quantities, remaining = await _remaining(session, reservation, allocations)
    basis = await physical_basis(session, organization_id, reservation, allocations)
    acts = (await session.scalars(select(PhysicalShipmentAct).where(
        PhysicalShipmentAct.document_id == reservation.document_id,
    ).order_by(PhysicalShipmentAct.id).execution_options(populate_existing=True))).all()
    verified_acts = [await act_result(session, act) for act in acts]
    if not acts:
        await release_preview(session, organization_id, source)
    versions = (await session.scalars(select(ReservationVersion).where(
        ReservationVersion.source.in_([row["source"] for row in allocations]),
    ).order_by(ReservationVersion.source, ReservationVersion.version)
        .execution_options(populate_existing=True))).all()
    movement_ids = [line["movement_id"] for act in verified_acts for line in act["snapshot"]["lines"]]
    movements = (await session.scalars(select(StockMovement).where(
        StockMovement.id.in_(movement_ids),
    ).order_by(StockMovement.id).execution_options(populate_existing=True))).all()
    result = {
        "schema_version": 1,
        "identity": {"organization_id": organization_id, "document_id": reservation.document_id,
            "document_version": source["version"], "content_sha256": source["content_sha256"],
            "reservation_digest": reservation.digest},
        "observed_state": "shipped" if acts else "no_shipment",
        "coverage_complete": False,
        "snapshot": {
            "reservation": {"digest": reservation.digest, "original_snapshot": reservation.snapshot},
            "versions": [{"id": r.id, "source": r.source, "version": r.version,
                "organization_id": r.organization_id, "sku_code": r.sku_code,
                "warehouse": r.warehouse, "qty": format(r.qty, ".2f")} for r in versions],
            "event_state": remaining["event_state"],
            "picks": [{"id": t.id, "document_id": reservation.document_id,
                "organization_id": t.organization_id, "kind": t.kind, "status": t.status,
                "sku_code": t.sku_code, "warehouse": t.warehouse, "qty": format(t.qty, ".2f"),
                "doc_ref": t.doc_ref, "from_location_id": t.from_location_id,
                "to_location_id": t.to_location_id, "done_at": t.done_at.isoformat() if t.done_at else None}
                for t in tasks],
            "acts": verified_acts,
            "movements": [{"id": m.id, "organization_id": m.organization_id,
                "sku_code": m.sku_code, "warehouse": m.warehouse, "kind": m.kind,
                "reason": m.reason, "qty": format(m.qty, ".2f"), "doc_ref": m.doc_ref,
                "location_id": m.location_id, "batch_ref": m.batch_ref,
                "created_at": m.created_at.isoformat() if m.created_at else None} for m in movements],
            "remaining": remaining,
            "physical_basis": basis,
        },
    }
    # Detach nested ORM JSON so consumers cannot mutate persisted source evidence.
    result = deepcopy(result)
    return {**result, "digest": _digest(result)}
