"""Read confirmed shipping places before handover; no carrier or stock writes."""
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select

from core.services.logistics import MobileFulfillmentAuthorityDenied

from modules.wms.physical_models import (
    DeliveryBindingRevision, PhysicalHold, PhysicalLedger, PhysicalLocation, PhysicalOperation, PhysicalTask,
    PickedAllocation, StockUnit,
)


async def delivery_options(session, stock, organization_id, warehouse_id, document_id):
    """Explicit allocation preview; never infer producer quantity coverage."""
    from modules.wms.invoice_reservations import InvoiceReservation, _locked_reservation
    from modules.wms.physical_stock import control, decimal_text, protect_blind_projection

    await control(session, warehouse_id, organization_id, lock=False)
    reservation = await session.get(InvoiceReservation, document_id)
    if reservation is None or reservation.organization_id != organization_id:
        raise HTTPException(404, "Подтверждённый резерв счёта не найден")
    exact = {"organization_id": organization_id, "document_id": document_id,
        "expected_version": reservation.snapshot["version"],
        "expected_content_sha256": reservation.snapshot["content_sha256"]}
    logistics = getattr(stock.services, "logistics", None)
    sales = getattr(stock.services, "sales_source", None)
    if logistics is None or sales is None:
        raise HTTPException(503, "Проверка отправки временно недоступна")
    prepared = await logistics.prepare_warehouse_invoice_fulfillment_snapshot(session, exact_invoice=exact, user=stock.user)
    try:
        source = await sales.invoice_shipping_source(session, document_id, organization_id=organization_id,
            expected_version=exact["expected_version"], expected_content_sha256=exact["expected_content_sha256"], operation="fulfill")
    except ValueError as exc:
        raise HTTPException(409, "Источник счёта изменился; повторите проверку отправки") from exc
    reservation, allocations = await _locked_reservation(session, organization_id, source)
    snapshot = await logistics.collect_prepared_invoice_fulfillment_snapshot(session, prepared)
    await control(session, warehouse_id, organization_id)
    await protect_blind_projection(session, [warehouse_id])
    sources = {item["source"] for item in allocations}
    rows = (await session.execute(select(PhysicalTask, PhysicalHold, StockUnit)
        .join(PhysicalHold, PhysicalTask.hold_id == PhysicalHold.id)
        .join(StockUnit, PhysicalHold.unit_id == StockUnit.id)
        .where(PhysicalTask.warehouse_id == warehouse_id, PhysicalHold.warehouse_id == warehouse_id,
            PhysicalHold.order_ref == f"invoice:{document_id}", PhysicalTask.kind == "pick",
            PhysicalTask.status == "open", PhysicalTask.assignee == "", PhysicalTask.binding_revision_id.is_(None),
            PhysicalHold.source.in_(sources), PhysicalHold.picked == 0, PhysicalHold.shipped == 0,
            PhysicalHold.consumed == 0, PhysicalTask.qty > 0).order_by(PhysicalTask.id))).all()
    tasks = [{"task_id": task.id, "sku_code": unit.sku_code, "qty": decimal_text(task.qty), "container": hold.container}
        for task, hold, unit in rows]
    executions = {}
    for item in sorted(snapshot["producer_sources"]["sources"], key=lambda item: (item["source"]["kind"], item["source"]["key"], item["source"]["revision"])):
        intent = item["envelope"]["payload"]["intent"]
        carrier = {str(intent.get(key) or "").strip().casefold() for key in ("carrier_code", "carrier_name")}
        if not item["fulfillment_allowed"] or not carrier.intersection({"автолайт", "autolight", "avtolight"}) or not intent.get("pickup_date"):
            continue
        executions.setdefault(item["execution_id"], {
            "execution_id": item["execution_id"], "source_kind": item["source"]["kind"],
            "source_key": item["source"]["key"], "source_revision": item["source"]["revision"],
            "expected_intent_digest": item["intent_digest"], "expected_source_state_sha256": item["source_state_sha256"],
            "document_id": document_id, "expected_version": exact["expected_version"],
            "expected_content_sha256": exact["expected_content_sha256"], "expected_reservation_digest": reservation.digest,
            "carrier": intent.get("carrier_name") or intent.get("carrier_code"), "pickup_date": intent["pickup_date"],
        })
    return {"protocol": 1, "organization_id": organization_id, "warehouse_id": warehouse_id,
        "document_id": document_id, "tasks": tasks, "executions": list(executions.values())}


