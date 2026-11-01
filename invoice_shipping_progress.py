"""Invoice-local progress from verified acts, never external coverage or a command."""
from decimal import Decimal, InvalidOperation
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from core.services.logistics import ExactInvoiceIdentity
from modules.wms.invoice_fulfillment import invoice_fulfillment_snapshot
from modules.wms.invoice_remainder import RemainderRelease
from modules.wms.invoice_remainder import result as release_result


def reject():
    raise HTTPException(409, "Canonical invoice shipment progress requires reconciliation")


async def progress(session, organization_id, source):
    """Caller holds organization -> Sales locks; WMS locks precede Office locks."""
    try:
        return await _progress(session, organization_id, source)
    except (ValueError, TypeError, KeyError, InvalidOperation) as exc:
        raise HTTPException(409, "Canonical invoice shipment progress is invalid") from exc


async def _progress(session, organization_id, source):
    with session.no_autoflush:
        evidence = await invoice_fulfillment_snapshot(session, organization_id, source)
        exact = ExactInvoiceIdentity(organization_id=organization_id, document_id=source["document_id"],
            expected_version=source["version"], expected_content_sha256=source["content_sha256"]).model_dump()
        snapshot = evidence["snapshot"]
        allocations = snapshot["reservation"]["original_snapshot"]["allocations"]
        if not allocations:
            reject()
        shipped = {row["source"]: Decimal("0") for row in allocations}
        reserved = {}
        for version in snapshot["versions"]:  # Collector orders source, version and verifies the complete chain.
            reserved[version["source"]] = Decimal(version["qty"])
        acts = []
        for act in snapshot["acts"]:
            if str(UUID(act["source_key"])) != act["source_key"]:
                reject()
            for row in act["snapshot"]["lines"]:
                if row["source"] not in shipped:
                    reject()
                shipped[row["source"]] += Decimal(row["qty"])
            acts.append({"act_id": act["act_id"], "source_key": act["source_key"], "digest": act["digest"],
                "operation_date": act["snapshot"]["operation_date"], "execution_mode": act["execution_mode"]})
        lines = []
        for allocation in allocations:
            key = allocation["source"]
            expected, issued, remaining = Decimal(allocation["qty"]), shipped[key], reserved.get(key)
            if (remaining is None or not all(value.is_finite() for value in (expected, issued, remaining))
                    or expected <= 0 or issued < 0 or remaining < 0 or issued + remaining > expected):
                reject()
            lines.append({**{name: allocation[name] for name in ("source", "line_no", "sku_code", "warehouse")},
                "expected_qty": format(expected, ".2f"), "shipped_qty": format(issued, ".2f"),
                "reserved_qty": format(remaining, ".2f"), "released_qty": format(expected - issued - remaining, ".2f")})
        releases = []
        for row in await session.scalars(select(RemainderRelease).where(
                RemainderRelease.organization_id == organization_id,
                RemainderRelease.document_id == source["document_id"]).order_by(RemainderRelease.id)
                .execution_options(populate_existing=True)):
            verified = await release_result(session, row)
            releases.append({"release_id": verified["release_id"], "digest": verified["digest"]})
        complete = all(shipped[row["source"]] == Decimal(row["qty"]) for row in allocations)
        return {"schema_version": 1, "exact_invoice": exact, "scope": "canonical_invoice_reservation",
            "coverage_complete": False, "state": "complete" if complete else "partial" if acts else "none_recorded",
            "lines": lines, "acts": acts, "releases": releases, "basis_digest": evidence["digest"]}
