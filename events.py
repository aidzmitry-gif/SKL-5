"""Реакции модуля WMS на события других модулей (через шину, §2.5).

Резерв создаёт задание на подбор без физического движения. Приёмка требует QC,
а расход товара — отдельного подтверждения отгрузки.
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy import select

from core.domain.models import OutboxEvent, Sku
from modules.wms import reservation_events  # noqa: F401
from modules.wms.models import Receipt, ReceiptLine


def _reservation_items(payload: dict) -> list[tuple[str, str, Decimal]]:
    """Validate the complete legacy event batch before adding any ORM objects."""
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("Reservation items must be a list")
    result = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Reservation item must be an object")
        sku = item.get("sku_code")
        warehouse = item.get("warehouse", "Главный")
        if not isinstance(sku, str) or not sku.strip() or len(sku) > 64:
            raise ValueError("Reservation SKU must be a non-empty code of at most 64 characters")
        if not isinstance(warehouse, str) or not warehouse.strip() or len(warehouse) > 128:
            raise ValueError("Reservation warehouse must be a non-empty name of at most 128 characters")
        try:
            qty = Decimal(str(item.get("qty")))
        except (InvalidOperation, ValueError):
            raise ValueError("Reservation quantity must be a positive decimal") from None
        if not qty.is_finite() or qty <= 0 or qty >= Decimal("1000000000000"):
            raise ValueError("Reservation quantity is outside the supported range")
        if qty != qty.quantize(Decimal("0.01")):
            raise ValueError("Reservation quantity cannot exceed two decimal places")
        result.append((sku, warehouse, qty))
    return result


def _invoice_ref(payload):
    document_id = payload.get("document_id")
    if type(document_id) is not int or document_id <= 0:
        raise ValueError("Reservation needs an exact positive document_id")
    return f"sales:document:{document_id}"


async def on_stock_reserved(payload: dict, ctx) -> None:
    await _reservation_event(payload, ctx, release=False)


async def on_stock_released(payload: dict, ctx) -> None:
    await _reservation_event(payload, ctx, release=True)


async def _erp_reservation_items(session, payload, source):
    """Validate delivery against Sales-qualified identity and WMS-owned lines."""
    from modules.wms.invoice_reservations import _locked_reservation

    anchor = source.get("issuance_receipt")
    delivered_anchor = payload.get("issuance_receipt")
    if (not isinstance(anchor, dict) or not anchor
            or not isinstance(delivered_anchor, dict)
            or type(delivered_anchor.get("document_id")) is not int
            or payload.get("issuance_mode") != "erp_issuance_v1"
            or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1
            or delivered_anchor != anchor
            or payload.get("content_sha256") != source["content_sha256"]):
        raise ValueError("ERP reservation issuance identity differs from its source")
    for name, expected in (("document_id", source["document_id"]),
                           ("deal_id", source["deal_id"]),
                           ("organization_id", source["organization_id"]),
                           ("document_version", source["version"])):
        if type(payload.get(name)) is not int or payload[name] != expected:
            raise ValueError("ERP reservation document identity differs from its source")
    reservation, allocations = await _locked_reservation(session, source["organization_id"], source)
    if (payload.get("reservation_digest") != reservation.digest
            or source.get("reservation_digest") != reservation.digest):
        raise ValueError("ERP reservation digest differs from its stored allocation")
    incoming = payload.get("items")
    if not isinstance(incoming, list) or len(incoming) != len(allocations):
        raise ValueError("ERP reservation allocation matrix is incomplete")
    normalized = []
    for row in incoming:
        if (not isinstance(row, dict) or type(row.get("line_no")) is not int
                or row["line_no"] <= 0 or not isinstance(row.get("source"), str)
                or not isinstance(row.get("qty"), str) or "warehouse" not in row):
            raise ValueError("ERP reservation requires exact line, warehouse and decimal quantity")
        sku, warehouse, qty = _reservation_items({"items": [row]})[0]
        normalized.append({"line_no": row["line_no"], "sku_code": sku,
                           "warehouse": warehouse, "qty": format(qty, ".2f"),
                           "source": row["source"]})
    if sorted(normalized, key=lambda row: row["source"]) != allocations:
        raise ValueError("ERP reservation allocation differs from the stored original")
    return [(row["sku_code"], row["warehouse"], Decimal(row["qty"])) for row in allocations]


async def _reservation_event(payload, ctx, *, release):
    from modules.wms.reservation_events import apply

    if ctx is None:
        raise ValueError("EventContext is required for stock reservation delivery")
    items = _reservation_items(payload)
    if not items:
        raise ValueError("Reservation needs nonempty items")
    _invoice_ref(payload)
    gateway = getattr(getattr(ctx, "services", None), "sales_source", None)
    if gateway is None:
        raise ValueError("Sales invoice source service is required")
    accounting = getattr(ctx.services, "accounting", None)
    if not callable(getattr(accounting, "lock_event_organization", None)):
        raise ValueError("Source organization serialization service is required")
    organization_id = await gateway.invoice_organization(ctx.session, payload["document_id"])
    await accounting.lock_event_organization(ctx.session, organization_id)
    source = await gateway.invoice_reservation(ctx.session, payload["document_id"])
    if source["organization_id"] != organization_id:
        raise ValueError("Reservation invoice owner changed during source discovery")
    if "organization_id" in payload and (type(payload["organization_id"]) is not int or payload["organization_id"] != organization_id):
        raise ValueError("Reservation organization differs from the invoice")
    if source.get("issuance_receipt") is not None or any(
        key in payload for key in ("issuance_mode", "issuance_receipt", "reservation_digest")
    ):
        items = await _erp_reservation_items(ctx.session, payload, source)
    quantities = {}
    for sku, _, qty in items:
        quantities[sku] = quantities.get(sku, Decimal("0")) + qty
    if {sku: format(qty, ".2f") for sku, qty in quantities.items()} != source["quantities"]:
        raise ValueError("Reservation quantities differ from the original invoice")
    if release and source["reserve_status"] != "released":
        raise ValueError("Invoice has not released its reservation")
    # A delayed reserve event must not resurrect an already released invoice.
    await apply(ctx.session, payload["document_id"], items, organization_id=organization_id,
                release=release or source["reserve_status"] == "released")


async def on_goods_received(payload: dict, ctx) -> None:
    """Приёмка из закупок/производства → документ приёмки в ``pending_qc`` (БЕЗ движения).

    Движение прихода пишется только после QC-приёмки (``/wms/receipts/{id}/accept``) по
    фактически принятому кол-ву — брак на свободный остаток не попадает. ``entity_ref``
    (напр. ``purchase:<id>``) сохраняется для трассировки; sku берётся из ``item``/``sku_code``.
    """
    if ctx is None:
        raise ValueError("EventContext is required for goods receipt delivery")
    sku_code = payload.get("sku_code") or payload.get("item") or ""
    if not isinstance(sku_code, str) or not sku_code.strip() or len(sku_code) > 64:
        raise ValueError("Receipt SKU must be a non-empty code of at most 64 characters")
    try:
        quantity = Decimal(str(payload.get("qty")))
    except (InvalidOperation, ValueError):
        raise ValueError("Receipt quantity must be a positive decimal") from None
    if not quantity.is_finite() or quantity <= 0 or quantity >= Decimal("1000000000000"):
        raise ValueError("Receipt quantity is outside the supported range")
    if quantity != quantity.quantize(Decimal("0.01")):
        raise ValueError("Receipt quantity cannot exceed two decimal places")
    organization_id = payload.get("organization_id")
    if organization_id is not None and (type(organization_id) is not int or organization_id <= 0):
        raise ValueError("Receipt organization_id must be a positive integer")
    warehouse = payload.get("warehouse", "Главный")
    if not isinstance(warehouse, str) or not warehouse.strip() or len(warehouse) > 128:
        raise ValueError("Receipt warehouse must be a non-empty name of at most 128 characters")
    entity_ref = payload.get("entity_ref", "")
    if not isinstance(entity_ref, str) or len(entity_ref) > 128:
        raise ValueError("Receipt entity_ref must be text of at most 128 characters")
    event_id = getattr(ctx, "event_id", None)
    if ("output_document_id" in payload or entity_ref.startswith("production_output:")) and event_id is None:
        raise ValueError("Confirmed output requires a durable event identity")
    if event_id is not None:
        # Serialize retries using the durable outbox identity, not document text:
        # two legitimate source events can refer to the same order or SKU.
        if type(event_id) is not int or event_id <= 0:
            raise ValueError("Receipt event_id must be a positive integer")
        event = await ctx.session.scalar(select(OutboxEvent).where(OutboxEvent.id == event_id)
            .with_for_update().execution_options(populate_existing=True))
        if event is None:
            raise ValueError("Receipt source event does not exist")
        # Check before idempotent return: a known event ID does not authorize a
        # different body. Canonical JSON keeps booleans distinct from numbers.
        if (event.event_type not in {"production.completed", "procurement.received", "production.output.confirmed"}
                or event.version != 1
                or json.dumps(event.payload, sort_keys=True, allow_nan=False)
                != json.dumps(payload, sort_keys=True, allow_nan=False)):
            raise ValueError("Receipt delivery differs from its stored source event")
        if event.event_type == "production.output.confirmed" or "output_document_id" in payload or entity_ref.startswith("production_output:"):
            if event.event_type != "production.output.confirmed":
                raise ValueError("Output requires a confirmed production event")
            gateway = getattr(ctx.services, "production_output", None)
            if gateway is None:
                raise ValueError("Production output source service is unavailable")
            await gateway.validate_delivery(ctx.session, event_id, payload)
        if await ctx.session.scalar(select(Receipt.id).where(Receipt.source_event_id == event_id)) is not None:
            return
    from modules.wms.primary_receipts import _reject_native_legacy_receipt

    try:
        await _reject_native_legacy_receipt(ctx.session, organization_id, warehouse)
    except HTTPException as exc:
        raise ValueError("Native or preparing warehouse receipts require typed physical intake") from exc
    # ponytail: источник по префиксу entity_ref (purchase→procurement, прочее→production);
    # точный источник лучше передавать в payload, когда у событий появится поле source.
    source = "procurement" if entity_ref.startswith("purchase") else "production"
    title = ""
    sku = (
        await ctx.session.execute(select(Sku).where(Sku.code == sku_code))
    ).scalars().first()
    if sku:
        title = sku.title
    receipt = Receipt(
        organization_id=organization_id,
        source_event_id=event_id,
        source=source,
        entity_ref=entity_ref,
        warehouse=warehouse,
        status="pending_qc",
    )
    ctx.session.add(receipt)
    await ctx.session.flush()
    receipt.number = f"ПРМ-2026-{receipt.id:04d}"
    ctx.session.add(
        ReceiptLine(
            receipt_id=receipt.id,
            sku_code=sku_code,
            sku_title=title,
            expected_qty=quantity,
            **({"location_id": payload["location_id"], "batch_ref": payload["lot"]}
               if "output_document_id" in payload else {}),
        )
    )