def delivery_priority(snapshot, now=None):
    """Only a verified carrier/pickup cutoff can prioritize today's shipment."""
    now = (now or datetime.now(ZoneInfo("Europe/Minsk"))).astimezone(ZoneInfo("Europe/Minsk"))
    raw = snapshot["priority"].get("priority_cutoff_at")
    if raw is None:
        return {"cutoff_at": None, "priority": False, "overdue": False}
    cutoff = datetime.fromisoformat(raw)
    if cutoff.tzinfo is None:
        raise HTTPException(409, "Срок отправки не подтверждён")
    cutoff = cutoff.astimezone(ZoneInfo("Europe/Minsk"))
    return {"cutoff_at": raw, "priority": cutoff.date() == now.date(), "overdue": now >= cutoff}


def _empty(snapshot, now=None):
    return {
        "binding_id": snapshot["binding_id"], "revision": snapshot["revision"],
        "document_id": snapshot["invoice"]["document_id"],
        "warehouse_id": snapshot["warehouse_id"],
        "carrier": snapshot["priority"]["carrier_name"] or snapshot["priority"]["carrier_code"] or "",
        "pickup_date": snapshot["priority"]["pickup_date"], **delivery_priority(snapshot, now),
        "status": "waiting", "reason": None, "place_count": None, "gross_kg_total": None,
        "packages": [], "confirmed_at": None, "confirmed_by": None, "packing_operation_id": None,
    }


async def _recorded_snapshot(session, binding):
    from modules.wms.physical_delivery import validate_binding_row

    operation = await session.get(PhysicalOperation, binding.operation_pk)
    return validate_binding_row(binding, operation)


async def task_deliveries(
    session, stock, organization_id, warehouse_id, tasks, *,
    mobile_authority=None,
):
    """Caller enters before warehouse domain locks; no mutation, no task reassignment."""
    from modules.wms.physical_delivery import collect_live_binding, prepare_live_binding
    from modules.wms.mobile_access import make_mobile_fulfillment_read_capability

    if mobile_authority is not None and getattr(mobile_authority, "purpose", None) != "state":
        raise MobileFulfillmentAuthorityDenied()

    ids = {task.binding_revision_id for task in tasks if task.binding_revision_id is not None}
    output = {}
    bindings = (await session.scalars(select(DeliveryBindingRevision).where(
        DeliveryBindingRevision.organization_id == organization_id,
        DeliveryBindingRevision.warehouse_id == warehouse_id,
        DeliveryBindingRevision.id.in_(ids),
    ))).all() if ids else []
    if {binding.id for binding in bindings} != ids:
        raise HTTPException(409, "Привязка задания к отправке повреждена")
    entries = [(binding, await _recorded_snapshot(session, binding)) for binding in bindings]
    prepared_entries = []
    # Complete every producer/source callback before entering the first gate.
    for binding, snapshot in sorted(entries, key=lambda item: (item[1]["invoice"]["document_id"], item[0].id)):
        reason = None
        prepared = None
        capability = None
        if mobile_authority is not None:
            invoice = snapshot["invoice"]
            exact_invoice = {
                "organization_id": organization_id,
                "document_id": invoice["document_id"],
                "expected_version": invoice["version"],
                "expected_content_sha256": invoice["content_sha256"],
            }
            capability = make_mobile_fulfillment_read_capability(
                session, stock, mobile_authority,
                warehouse_id=warehouse_id, binding_revision_id=binding.id, exact_invoice=exact_invoice,
            )
        try:
            prepared = await prepare_live_binding(
                session, stock, binding, mobile_fulfillment=capability,
            )
        except MobileFulfillmentAuthorityDenied:
            raise
        except HTTPException as exc:
            if exc.status_code not in {403, 404, 409, 422}:
                raise
            reason = "Источник отправки изменился или больше недоступен; нужна проверка ответственного"
        prepared_entries.append((binding, snapshot, prepared, reason))
    for binding, snapshot, prepared, reason in prepared_entries:
        if prepared is not None:
            try:
                await collect_live_binding(session, stock, prepared)
            except MobileFulfillmentAuthorityDenied:
                raise
            except HTTPException as exc:
                if exc.status_code not in {403, 404, 409, 422}:
                    raise
                reason = "Источник отправки изменился или больше недоступен; нужна проверка ответственного"
        metadata = {"binding_revision_id": binding.id,
            "carrier": (snapshot["priority"]["carrier_name"] or snapshot["priority"]["carrier_code"] or "") if reason is None else "",
            **(delivery_priority(snapshot) if reason is None else {"cutoff_at": None, "priority": False, "overdue": False}),
            "blocked_reason": reason}
        allowed = {row["task_id"] for row in snapshot["allocations"]}
        for task in tasks:
            if task.binding_revision_id == binding.id:
                if task.id not in allowed:
                    raise HTTPException(409, "Задание не входит в подтверждённую отправку")
                output[task.id] = metadata
    return output


