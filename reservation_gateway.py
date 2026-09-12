"""WMS-owned implementation of the core invoice reservation facade."""
from modules.wms.invoice_reservations import (
    ReleaseInput,
    ReserveInput,
    invoice_availability,
    release,
    release_preview,
    reserve,
)


class WmsReservationService:
    async def invoice_remainder_preview(self, session, organization_id, source):
        from modules.wms.invoice_remainder import preview
        return await preview(session, organization_id, source)

    async def release_invoice_remainder(self, session, organization_id, source, request, actor):
        from modules.wms.invoice_remainder import release
        return await release(session, organization_id, source, request, actor)

    async def production_receipt_status(self, session, organization_id, event_id, source):
        from decimal import Decimal

        from sqlalchemy import select

        from modules.wms.models import Receipt, ReceiptLine, StockMovement
        from modules.wms.production_arrivals import ProductionArrival

        receipt = await session.scalar(select(Receipt).where(Receipt.source_event_id == event_id)
            .with_for_update().execution_options(populate_existing=True))
        if receipt is None:
            return {"state": "awaiting_delivery", "receipt_id": None, "accepted_quantity": None, "rejected_quantity": None}
        if (receipt.organization_id != organization_id or receipt.source != "production"
                or receipt.entity_ref != source["entity_ref"] or receipt.warehouse != source["warehouse"]):
            raise ValueError("Production receipt identity differs from the confirmed source")
        lines = (await session.scalars(select(ReceiptLine).where(ReceiptLine.receipt_id == receipt.id)
            .execution_options(populate_existing=True))).all()
        if len(lines) != 1:
            raise ValueError("Production receipt line set differs from its source")
        line = lines[0]
        if (line.sku_code != source["sku_code"] or line.expected_qty != Decimal(source["qty"])
                or line.location_id != source["location_id"] or line.batch_ref != source["lot"]):
            raise ValueError("Production receipt quantities or dimensions differ from its source")
        accepted = rejected = None
        movements = (await session.scalars(select(StockMovement).where(
            StockMovement.organization_id == organization_id, StockMovement.doc_ref == receipt.number,
            StockMovement.reason == "receipt").order_by(StockMovement.id))).all()
        if receipt.status == "accepted":
            if (line.accepted_qty is None or line.rejected_qty is None or line.accepted_qty < 0
                    or line.rejected_qty < 0 or line.accepted_qty + line.rejected_qty != line.expected_qty):
                raise ValueError("Accepted production receipt has inconsistent QC quantities")
            accepted, rejected = format(line.accepted_qty, ".2f"), format(line.rejected_qty, ".2f")
            expected = [] if line.accepted_qty == 0 else [(line.sku_code, receipt.warehouse, "in",
                line.accepted_qty, line.location_id, line.batch_ref)]
            actual = [(m.sku_code, m.warehouse, m.kind, m.qty, m.location_id, m.batch_ref) for m in movements]
            if actual != expected:
                raise ValueError("Production receipt arrival movements differ from accepted QC")
            binding = await session.get(ProductionArrival, receipt.id)
            if (binding is None or binding.line_id != line.id or binding.event_id != event_id
                    or binding.organization_id != organization_id
                    or binding.accepted_quantity != line.accepted_qty or binding.rejected_quantity != line.rejected_qty
                    or binding.movement_id != (movements[0].id if movements else None)):
                raise ValueError("Production arrival binding does not match receipt and movement")
        elif receipt.status != "pending_qc":
            raise ValueError("Production receipt status requires reconciliation")
        elif movements:
            raise ValueError("Pending production receipt already has arrival movements")
        return {"state": receipt.status, "receipt_id": receipt.id, "accepted_quantity": accepted,
            "rejected_quantity": rejected, "movement_ids": [m.id for m in movements]}

    async def receiving_places(self, session, query):
        from sqlalchemy import or_, select

        from modules.wms.models import Location

        rows = (await session.scalars(select(Location).where(Location.is_active.is_(True), or_(
            Location.code.icontains(query, autoescape=True), Location.warehouse.icontains(query, autoescape=True),
            Location.title.icontains(query, autoescape=True)))
            .order_by(Location.warehouse, Location.code, Location.id).limit(50))).all()
        return [{"location_id": row.id, "warehouse": row.warehouse, "code": row.code,
            "zone": row.zone, "title": row.title} for row in rows]

    async def receiving_place(self, session, warehouse, location_id):
        from fastapi import HTTPException
        from sqlalchemy import select

        from modules.wms.models import Location

        location = await session.scalar(select(Location).where(Location.id == location_id)
            .with_for_update().execution_options(populate_existing=True))
        if location is None or not location.is_active or location.warehouse != warehouse:
            raise HTTPException(409, "Receiving location must be active and match the warehouse")
        return {"location_id": location.id, "warehouse": location.warehouse,
            "code": location.code, "zone": location.zone, "title": location.title}

    async def receipt_reconciliation(self, session, organization_id, source):
        from modules.wms.primary_receipt_reconciliation import reconcile

        return await reconcile(session, organization_id, source)

    async def invoice_fulfillment_snapshot(self, session, organization_id, source):
        from modules.wms.invoice_fulfillment import invoice_fulfillment_snapshot

        return await invoice_fulfillment_snapshot(session, organization_id, source)

    async def invoice_availability(self, session, organization_id, sku_codes):
        return await invoice_availability(session, organization_id, sku_codes)

    async def reserve_invoice(self, session, organization_id, source, request, actor):
        return await reserve(session, organization_id, source,
                             ReserveInput.model_validate(request), actor)

    async def invoice_release_preview(self, session, organization_id, source):
        return await release_preview(session, organization_id, source)

    async def release_invoice(self, session, organization_id, source, request, actor,
                              fulfillment_verifier):
        return await release(session, organization_id, source,
                             ReleaseInput.model_validate(request), actor, fulfillment_verifier)

    async def accounting_shipment_source(self, session, organization_id, key, sales_source):
        from sqlalchemy import select

        from modules.wms.invoice_shipments import (
            PhysicalShipmentAct,
            act_result,
            render_source_document,
        )

        act = await session.scalar(select(PhysicalShipmentAct).where(
            PhysicalShipmentAct.organization_id == organization_id,
            PhysicalShipmentAct.source_key == str(key),
        ).execution_options(populate_existing=True))
        if act is None:
            return None
        await sales_source.invoice_shipping_source(session, act.document_id,
            organization_id=organization_id, expected_version=act.document_version,
            expected_content_sha256=act.content_sha256, operation="historical_claim")
        result = await act_result(session, act)
        return {"receipt": result, "document_html": render_source_document(result)}

    async def invoice_shipment_preview(self, session, organization_id, source):
        from modules.wms.invoice_shipments import preview
        return await preview(session, organization_id, source)

    async def create_invoice_shipment(self, session, organization_id, source, request, actor):
        from modules.wms.invoice_shipments import create
        return await create(session, organization_id, source, request, actor)

    async def invoice_shipment_by_key(self, session, organization_id, document_id, key):
        from modules.wms.invoice_shipments import act_result, lookup
        act = await lookup(session, organization_id, document_id, key)
        return await act_result(session, act) if act else None

    async def invoice_shipments_register(self, session, organization_id, document_ids):
        """Read verified internal shipment acts for a sales document register.

        This projection is intentionally labelled as internal WMS evidence. It
        does not assert that a TN/TTN exists or replace the statutory document
        source that will be connected separately.
        """
        from sqlalchemy import select

        from modules.wms.invoice_shipments import PhysicalShipmentAct, act_result

        ids = sorted({int(document_id) for document_id in document_ids if int(document_id) > 0})
        if not ids:
            return {"status": "internal_acts_only", "tn_ttn_status": "not_certified", "items": []}
        rows = (await session.scalars(select(PhysicalShipmentAct).where(
            PhysicalShipmentAct.organization_id == organization_id,
            PhysicalShipmentAct.document_id.in_(ids),
        ).order_by(PhysicalShipmentAct.document_id, PhysicalShipmentAct.id)
            .execution_options(populate_existing=True))).all()
        items = []
        for row in rows:
            result = await act_result(session, row)
            snapshot = result["snapshot"]
            items.append({
                "act_id": result["act_id"],
                "document_id": row.document_id,
                "document_version": row.document_version,
                "content_sha256": row.content_sha256,
                "source_key": result["source_key"],
                "operation_date": snapshot["operation_date"],
                "actor": snapshot["actor"],
                "evidence": snapshot["evidence"],
                "line_count": len(snapshot["lines"]),
                "status": "verified_internal_act",
                "tn_ttn_status": "not_certified",
                "document_url": f"/wms/organizations/{organization_id}/physical-shipments/by-key/{result['source_key']}/document",
            })
        return {"status": "internal_acts_only", "tn_ttn_status": "not_certified", "items": items}
