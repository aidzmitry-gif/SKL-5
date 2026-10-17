"""Verified immutable delivery attribution for native warehouse work."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from modules.wms.physical_models import (
    DeliveryBindingRevision,
    PhysicalHold,
    PhysicalOperation,
    PhysicalTask,
    PickedAllocation,
    StockUnit,
)


def _reject(message: str, status: int = 409) -> None:
    raise HTTPException(status, message)


def _same_typed(left, right) -> bool:
    return type(left) is type(right) and left == right


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def binding_key(execution_id, warehouse_id: int, organization_id: int) -> str:
    return f"{int(organization_id)}:{str(execution_id)}:{int(warehouse_id)}"


def normalize_legacy_optional_fields(request: dict, kind: str) -> dict:
    normalized = dict(request)
    key = "task_id" if kind == "pick" else "binding_revision_id" if kind == "pack_v2" else None
    if key is not None and normalized.get(key) is None:
        normalized.pop(key, None)
    return normalized


def split_task_target(remaining: Decimal, target: Decimal) -> tuple[Decimal, Decimal]:
    if target <= 0 or remaining <= 0 or target > remaining:
        _reject("Цель привязки должна быть положительной и не превышать остаток задания", 409)
    return target, remaining - target


def pick_remaining(task_remaining: Decimal, hold_remaining: Decimal, bound_other: Decimal, qty: Decimal) -> Decimal:
    available = min(task_remaining, hold_remaining - bound_other)
    if qty <= 0 or available < 0 or qty > available:
        _reject("Количество превышает свободную цель задания", 409)
    return task_remaining - qty


def restore_task_quantity(task_remaining: Decimal, picked_qty: Decimal) -> Decimal:
    if picked_qty <= 0:
        _reject("Отменяемое количество должно быть положительным", 409)
    return task_remaining + picked_qty


def validate_task_pick_totals(expected: dict[str, Decimal], actual: dict[str, Decimal]) -> None:
    if actual != expected:
        _reject("Подборы не завершают точные цели привязки", 409)


def bound_target_remaining(target: Decimal, active_picked: Decimal) -> Decimal:
    """Derive bound work only from the immutable target and active pick rows."""
    if (not target.is_finite() or not active_picked.is_finite()
            or target <= 0 or active_picked < 0 or active_picked > target):
        _reject("Активные подборы вышли за неизменяемую цель доставки", 409)
    return target - active_picked


def validate_pick_place_partition(picks, packages) -> None:
    """Require measured places to partition each selected pick exactly once in total."""
    expected = {str(pick.id): Decimal(pick.qty) for pick in picks}
    totals = {}
    for package in packages or ():
        lines = package.get("lines") if type(package) is dict else getattr(package, "lines", None)
        if not isinstance(lines, (list, tuple)) or not lines:
            _reject("Измеренное место не содержит проверенные строки подбора", 409)
        in_package = set()
        for line in lines:
            pick_id = line.get("pick_id") if type(line) is dict else getattr(line, "pick_id", None)
            raw_qty = line.get("qty") if type(line) is dict else getattr(line, "qty", None)
            if type(raw_qty) not in (str, Decimal):
                _reject("Количество места повреждено", 409)
            try:
                qty = raw_qty if isinstance(raw_qty, Decimal) else Decimal(raw_qty)
            except (InvalidOperation, TypeError, ValueError):
                _reject("Количество места повреждено", 409)
            key = str(pick_id)
            if (key in in_package or key not in expected or not qty.is_finite() or qty <= 0
                    or max(0, -qty.as_tuple().exponent) > 6):
                _reject("Строка места не соответствует точности измерения подбора", 409)
            in_package.add(key)
            totals[key] = totals.get(key, Decimal("0")) + qty
    if totals != expected:
        _reject("Измеренные места должны точно покрывать все связанные подборы", 409)


async def validate_bound_pack_receipt(session, binding: DeliveryBindingRevision, picks) -> dict:
    """Validate the latest immutable Pack v2 receipt before a bound shipment."""
    proof_path = PhysicalOperation.result["physical_causal_v1"]["canonical_request"]["binding_revision_id"].as_string()
    operation = await session.scalar(select(PhysicalOperation).where(
        PhysicalOperation.organization_id == binding.organization_id,
        PhysicalOperation.warehouse_id == binding.warehouse_id,
        PhysicalOperation.kind == "pack_v2",
        proof_path == binding.id,
    ).order_by(PhysicalOperation.id.desc()).limit(1))
    if operation is None:
        _reject("Для связанной доставки отсутствует сохранённое измерение упаковки", 409)
    try:
        from modules.wms.item_identity import _OriginUnproven, _origin_validate_operation

        _origin_validate_operation({
            "id": operation.id,
            "organization_id": operation.organization_id,
            "warehouse_id": operation.warehouse_id,
            "operation_id": operation.operation_id,
            "kind": operation.kind,
            "request_hash": operation.request_hash,
            "result": operation.result,
        }, binding.organization_id)
    except (_OriginUnproven, TypeError, ValueError, KeyError, AttributeError):
        _reject("Квитанция измерения упаковки не прошла проверку происхождения", 409)
    result = operation.result
    causal = result.get("physical_causal_v1") if type(result) is dict else None
    request = causal.get("canonical_request") if type(causal) is dict else None
    package_proof = result.get("shipping_packages_v1") if type(result) is dict else None
    snapshot = binding_snapshot(binding)
    if (type(request) is not dict or request.get("binding_revision_id") != binding.id
            or request.get("source") != snapshot.get("order_ref")
            or request.get("container") != snapshot.get("container")
            or type(package_proof) is not dict
            or package_proof.get("binding_revision_id") != binding.id
            or package_proof.get("source") != request.get("source")
            or package_proof.get("container") != request.get("container")
            or type(package_proof.get("packages")) is not list
            or not package_proof["packages"]
            or type(package_proof.get("place_count")) is not int
            or isinstance(package_proof.get("place_count"), bool)
            or package_proof.get("place_count") != len(package_proof["packages"])):
        _reject("Квитанция упаковки не относится к текущей привязке", 409)
    expected = {pick.id: Decimal(pick.qty) for pick in picks}
    request_lines = request.get("lines")
    if type(request_lines) is not list:
        _reject("В квитанции упаковки отсутствуют строки подбора", 409)
    try:
        requested = {int(row["pick_id"]): Decimal(str(row["qty"])) for row in request_lines}
    except (KeyError, TypeError, ValueError, InvalidOperation):
        _reject("Строки квитанции упаковки повреждены", 409)
    if requested != expected:
        _reject("Квитанция упаковки не покрывает точные связанные подборы", 409)
    validate_pick_place_partition(picks, package_proof.get("packages"))
    return package_proof


def binding_snapshot(binding: DeliveryBindingRevision) -> dict:
    """Return only a self-consistent immutable binding projection."""
    snapshot = binding.snapshot
    if (type(snapshot) is not dict or binding.digest != _digest(snapshot)
            or type(snapshot.get("schema_version")) is not int or snapshot.get("schema_version") != 1
            or type(snapshot.get("binding_id")) is not str
            or snapshot.get("binding_id") != binding.id
            or type(snapshot.get("binding_key")) is not str
            or snapshot.get("binding_key") != binding.binding_key
            or type(snapshot.get("organization_id")) is not int
            or snapshot.get("organization_id") != binding.organization_id
            or type(snapshot.get("warehouse_id")) is not int
            or snapshot.get("warehouse_id") != binding.warehouse_id
            or type(snapshot.get("execution_id")) is not str
            or snapshot.get("execution_id") != binding.execution_id
            or type(snapshot.get("revision")) is not int or snapshot.get("revision") != binding.revision
            or not isinstance(snapshot.get("allocations"), list)
            or not snapshot["allocations"]):
        _reject("Квитанция привязки доставки повреждена")
    seen_tasks = set()
    for row in snapshot["allocations"]:
        if (type(row) is not dict or not isinstance(row.get("task_id"), str)
                or not isinstance(row.get("hold_id"), int) or isinstance(row.get("hold_id"), bool)
                or not isinstance(row.get("source_key"), str)
                or not isinstance(row.get("unit_id"), str) or not row.get("unit_id")
                or not isinstance(row.get("target_qty"), str)
                or not isinstance(row.get("remaining_at_binding_qty"), str)
                or row["task_id"] in seen_tasks):
            _reject("Состав привязки доставки повреждён")
        seen_tasks.add(row["task_id"])
        try:
            target = Decimal(row["target_qty"])
            remaining = Decimal(row["remaining_at_binding_qty"])
        except (InvalidOperation, ValueError):
            _reject("Количество привязки доставки повреждено")
        if (not target.is_finite() or not remaining.is_finite() or target <= 0
                or remaining < target):
            _reject("Количество привязки доставки повреждено")
    return {**snapshot, "snapshot_digest": binding.digest}


def _validated_binding_request(snapshot: dict, operation: PhysicalOperation):
    raw = snapshot.get("canonical_request")
    if type(raw) is not dict:
        _reject("В привязке отсутствует типизированный исходный запрос", 409)
    try:
        from modules.wms.physical_schemas import DeliveryBind

        request = DeliveryBind.model_validate(raw)
        canonical_request = request.model_dump(mode="json")
    except (ValidationError, TypeError, ValueError) as exc:
        raise HTTPException(409, "Типизированный запрос привязки повреждён") from exc
    if (canonical_request != raw
            or snapshot.get("operation_request_hash") != operation.request_hash
            or operation.request_hash != _digest(canonical_request)):
        _reject("Типизированный запрос не совпадает с квитанцией исходной операции", 409)

    invoice = snapshot.get("invoice")
    producer = snapshot.get("producer_source")
    if (type(invoice) is not dict or type(producer) is not dict
            or request.kind != "delivery_bind"
            or str(request.operation_id) != snapshot.get("binding_id")
            or str(request.execution_id) != snapshot.get("execution_id")
            or type(snapshot.get("warehouse_id")) is not int
            or request.warehouse_id != snapshot.get("warehouse_id")
            or not _same_typed(request.document_id, invoice.get("document_id"))
            or not _same_typed(request.expected_version, invoice.get("version"))
            or not _same_typed(request.expected_content_sha256, invoice.get("content_sha256"))
            or not _same_typed(request.expected_reservation_digest, invoice.get("reservation_digest"))
            or not _same_typed(request.source_kind, producer.get("kind"))
            or not _same_typed(request.source_key, producer.get("key"))
            or not _same_typed(request.source_revision, producer.get("revision"))
            or not _same_typed(request.expected_intent_digest, producer.get("intent_digest"))
            or not _same_typed(request.expected_source_state_sha256, producer.get("state_sha256"))
            or request.previous_revision_id is not None
            or request.evidence != snapshot.get("evidence")
            or len(request.allocations) != len(snapshot.get("allocations", ()))):
        _reject("Запрос привязки не совпадает с подтверждёнными исходными полями", 409)
    for request_row, snapshot_row in zip(request.allocations, snapshot["allocations"]):
        try:
            snapshot_target = Decimal(snapshot_row["target_qty"])
        except (KeyError, TypeError, InvalidOperation, ValueError):
            _reject("Цель типизированного запроса повреждена", 409)
        if str(request_row.task_id) != snapshot_row.get("task_id") or request_row.qty != snapshot_target:
            _reject("Цель типизированного запроса не совпадает с привязкой", 409)
    return request


def validate_bound_task_identity(task, binding, snapshot, allocation, hold, unit) -> None:
    """Require the live task, reserve and original stock unit to match one allocation."""
    if (type(snapshot) is not dict
            or type(binding.id) is not str
            or not _same_typed(snapshot.get("binding_id"), binding.id)
            or not _same_typed(snapshot.get("warehouse_id"), binding.warehouse_id)
            or type(allocation) is not dict
            or not _same_typed(task.id, allocation.get("task_id"))
            or not _same_typed(task.binding_revision_id, binding.id)
            or not _same_typed(task.kind, "pick")
            or not _same_typed(task.warehouse_id, binding.warehouse_id)
            or not _same_typed(task.hold_id, allocation.get("hold_id"))
            or not _same_typed(task.unit_id, allocation.get("unit_id"))
            or not _same_typed(task.source, allocation.get("source_key"))
            or not _same_typed(hold.id, allocation.get("hold_id"))
            or not _same_typed(hold.unit_id, allocation.get("unit_id"))
            or not _same_typed(hold.source, allocation.get("source_key"))
            or not _same_typed(hold.order_ref, snapshot.get("order_ref"))
            or not _same_typed(hold.container, snapshot.get("container"))
            or not _same_typed(hold.warehouse_id, snapshot.get("warehouse_id"))
            or not _same_typed(unit.id, allocation.get("unit_id"))
            or not _same_typed(unit.warehouse_id, snapshot.get("warehouse_id"))):
        _reject("Задание, резерв или складской остаток не совпадает с исходной привязкой", 409)


def _validate_bound_pick_holds(picks, allocation) -> None:
    for pick in picks:
        if not _same_typed(pick.hold_id, allocation["hold_id"]):
            _reject("Связанный подбор относится к другому резерву", 409)


async def load_bound_task_identity(session, task: PhysicalTask, snapshot: dict | None = None):
    """Load and verify the live identities fixed by a delivery allocation."""
    if task.binding_revision_id is None:
        _reject("Обычное задание не имеет неизменяемой цели доставки", 409)
    binding = await session.get(DeliveryBindingRevision, task.binding_revision_id)
    operation = await session.get(PhysicalOperation, binding.operation_pk) if binding is not None else None
    if binding is None or operation is None:
        _reject("Исходная привязка задания подбора повреждена", 409)
    verified_snapshot = validate_binding_row(binding, operation)
    if (snapshot is not None and (type(snapshot) is not dict
            or snapshot.get("snapshot_digest") != verified_snapshot.get("snapshot_digest")
            or snapshot.get("allocations") != verified_snapshot.get("allocations"))):
        _reject("Снимок задания не совпадает с исходной привязкой", 409)
    allocation = next((row for row in verified_snapshot["allocations"]
        if _same_typed(row.get("task_id"), task.id)), None)
    if allocation is None:
        _reject("Задание отсутствует в исходной привязке доставки", 409)
    hold = await session.get(PhysicalHold, allocation["hold_id"])
    unit = await session.get(StockUnit, allocation["unit_id"])
    if hold is None or unit is None:
        _reject("Резерв или складской остаток исходной привязки не найден", 409)
    validate_bound_task_identity(task, binding, verified_snapshot, allocation, hold, unit)
    return binding, verified_snapshot, allocation, hold, unit


async def bound_task_remaining(session, task: PhysicalTask, snapshot: dict | None = None) -> Decimal:
    """Return immutable target minus actual active pick rows for one bound task."""
    _binding, _verified_snapshot, row, _hold, _unit = await load_bound_task_identity(
        session, task, snapshot,
    )
    active = (await session.scalars(select(PickedAllocation).where(
        PickedAllocation.task_id == task.id,
        PickedAllocation.binding_revision_id == task.binding_revision_id,
        PickedAllocation.canceled.is_(False),
    ).order_by(PickedAllocation.id))).all()
    _validate_bound_pick_holds(active, row)
    actual = sum((Decimal(pick.qty) for pick in active), Decimal("0"))
    return bound_target_remaining(Decimal(row["target_qty"]), actual)


def seal_binding_snapshot(snapshot: dict) -> tuple[dict, str, dict]:
    if type(snapshot) is not dict or "snapshot_digest" in snapshot:
        _reject("Снимок привязки доставки имеет неверный формат")
    sealed = json.loads(_canonical(snapshot))
    sealed_digest = _digest(sealed)
    return sealed, sealed_digest, {**sealed, "snapshot_digest": sealed_digest}


def validate_binding_row(binding: DeliveryBindingRevision, operation: PhysicalOperation) -> dict:
    snapshot = binding_snapshot(binding)
    result = operation.result
    if (operation.id != binding.operation_pk or operation.operation_id != binding.id
            or operation.organization_id != binding.organization_id
            or operation.warehouse_id != binding.warehouse_id
            or operation.kind != "delivery_bind"
            or type(result) is not dict
            or result.get("kind") != "delivery_bind"
            or result.get("operation_id") != binding.id
            or result.get("warehouse_id") != binding.warehouse_id
            or result.get("binding_snapshot") != snapshot):
        _reject("Привязка доставки не совпадает с исходной операцией")
    _validated_binding_request(snapshot, operation)
    return snapshot


def _verified_intent(source_row: dict, *, source_kind, source_key, source_revision,
                     intent_digest: str, source_state_sha256: str) -> tuple[dict, dict]:
    if type(source_row) is not dict or source_row.get("fulfillment_allowed") is not True:
        _reject("Исполнение доставки не разрешено проверенным источником")
    source = source_row.get("source")
    if type(source) is not dict:
        _reject("Проверенный источник доставки повреждён")
    for key, expected in (("kind", source_kind), ("key", source_key), ("revision", source_revision)):
        if type(source.get(key)) is not type(expected) or source.get(key) != expected:
            _reject("Версия исходного исполнения доставки изменилась")
    if (source_row.get("intent_digest") != intent_digest
            or source_row.get("source_state_sha256") != source_state_sha256):
        _reject("Состояние исходного исполнения доставки изменилось")
    envelope = source_row.get("envelope")
    payload = envelope.get("payload") if type(envelope) is dict else None
    intent = payload.get("intent") if type(payload) is dict else None
    if type(intent) is not dict:
        _reject("Проверенное исполнение не содержит намерение доставки")
    carrier_code = intent.get("carrier_code")
    carrier_name = intent.get("carrier_name")
    normalized = {
        str(value).strip().casefold()
        for value in (carrier_code, carrier_name)
        if isinstance(value, str) and value.strip()
    }
    if not normalized.intersection({"autolight", "avtolight", "автолайт"}):
        _reject("Приоритет доступен только для проверенного Autolight-исполнения")
    raw_date = intent.get("pickup_date")
    try:
        if type(raw_date) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
            raise ValueError("pickup_date_format")
        pickup = date.fromisoformat(raw_date)
    except ValueError as exc:
        _reject("Дата забора в источнике доставки не подтверждена")
    cutoff = datetime.combine(pickup, time(15, 0), ZoneInfo("Europe/Minsk"))
    return {
        "carrier_code": carrier_code,
        "carrier_name": carrier_name,
        "pickup_date": pickup.isoformat(),
        "priority_cutoff_at": cutoff.isoformat(timespec="seconds"),
    }, source


def exact_producer_alias(rows, *, execution_id, source_kind, source_key, source_revision) -> dict:
    """Select one producer alias by its complete immutable source identity."""
    matches = []
    for row in rows:
        if type(row) is not dict or type(row.get("execution_id")) is not str:
            continue
        source = row.get("source")
        if type(source) is not dict:
            continue
        identity = (
            (row.get("execution_id"), execution_id),
            (source.get("kind"), source_kind),
            (source.get("key"), source_key),
            (source.get("revision"), source_revision),
        )
        if all(_same_typed(actual, expected) for actual, expected in identity):
            matches.append(row)
    if len(matches) != 1:
        _reject("Точный алиас исполнения доставки не уникален в подтверждённом счёте", 409)
    return matches[0]


async def _prepare_live_source_context(
    session,
    stock,
    organization_id: int,
    *,
    document_id: int,
    version: int,
    content_sha256: str,
    execution_id: str,
    source_kind,
    source_key: str,
    source_revision,
    intent_digest: str,
    source_state_sha256: str,
    expected_reservation_digest: str,
    mobile_fulfillment=None,
):
    services = getattr(stock, "services", None)
    logistics = getattr(services, "logistics", None)
    sales_source = getattr(services, "sales_source", None)
    user = getattr(stock, "user", None)
    if logistics is None or sales_source is None or user is None:
        _reject("Проверенные источники исполнения и продаж недоступны", 409)
    exact_invoice = {
        "organization_id": organization_id,
        "document_id": document_id,
        "expected_version": version,
        "expected_content_sha256": content_sha256,
    }
    try:
        with session.no_autoflush:
            sales = await sales_source.invoice_shipping_source(
                session,
                document_id,
                organization_id=organization_id,
                expected_version=version,
                expected_content_sha256=content_sha256,
                operation="fulfill",
            )
            from modules.wms.invoice_reservations import _locked_reservation, _remaining

            reservation, allocations = await _locked_reservation(session, organization_id, sales)
            latest, state, tasks, quantities, before = await _remaining(session, reservation, allocations)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise HTTPException(409, "Резерв счёта не прошёл повторную проверку") from exc
    reservation_snapshot = reservation.snapshot
    if (reservation.organization_id != organization_id
            or reservation.document_id != document_id
            or reservation.digest != expected_reservation_digest
            or type(reservation_snapshot) is not dict
            or reservation_snapshot.get("version") != version
            or reservation_snapshot.get("content_sha256") != content_sha256):
        _reject("Версия подтверждённого резерва счёта изменилась")
    try:
        with session.no_autoflush:
            if mobile_fulfillment is None:
                prepared = await logistics.prepare_warehouse_invoice_fulfillment_snapshot(
                    session, exact_invoice=exact_invoice, user=user,
                )
            else:
                prepared = await logistics.prepare_mobile_invoice_fulfillment_snapshot(
                    session, exact_invoice=exact_invoice, user=user, authority=mobile_fulfillment,
                )
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise HTTPException(409, "Проверенное исполнение счёта недоступно") from exc
    return {
        "logistics": logistics,
        "prepared_fulfillment": prepared,
        "exact_invoice": exact_invoice,
        "execution_id": str(execution_id),
        "source_kind": source_kind,
        "source_key": source_key,
        "source_revision": source_revision,
        "intent_digest": intent_digest,
        "source_state_sha256": source_state_sha256,
        "expected_reservation_digest": expected_reservation_digest,
        "sales_source": sales,
        "reservation": reservation,
        "reservation_snapshot_digest": _digest(reservation_snapshot),
        "allocations": allocations,
        "latest_versions": latest,
        "event_state": state,
        "sales_tasks": tasks,
        "sales_quantities": quantities,
        "fulfillment_before": before,
        "fulfillment_before_digest": _digest(before),
    }


async def _collect_live_source_context(session, context):
    if context.get("source_context_collected") is True:
        _reject("Проверенный снимок исполнения уже использован", 409)
    try:
        with session.no_autoflush:
            fulfillment = await context["logistics"].collect_prepared_invoice_fulfillment_snapshot(
                session, context["prepared_fulfillment"],
            )
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise HTTPException(409, "Проверенное исполнение счёта недоступно") from exc
    producer_sources = fulfillment.get("producer_sources") if type(fulfillment) is dict else None
    rows = producer_sources.get("sources") if type(producer_sources) is dict else None
    if type(rows) is not list:
        _reject("Список проверенных исполнений счёта повреждён")
    source_row = exact_producer_alias(
        rows,
        execution_id=context["execution_id"],
        source_kind=context["source_kind"],
        source_key=context["source_key"],
        source_revision=context["source_revision"],
    )
    intent, source = _verified_intent(
        source_row,
        source_kind=context["source_kind"],
        source_key=context["source_key"],
        source_revision=context["source_revision"],
        intent_digest=context["intent_digest"],
        source_state_sha256=context["source_state_sha256"],
    )
    context.update({
        "fulfillment": fulfillment,
        "execution": source_row,
        "source": source,
        "intent": intent,
        "source_context_collected": True,
    })
    return context


async def prepare_delivery_binding(session, stock, organization_id: int, data):
    """Prepare producer and invoice provenance without collecting the producer yet."""
    context = await _prepare_live_source_context(
        session, stock, organization_id,
        document_id=data.document_id,
        version=data.expected_version,
        content_sha256=data.expected_content_sha256,
        execution_id=str(data.execution_id),
        source_kind=data.source_kind,
        source_key=data.source_key,
        source_revision=data.source_revision,
        intent_digest=data.expected_intent_digest,
        source_state_sha256=data.expected_source_state_sha256,
        expected_reservation_digest=data.expected_reservation_digest,
    )
    context["document_id"] = data.document_id
    context["version"] = data.expected_version
    context["content_sha256"] = data.expected_content_sha256
    context["reservation_digest"] = data.expected_reservation_digest
    return context


async def collect_delivery_binding(session, context: dict) -> dict:
    """Collect prepared producer state after all source preparations are complete."""
    context = await _collect_live_source_context(session, context)
    if (context.get("reservation_digest") != context.get("expected_reservation_digest")
            or context.get("document_id") != context["exact_invoice"].get("document_id")
            or context.get("version") != context["exact_invoice"].get("expected_version")
            or context.get("content_sha256") != context["exact_invoice"].get("expected_content_sha256")):
        _reject("Подготовленный запрос больше не совпадает с точным счётом", 409)
    return context


async def prepare_live_binding(
    session, stock, binding: DeliveryBindingRevision, *, mobile_fulfillment=None,
) -> dict:
    """Acquire verified source locks and read reservation state before WMS locks."""
    snapshot = binding_snapshot(binding)
    invoice = snapshot.get("invoice")
    producer = snapshot.get("producer_source")
    if type(invoice) is not dict or type(producer) is not dict:
        _reject("В снимке привязки доставки отсутствуют исходные ключи")
    context = await _prepare_live_source_context(
        session, stock, binding.organization_id,
        document_id=invoice.get("document_id"),
        version=invoice.get("version"),
        content_sha256=invoice.get("content_sha256"),
        execution_id=binding.execution_id,
        source_kind=producer.get("kind"),
        source_key=producer.get("key"),
        source_revision=producer.get("revision"),
        intent_digest=producer.get("intent_digest"),
        source_state_sha256=producer.get("state_sha256"),
        expected_reservation_digest=invoice.get("reservation_digest"),
        mobile_fulfillment=mobile_fulfillment,
    )
    context["binding"] = binding
    return context


async def collect_live_binding(session, stock, context: dict) -> dict:
    """Finish prepared producer collection after source/reservation checks."""
    binding = context.get("binding")
    if not isinstance(binding, DeliveryBindingRevision):
        _reject("Подготовленный контекст не связан с привязкой доставки", 409)
    context = await _collect_live_source_context(session, context)
    snapshot = binding_snapshot(binding)
    invoice = snapshot.get("invoice")
    if (type(invoice) is not dict
            or context["reservation_snapshot_digest"] != invoice.get("reservation_snapshot_digest")
            or context["fulfillment_before_digest"] != snapshot.get("fulfillment_before_digest")
            or context["intent"] != snapshot.get("priority")):
        _reject("Актуальное исполнение больше не совпадает с привязкой")
    context["snapshot"] = snapshot
    return snapshot


async def validate_binding_replay(session, operation: PhysicalOperation) -> dict:
    binding = await session.get(DeliveryBindingRevision, operation.operation_id)
    if binding is None:
        _reject("Квитанция привязки доставки не имеет постоянной записи")
    return validate_binding_row(binding, operation)


async def prelock_command_binding(
    session, stock, organization_id: int, data, actor: str, *, mobile_authority=None,
):
    """Resolve and prepare a selected bound task before WMS locks; defer collection."""
    task = None
    binding_id = None
    if data.kind == "task_claim":
        task = await session.get(PhysicalTask, str(data.task_id))
    elif data.kind == "pick":
        if data.task_id is not None:
            task = await session.get(PhysicalTask, str(data.task_id))
        else:
            candidates = (await session.scalars(select(PhysicalTask).where(
                PhysicalTask.hold_id == data.hold_id,
                PhysicalTask.kind == "pick",
                PhysicalTask.status == "claimed",
                PhysicalTask.assignee == actor,
            ).order_by(PhysicalTask.id))).all()
            if len(candidates) == 1:
                task = candidates[0]
    elif data.kind == "ship":
        pick_ids = [line.pick_id for line in data.lines]
        picks = (await session.scalars(select(PickedAllocation).where(
            PickedAllocation.id.in_(pick_ids),
        ).order_by(PickedAllocation.id))).all() if pick_ids else []
        binding_ids = {row.binding_revision_id for row in picks if row.binding_revision_id is not None}
        if binding_ids:
            if len(binding_ids) != 1 or len(picks) != len(pick_ids) or any(row.binding_revision_id is None for row in picks):
                _reject("Отгрузка не может смешивать связанные и обычные подборы")
            binding_id = next(iter(binding_ids))
            binding = await session.get(DeliveryBindingRevision, str(binding_id))
            if (binding is None or binding.organization_id != organization_id
                    or binding.warehouse_id != data.warehouse_id):
                _reject("Привязка отгружаемых подборов повреждена")
            binding_id = binding.id
    if data.kind == "pack_v2":
        binding_id = data.binding_revision_id
    if task is not None and task.binding_revision_id is not None:
        binding_id = task.binding_revision_id
    if binding_id is None:
        return None
    binding = await session.get(DeliveryBindingRevision, str(binding_id))
    if (binding is None or binding.organization_id != organization_id
            or binding.warehouse_id != data.warehouse_id):
        _reject("Задание не связано с проверенной привязкой доставки", 409)
    operation = await session.get(PhysicalOperation, binding.operation_pk)
    if operation is None:
        _reject("Квитанция исходной привязки доставки не найдена")
    validate_binding_row(binding, operation)
    if task is not None and task.binding_revision_id != binding.id:
        _reject("Идентификатор привязки задания изменился", 409)
    capability = None
    if mobile_authority is not None:
        if (getattr(mobile_authority, "purpose", None) != "pack" or data.kind != "pack_v2"
                or str(data.binding_revision_id) != binding.id or data.shipping_packages is None):
            from core.services.logistics import MobileFulfillmentAuthorityDenied

            raise MobileFulfillmentAuthorityDenied()
        from modules.wms.mobile_access import make_mobile_fulfillment_read_capability

        snapshot = binding_snapshot(binding)
        invoice = snapshot.get("invoice")
        if type(invoice) is not dict:
            _reject("Снимок привязки доставки не содержит счёт", 409)
        exact_invoice = {
            "organization_id": organization_id,
            "document_id": invoice.get("document_id"),
            "expected_version": invoice.get("version"),
            "expected_content_sha256": invoice.get("content_sha256"),
        }
        capability = make_mobile_fulfillment_read_capability(
            session, stock, mobile_authority,
            warehouse_id=data.warehouse_id, binding_revision_id=binding.id,
            exact_invoice=exact_invoice,
        )
    return await prepare_live_binding(
        session, stock, binding, mobile_fulfillment=capability,
    )


async def validate_bound_shipment(session, binding: DeliveryBindingRevision, data) -> dict:
    snapshot = binding_snapshot(binding)
    allocations = {row["task_id"]: Decimal(row["target_qty"]) for row in snapshot["allocations"]}
    for allocation in snapshot["allocations"]:
        task = await session.get(PhysicalTask, allocation["task_id"])
        if task is None:
            _reject("Задание исходной привязки доставки не найдено", 409)
        await bound_task_remaining(session, task, snapshot)
    picks = (await session.scalars(select(PickedAllocation).where(
        PickedAllocation.binding_revision_id == binding.id,
        PickedAllocation.canceled.is_(False),
    ).order_by(PickedAllocation.id))).all()
    by_task: dict[str, Decimal] = {}
    for pick in picks:
        if pick.task_id is None:
            _reject("Связанный подбор не содержит идентификатор задания")
        by_task[pick.task_id] = by_task.get(pick.task_id, Decimal("0")) + pick.qty
        if not pick.packed or pick.shipped != 0:
            _reject("Связанную доставку можно отгрузить только целиком после упаковки")
    validate_task_pick_totals(allocations, by_task)
    lines = {line.pick_id: line for line in data.lines}
    if set(lines) != {pick.id for pick in picks}:
        _reject("Отгрузка должна содержать все активные подборы привязки")
    for pick in picks:
        line = lines[pick.id]
        if line.qty != pick.qty or pick.binding_revision_id != binding.id:
            _reject("Отгрузка не может частично менять измеренную привязку")
        hold = await session.get(PhysicalHold, pick.hold_id)
        if (hold is None or hold.warehouse_id != binding.warehouse_id
                or hold.order_ref != snapshot.get("order_ref")
                or hold.container != snapshot.get("container")
                or data.source != hold.order_ref):
            _reject("Подбор принадлежит другому заказу или контейнеру")
    await validate_bound_pack_receipt(session, binding, picks)
    return snapshot