async def _packing_facts(session, stock, warehouse, snapshot, row):
    from modules.wms.item_identity import _OriginUnproven, _origin_validate_operation
    from modules.wms.physical_stock import decimal_text, dimensions, picked_output

    binding_id = snapshot["binding_id"]
    picks = (await session.scalars(select(PickedAllocation).join(
        PhysicalHold, PickedAllocation.hold_id == PhysicalHold.id,
    ).where(PhysicalHold.warehouse_id == snapshot["warehouse_id"],
        PickedAllocation.binding_revision_id == binding_id,
        PickedAllocation.canceled.is_(False)))).all()
    if any(pick.shipped > 0 for pick in picks):
        row.update(status="handover_started", reason="Для следующей накладной требуется отдельный подтверждённый состав отправки")
        return row
    operation = await session.scalar(select(PhysicalOperation).where(
        PhysicalOperation.organization_id == snapshot["organization_id"],
        PhysicalOperation.warehouse_id == snapshot["warehouse_id"], PhysicalOperation.kind == "pack_v2",
        PhysicalOperation.result["physical_causal_v1"]["canonical_request"]["binding_revision_id"].as_string() == binding_id,
    ).order_by(PhysicalOperation.id.desc()).limit(1))
    if operation is None:
        return row
    # Any subsequent cancellation/re-pick changes exact IDs. Old figures disappear
    # immediately, even if their original immutable receipt remains available.
    row.update(status="stale", reason="Состав, состояние или подтверждение упаковки изменились")
    try:
        value = {key: getattr(operation, key) for key in (
            "id", "organization_id", "warehouse_id", "operation_id", "kind", "request_hash", "result", "actor")}
        _origin_validate_operation(value, snapshot["organization_id"])
        proof = operation.result["shipping_packages_v1"]
        if proof["binding_revision_id"] != binding_id:
            return row
        expected = defaultdict(lambda: Decimal(0))
        for package in proof["packages"]:
            for line in package["lines"]:
                expected[line["pick_id"]] += Decimal(line["qty"])
        if expected != {pick.id: pick.qty for pick in picks}:
            return row
        if len({pick.unit_id for pick in picks}) != len(picks):
            return row
        if set(operation.result["physical_causal_v1"]["prior_frontiers"]) != {pick.unit_id for pick in picks}:
            return row
        ledger = (await session.scalars(select(PhysicalLedger).where(PhysicalLedger.operation_pk == operation.id))).all()
        if {item.unit_id for item in ledger} != {pick.unit_id for pick in picks}:
            return row
        targets = {item["task_id"]: item for item in snapshot["allocations"]}
        pack_lines = {item["pick_id"]: item for item in operation.result["physical_causal_v1"]["canonical_request"]["lines"]}
        by_task = defaultdict(lambda: Decimal(0))
        for pick in picks:
            if not pick.packed or pick.task_id is None:
                return row
            target = targets.get(pick.task_id)
            if target is None or pick.hold_id != target["hold_id"]:
                return row
            hold = await session.get(PhysicalHold, pick.hold_id)
            task = await session.get(PhysicalTask, pick.task_id)
            if (hold is None or task is None or hold.source != target["source_key"]
                    or hold.unit_id != target["unit_id"] or task.kind != "pick"
                    or hold.warehouse_id != snapshot["warehouse_id"] or hold.order_ref != f"invoice:{snapshot['invoice']['document_id']}"
                    or hold.container != proof["container"] or task.hold_id != hold.id or task.source != hold.source
                    or task.warehouse_id != snapshot["warehouse_id"] or task.unit_id != hold.unit_id
                    or task.binding_revision_id != binding_id):
                return row
            by_task[pick.task_id] += pick.qty
            clone = await session.get(StockUnit, pick.unit_id)
            original = await session.get(StockUnit, hold.unit_id)
            location = await session.get(PhysicalLocation, clone.location_id) if clone is not None else None
            if (clone is None or original is None or location is None or location.kind != "dispatch"
                    or original.warehouse_id != snapshot["warehouse_id"]
                    or clone.warehouse_id != snapshot["warehouse_id"]
                    or clone.qty != pick.qty or clone.reserved != pick.qty
                    or clone.container != hold.container or clone.assembly_output_id != original.assembly_output_id):
                return row
            projection = await picked_output(session, stock.reference, warehouse, pick, hold, clone, original)
            if pack_lines[pick.id]["batch_scan"] != projection["batch_scan"]:
                return row
            changes = [item for item in ledger if item.unit_id == clone.id]
            if (len(changes) != 2 or sorted(item.qty_delta for item in changes) != [-pick.qty, pick.qty]
                    or next(item.dimensions for item in changes if item.qty_delta > 0) != dimensions(clone)):
                return row
            # Exact pick->hold/task/clone attribution is in its original receipt,
            # rather than inferred from today's equal quantities or same SKU.
            pick_operations = (await session.scalars(select(PhysicalOperation).where(
                PhysicalOperation.organization_id == snapshot["organization_id"],
                PhysicalOperation.warehouse_id == snapshot["warehouse_id"], PhysicalOperation.kind == "pick",
                PhysicalOperation.result["pick_id"].as_integer() == pick.id,
            ).limit(2))).all()
            if len(pick_operations) != 1:
                return row
            pick_operation = pick_operations[0]
            _origin_validate_operation({key: getattr(pick_operation, key) for key in (
                "id", "organization_id", "warehouse_id", "operation_id", "kind", "request_hash", "result", "actor")}, snapshot["organization_id"])
            request = pick_operation.result["physical_causal_v1"]["canonical_request"]
            if (request["hold_id"] != hold.id or request.get("task_id") != task.id
                    or Decimal(request["qty"]) != pick.qty or pick_operation.result["unit"]["id"] != clone.id):
                return row
            pick_ledger = (await session.scalars(select(PhysicalLedger).where(
                PhysicalLedger.operation_pk == pick_operation.id))).all()
            if sorted((item.unit_id, item.qty_delta) for item in pick_ledger) != sorted([
                (original.id, -pick.qty), (clone.id, pick.qty),
            ]):
                return row
        if by_task != {item["task_id"]: Decimal(item["target_qty"]) for item in snapshot["allocations"]}:
            return row
        created_at = operation.created_at
        if created_at.tzinfo is None:
            # SQLAlchemy SQLite loses offsets; PostgreSQL stores aware timestamps.
            from datetime import timezone
            created_at = created_at.replace(tzinfo=timezone.utc)
        row.update(status="confirmed", reason=None, place_count=proof["place_count"],
            gross_kg_total=decimal_text(Decimal(proof["gross_kg_total"])),
            packages=[{"code": item["code"], "gross_kg": item["gross_kg"]} for item in proof["packages"]],
            confirmed_at=created_at.isoformat(), confirmed_by=operation.actor,
            packing_operation_id=operation.operation_id)
    except (_OriginUnproven, ValueError, KeyError, TypeError, AttributeError, ArithmeticError, HTTPException):
        pass  # Malformed historical receipt never becomes current office figures.
    return row


