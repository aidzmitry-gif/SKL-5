"""Bind TSD fulfillment to the verified immutable ERP sales invoice/stock act."""
import re
from collections import defaultdict
from decimal import Decimal

from modules.wms.invoice_reservations import InvoiceReservation
from modules.wms.invoice_shipments import ShipmentInput, create, preview
from modules.wms.physical_stock import reject, today


async def create_invoice_act(session, services, user, warehouse, data, actor, qualified):
    if not data.source.startswith("invoice:"):
        return None
    if not re.fullmatch(r"invoice:[1-9][0-9]*", data.source) or services is None or user is None:
        reject("Отгрузка счёта требует проверенный источник продаж")
    document_id = int(data.source.split(":")[1])
    reservation = await session.get(InvoiceReservation, document_id)
    if reservation is None or reservation.organization_id != warehouse.organization_id:
        reject("Счёт не имеет подтверждённого резерва организации")
    source = await services.sales_source.invoice_shipping_source(session, document_id,
        organization_id=warehouse.organization_id, expected_version=reservation.snapshot["version"],
        expected_content_sha256=reservation.snapshot["content_sha256"], operation="fulfill")
    allocations = {row["source"]: row for row in reservation.snapshot["allocations"]}
    totals = defaultdict(lambda: Decimal(0))
    for line, _, hold, unit, _ in qualified:
        original = allocations.get(hold.source)
        if original is None or original["sku_code"] != unit.sku_code:
            reject("Подобранная строка не соответствует исходному счёту")
        if any(original.get(key, "") != getattr(unit, key) for key in ("characteristic", "purpose")):
            reject("Характеристика или назначение подобранного товара отличается от подтверждённого распределения счёта")
        totals[hold.source] += line.qty
    basis = await preview(session, warehouse.organization_id, source, reference=services.stock)
    request = ShipmentInput(expected_version=source["version"], expected_content_sha256=source["content_sha256"],
        source_key=data.operation_id, expected_reservation_digest=basis["reservation_digest"],
        expected_remaining_digest=basis["remaining_digest"], expected_physical_digest=basis["physical_digest"],
        operation_date=today(), evidence="; ".join(data.accompanying_documents)[:1000],
        lines=[{"line_no": allocations[key]["line_no"], "warehouse": allocations[key]["warehouse"],
            "qty": format(qty, "f")} for key, qty in sorted(totals.items())])
    await services.accounting.source_changed(session, warehouse.organization_id, user,
        f"wms:physical-shipment:{warehouse.organization_id}:{data.operation_id}", 1, today().isoformat())
    result = await create(session, warehouse.organization_id, source, request, actor,
        reference=services.stock, erp_execution_id=data.operation_id)
    services.event_bus.emit(session, "wms.invoice.physical_shipped", {"organization_id": warehouse.organization_id,
        "document_id": document_id, "act_id": result["act_id"], "digest": result["digest"], "source_key": result["source_key"]})
    return {"act_id": result["act_id"], "digest": result["digest"], "document_id": document_id,
        "document_version": source["version"], "content_sha256": source["content_sha256"]}
