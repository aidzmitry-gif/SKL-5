"""Physical receipt reconciliation without treating prepared quantities as stock."""
from collections import Counter
from decimal import Decimal

from sqlalchemy import select

from modules.wms.models import Receipt, ReceiptLine, StockMovement
from modules.wms.primary_receipts import PrimaryReceiptBinding


async def reconcile(session, organization_id, source):
    bindings = (await session.scalars(select(PrimaryReceiptBinding).where(
        PrimaryReceiptBinding.organization_id == organization_id,
        PrimaryReceiptBinding.source_receipt_id == source["id"],
    ).order_by(PrimaryReceiptBinding.receipt_id))).all()
    versions = sorted({binding.source_version for binding in bindings})
    conflict = any(version != source["version"] for version in versions)
    totals = {}
    receipts = []
    for binding in bindings:
        receipt = await session.get(Receipt, binding.receipt_id)
        if receipt is None or receipt.organization_id != organization_id:
            raise ValueError("Physical receipt ownership is inconsistent")
        if receipt.warehouse != binding.command["warehouse"]:
            raise ValueError("Physical receipt warehouse differs from the binding")
        lines = (await session.scalars(select(ReceiptLine).where(ReceiptLine.receipt_id == receipt.id))).all()
        if {line.id for line in lines} != {item["receipt_line_id"] for item in binding.line_bindings}:
            raise ValueError("Physical receipt line set differs from the binding")
        by_id = {line.id: line for line in lines}
        details = []
        expected_movements = Counter()
        for item in binding.line_bindings:
            line = by_id[item["receipt_line_id"]]
            fact = binding.source_snapshot["document"]["items"][item["position"] - 1]
            expected = Decimal(item["quantity"])
            if line.expected_qty != expected or line.sku_code != item["sku_code"] or line.batch_ref != fact["lot"]:
                raise ValueError("Physical receipt facts differ from the binding")
            accepted, rejected = Decimal(0), Decimal(0)
            if receipt.status == "accepted":
                if (line.accepted_qty is None or line.rejected_qty is None or line.accepted_qty < 0
                        or line.rejected_qty < 0 or line.accepted_qty + line.rejected_qty != expected):
                    raise ValueError("Accepted physical receipt has incomplete QC")
                accepted, rejected = line.accepted_qty, line.rejected_qty
                if accepted > 0:
                    expected_movements[(line.sku_code, receipt.warehouse, accepted, "in", line.location_id, line.batch_ref)] += 1
            elif receipt.status != "pending_qc":
                raise ValueError("Unsupported physical receipt reconciliation state")
            details.append({"source_line": item["source_line"], "position": item["position"],
                            "receipt_line_id": line.id, "sku_code": line.sku_code, "unit": item["unit"],
                            "prepared": str(expected), "accepted": str(accepted), "rejected": str(rejected)})
            if not conflict:
                total = totals.setdefault(item["position"], [Decimal(0), Decimal(0), Decimal(0)])
                for index, value in enumerate((expected, accepted, rejected)):
                    total[index] += value
        movements = (await session.scalars(select(StockMovement).where(
            StockMovement.organization_id == organization_id, StockMovement.doc_ref == receipt.number,
            StockMovement.reason == "receipt",
        ))).all()
        actual_movements = Counter((row.sku_code, row.warehouse, row.qty, row.kind, row.location_id, row.batch_ref) for row in movements)
        if actual_movements != expected_movements:
            raise ValueError("Physical arrival movements do not match the receipt QC")
        receipts.append({"receipt_id": receipt.id, "number": receipt.number, "warehouse": receipt.warehouse,
                         "source_version": binding.source_version, "status": receipt.status,
                         "actor": binding.actor, "lines": details})
    summary = []
    if not conflict:
        for position, fact in enumerate(source["document"]["items"], start=1):
            quantity = Decimal(fact["quantity"])
            prepared, accepted, rejected = totals.get(position, (Decimal(0), Decimal(0), Decimal(0)))
            if prepared > quantity or accepted < 0 or rejected < 0 or accepted + rejected > prepared:
                raise ValueError("Physical quantities exceed the primary receipt")
            summary.append({"position": position, "sku": fact["sku"], "lot": fact["lot"], "unit": fact.get("unit"),
                            "quantity": str(quantity), "prepared": str(prepared), "accepted": str(accepted),
                            "rejected": str(rejected), "pending_qc": str(prepared - accepted - rejected),
                            "unallocated": str(quantity - prepared)})
    return {"organization_id": organization_id, "receipt_id": source["id"], "source_version": source["version"],
            "status": "version_conflict" if conflict else "linked" if bindings else "unlinked",
            "bound_versions": versions, "rows": summary, "receipts": receipts,
            "cost_status": "not_assessed"}