async def office_packing(session, stock, organization_id, warehouse_id):
    from modules.wms.physical_delivery import collect_live_binding, prepare_live_binding
    from modules.wms.physical_stock import control, protect_blind_projection

    # Organization membership is checked by the route before this reader.
    # Read immutable identities first; source/domain order matches bound commands.
    await control(session, warehouse_id, organization_id, lock=False)
    bindings = (await session.scalars(select(DeliveryBindingRevision).where(
        DeliveryBindingRevision.organization_id == organization_id,
        DeliveryBindingRevision.warehouse_id == warehouse_id,
    ).order_by(DeliveryBindingRevision.created_at.desc(), DeliveryBindingRevision.id).limit(201))).all()
    items = []
    entries = [(binding, await _recorded_snapshot(session, binding)) for binding in bindings[:200]]
    prepared_entries = []
    for binding, snapshot in sorted(entries, key=lambda item: (item[1]["invoice"]["document_id"], item[0].id)):
        row = _empty(snapshot)
        prepared = None
        try:
            prepared = await prepare_live_binding(session, stock, binding)
        except HTTPException as exc:
            if exc.status_code in {403, 404}:
                continue  # The limited office read grants no producer visibility.
            if exc.status_code not in {409, 422}:
                raise
            row.update(status="blocked", reason="Источник отправки изменился; нужна проверка ответственного")
        prepared_entries.append((snapshot, row, prepared))
    # No source callbacks below this line. All source locks are already held.
    for snapshot, row, prepared in prepared_entries:
        if prepared is not None:
            try:
                await collect_live_binding(session, stock, prepared)
            except HTTPException as exc:
                if exc.status_code in {403, 404}:
                    continue
                if exc.status_code not in {409, 422}:
                    raise
                row.update(status="blocked", reason="Источник отправки изменился; нужна проверка ответственного")
        if row["status"] == "blocked":
            row.update(carrier="", pickup_date=None, cutoff_at=None, priority=False, overdue=False)
        items.append((snapshot, row))
    warehouse = await control(session, warehouse_id, organization_id)
    await protect_blind_projection(session, [warehouse_id])
    result = []
    for snapshot, row in items:
        result.append(await _packing_facts(session, stock, warehouse, snapshot, row) if row["status"] != "blocked" else row)
    result.sort(key=lambda row: (not row["priority"], row["cutoff_at"] or "9999", row["document_id"], row["binding_id"]))
    return {"protocol": 1, "organization_id": organization_id, "warehouse_id": warehouse_id,
        "items": result, "truncated": len(bindings) > 200}
