"""Physical operations inside the caller's transaction; never commits or calls 1C.

Lock order: accounting organization -> warehouse controls by ID -> addressed
units. The warehouse lock also covers absent rows, first reserves and counts.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import aliased

from core.domain.models import Sku
from modules.wms.models import Location, ReservationVersion, StockMovement, WarehouseDirectory
from modules.wms.physical_models import (
    AssemblyConsumption,
    AssemblyExecution,
    AssemblyInputLink,
    AssemblyOutputIdentity,
    CountLocationLock,
    DeliveryBindingRevision,
    CurrentStockStateError,
    ExchangeJob,
    Packaging,
    PhysicalCount,
    PhysicalCountLine,
    PhysicalHold,
    PhysicalLedger,
    PhysicalLocation,
    PhysicalOperation,
    PhysicalTask,
    PickedAllocation,
    PickRule,
    StockUnit,
    StoragePolicy,
    WarehouseControl,
    require_current_in_transit,
    require_current_on_hand,
)


def today():
    return datetime.now(ZoneInfo("Europe/Minsk")).date()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


RESPONSIBLE_KIND = "warehouse_responsible_set"
RESPONSIBLE_PRIVATE = "warehouse_responsible_v1"


def responsible_operation_indicator(kind, result):
    return kind == RESPONSIBLE_KIND or (
        isinstance(result, dict)
        and (result.get("kind") == RESPONSIBLE_KIND or RESPONSIBLE_PRIVATE in result)
    )


def responsible_recorded_receipt(operation):
    """Validate the closed historical metadata, never against today's candidate."""
    from pydantic import ValidationError

    from modules.wms.physical_schemas import WarehouseResponsibleReceipt

    try:
        data = WarehouseResponsibleReceipt.model_validate(operation.result)
        canonical_result = data.model_dump(mode="json")
        proof = data.warehouse_responsible_v1
        request_hash = digest({
            "organization_id": data.organization_id, "warehouse_id": data.warehouse_id,
            "actor_user_id": proof.setter_user_id, "actor_sub": proof.setter_subject,
            "body": proof.request.model_dump(mode="json"),
        })
        if (
            canonical_result != operation.result or operation.kind != RESPONSIBLE_KIND
            or operation.organization_id != data.organization_id
            or operation.warehouse_id != data.warehouse_id
            or operation.operation_id != str(data.operation_id)
            or operation.actor != proof.setter_subject or operation.request_hash != request_hash
        ):
            raise ValueError("Responsible receipt identity is inconsistent")
    except (ValidationError, ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(409, "Квитанция назначения ответственного повреждена") from exc
    return data


def public_operation_result(result, *, operation_kind=None):
    """Hide only server-private causal evidence from ERP receipt responses."""
    if not isinstance(result, dict):
        reject("Повреждённый результат операции", 409)
    from modules.wms.transfer_loss_protocol import loss_operation_indicator

    if loss_operation_indicator(operation_kind, result):
        reject("Расхождение доступно только через проверенный специализированный интерфейс", 404)
    kind = result.get("kind") if operation_kind is None else operation_kind
    if responsible_operation_indicator(kind, result):
        from pydantic import ValidationError

        from modules.wms.physical_schemas import WarehouseResponsibleReceipt

        try:
            receipt = WarehouseResponsibleReceipt.model_validate(result)
            if kind != RESPONSIBLE_KIND or result != receipt.model_dump(mode="json"):
                raise ValueError("Responsible receipt kind or canonical content differs")
            return receipt.model_dump(mode="json", exclude={RESPONSIBLE_PRIVATE})
        except (ValidationError, ValueError, TypeError) as exc:
            raise HTTPException(409, "Квитанция назначения ответственного повреждена") from exc
    from modules.wms.physical_capacity import (
        measurement_operation_indicator,
        measurement_public_result,
    )
    from modules.wms.placement_models import work_operation_indicator
    if work_operation_indicator(kind, result):
        from modules.wms.placement_service import (
            WORK_KIND,
            WORK_PRIVATE,
            validate_work_envelope,
            work_ack,
        )
        if kind != WORK_KIND or result.get("kind") != WORK_KIND:
            reject("work_receipt_kind_mismatch", 409)
        proof = result.get(WORK_PRIVATE)
        if type(proof) is not dict or type(proof.get("policy_row")) is not dict:
            reject("work_private_receipt_missing", 409)
        try:
            validate_work_envelope({key: proof[key] for key in ("organization_id", "warehouse_id", "operation_id", "kind", "request_hash")} | {"actor": proof["approver"]["sub"], "result": result}, proof["policy_row"])
        except (ValueError, KeyError, TypeError) as exc:
            reject(str(exc), 409)
        return work_ack(proof["policy_row"])
    if measurement_operation_indicator(kind, result):
        try:
            return measurement_public_result(result, kind)
        except ValueError as exc:
            reject(str(exc), 409)
    assembly_keys = {"assembly_bind_proof_v1": "assembly_bind", "assembly_consume_proof_v1": "assembly_consume",
        "assembly_output_v1": "assembly_output", "assembly_qc_v1": "assembly_qc", "assembly_return_v1": "assembly_return"}
    assembly_kind = kind in {*assembly_keys.values(), "assembly_kit_move", "assembly_unused_return"}
    if (assembly_kind or any(key in result for key in assembly_keys)) and result.get("kind") != kind:
        reject("Тип сохранённого результата не совпадает с операцией", 409)
    if any(key in result and kind != expected for key, expected in assembly_keys.items()):
        reject("Серверное доказательство не соответствует типу операции", 409)
    if kind in {"assembly_output", "assembly_qc", "assembly_return"}:
        from modules.wms.assembly_quality import physical_assembly_public_result

        return physical_assembly_public_result(result)
    return {key: value for key, value in result.items() if key not in {
        "physical_causal_v1", "native_receipt_owner_v1", "native_pack_owner_v1",
        "assembly_bind_proof_v1", "assembly_consume_proof_v1",
    }}


def native_receipt_owner(result):
    """Absent means historical v1; present malformed metadata never falls back."""
    if not isinstance(result, dict):
        reject("Запись не найдена", 404)
    if "native_receipt_owner_v1" not in result:
        return None
    owner = result["native_receipt_owner_v1"]
    if (not isinstance(owner, dict) or set(owner) != {"protocol", "user_id", "sub"}
        or type(owner["protocol"]) is not int or owner["protocol"] != 1
        or type(owner["user_id"]) is not int or not 1 <= owner["user_id"] <= 2147483647
        or not isinstance(owner["sub"], str) or not owner["sub"].strip()
        or len(owner["sub"]) > 256):
        reject("Запись не найдена", 404)
    return owner


def require_native_receipt_owner(result, original_user_id, original_sub):
    owner = native_receipt_owner(result)
    if owner is not None and (
        owner["user_id"] != original_user_id or owner["sub"] != original_sub
    ):
        reject("Запись не найдена", 404)
    return owner


def native_pack_owner(result, authority=None, proof=None):
    """Validate the private measured-pack owner marker; absence is legacy only."""
    if not isinstance(result, dict):
        reject("Запись не найдена", 404)
    if "native_pack_owner_v1" not in result:
        return None
    owner = result["native_pack_owner_v1"]
    try:
        device_id = str(UUID(owner["device_id"])) if isinstance(owner, dict) else None
        grant_id = str(UUID(owner["grant_id"])) if isinstance(owner, dict) else None
    except (ValueError, TypeError, AttributeError, KeyError):
        reject("Запись не найдена", 404)
    if (not isinstance(owner, dict) or set(owner) != {"protocol", "user_id", "sub", "device_id", "grant_id"}
            or type(owner["protocol"]) is not int or owner["protocol"] != 1
            or type(owner["user_id"]) is not int or not 1 <= owner["user_id"] <= 2147483647
            or not isinstance(owner["sub"], str) or not owner["sub"].strip() or len(owner["sub"]) > 256
            or device_id != owner["device_id"] or grant_id != owner["grant_id"]):
        reject("Запись не найдена", 404)
    if authority is not None:
        identity = getattr(authority, "identity", None)
        if (identity is None or owner["user_id"] != identity.original_user_id
                or owner["sub"] != identity.original_sub or proof is None
                or owner["device_id"] != proof.device_id or owner["grant_id"] != proof.grant_id):
            reject("Запись не найдена", 404)
    return owner


def native_pack_v1_projection(operation):
    """Return the saved measured proof only after exact immutable receipt validation."""
    from modules.wms.item_identity import (
        _OriginUnproven, _origin_operation_snapshot, _origin_validate_operation,
    )
    from modules.wms.physical_delivery import normalize_legacy_optional_fields
    from modules.wms.physical_schemas import PackV2

    try:
        result = operation.result
        owner = native_pack_owner(result)
        if owner is None or operation.kind != "pack_v2" or result.get("kind") != "pack_v2":
            raise ValueError("Measured mobile pack owner is missing")
        container = result.get("container")
        if (type(container) is not str or not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,23}", container)
                or type(result.get("packed")) is not bool or result.get("packed") is not True
                or type(result.get("protocol")) is not int or result.get("protocol") != 2):
            raise ValueError("Measured pack result marker is not canonical")
        _origin_validate_operation(_origin_operation_snapshot(operation), operation.organization_id)
        causal = result.get("physical_causal_v1")
        canonical_request = causal.get("canonical_request") if isinstance(causal, dict) else None
        if type(canonical_request) is not dict:
            raise ValueError("Canonical pack request is missing")
        data = PackV2.model_validate(canonical_request)
        request = normalize_legacy_optional_fields(data.model_dump(mode="json"), "pack_v2")
        expected = data.shipping_package_proof()
        proof = result.get("shipping_packages_v1")
        binding_id = str(data.binding_revision_id) if data.binding_revision_id is not None else None
        if (expected is None or binding_id is None
                or canonical(canonical_request) != canonical(request)
                or canonical_request.get("binding_revision_id") != binding_id
                or operation.operation_id != str(data.operation_id)
                or operation.warehouse_id != data.warehouse_id
                or container != data.container
                or type(result.get("binding_revision_id")) is not str
                or result.get("binding_revision_id") != binding_id
                or operation.actor != owner["sub"]
                or operation.request_hash != digest(request)
                or type(proof) is not dict
                or type(proof.get("binding_revision_id")) is not str
                or proof.get("binding_revision_id") != binding_id
                or canonical(proof) != canonical(expected)):
            raise ValueError("Saved measured proof differs from the exact bound pack request")
        return expected
    except (HTTPException, _OriginUnproven, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise HTTPException(409, "Квитанция упаковки не подтверждена") from exc


def native_receipt_v2_projection(row):
    """Validate immutable receipt/request correspondence, never current unit facts."""
    from modules.wms.physical_schemas import Receive

    result = row.result
    owner = native_receipt_owner(result)
    if owner is None:
        return None
    binding_keys = {"protocol", "organization_id", "warehouse_id", "source_ref", "receipt_id",
        "version", "position", "source_snapshot", "sku_id", "sku_code", "unit", "batch_id",
        "lot_no", "coefficient", "allocated_qty"}
    snapshot_keys = {"organization_id", "receipt_id", "version", "source", "warehouse_id",
        "warehouse", "warehouse_title", "lines"}
    line_keys = {"source_line", "position", "sku_id", "sku", "unit", "lot", "quantity"}
    operational = {"id", "warehouse_id", "location_id", "sku_code", "batch_id", "quality",
        "package_state", "container", "qty", "reserved", "free", "blocked_reason",
        "characteristic", "purpose", "packaging_id", "arrival_source"}

    def integer(value):
        return type(value) is int and 1 <= value <= 2147483647

    def text(value, limit):
        return isinstance(value, str) and bool(value.strip()) and len(value) <= limit

    def quantity(value, source=False):
        pattern = (r"(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{0,5}[1-9])?"
                   if source else r"(?:0|[1-9][0-9]{0,13})(?:\.[0-9]{1,6})?")
        if not isinstance(value, str) or re.fullmatch(pattern, value) is None or Decimal(value) <= 0:
            raise ValueError("receipt_quantity")
        return Decimal(value)

    try:
        if (not isinstance(result, dict) or set(result) != {"operation_id", "kind", "warehouse_id",
            "unit", "source_ref", "native_metadata", "native_receipt_source_v1", "physical_causal_v1",
            "native_receipt_owner_v1"} or row.kind != "receive" or result["kind"] != "receive"
            or result["operation_id"] != row.operation_id or not _origin_uuid_text(row.operation_id)
            or not integer(result["warehouse_id"]) or result["warehouse_id"] != row.warehouse_id
            or not integer(row.warehouse_id)
            or not integer(row.organization_id) or owner["sub"] != row.actor):
            raise ValueError("receipt_scope")
        binding = result["native_receipt_source_v1"]
        if not isinstance(binding, dict) or set(binding) != binding_keys:
            raise ValueError("receipt_binding")
        if any(not integer(binding[key]) for key in ("protocol", "organization_id", "warehouse_id",
            "receipt_id", "version", "position", "sku_id", "batch_id")):
            raise ValueError("receipt_identity")
        source_ref = f"procurement:receipt:{binding['receipt_id']}:{binding['version']}:{binding['position']}"
        if (binding["protocol"] != 1 or binding["organization_id"] != row.organization_id
            or binding["warehouse_id"] != row.warehouse_id or binding["source_ref"] != source_ref
            or result["source_ref"] != source_ref or binding["coefficient"] != "1"
            or not text(binding["sku_code"], 200) or not text(binding["unit"], 32)
            or not text(binding["lot_no"], 200)):
            raise ValueError("receipt_mapping")
        amount = quantity(binding["allocated_qty"], source=True)
        snapshot = binding["source_snapshot"]
        if (not isinstance(snapshot, dict) or set(snapshot) != snapshot_keys
            or snapshot["organization_id"] != row.organization_id or not integer(snapshot["organization_id"])
            or snapshot["warehouse_id"] != row.warehouse_id or not integer(snapshot["warehouse_id"])
            or snapshot["receipt_id"] != binding["receipt_id"] or not integer(snapshot["receipt_id"])
            or snapshot["version"] != binding["version"] or not integer(snapshot["version"])
            or snapshot["source"] != f"procurement:receipt:{binding['receipt_id']}"
            or not text(snapshot["warehouse"], 200) or not text(snapshot["warehouse_title"], 255)
            or not isinstance(snapshot["lines"], list) or not 1 <= len(snapshot["lines"]) <= 300):
            raise ValueError("receipt_snapshot")
        for index, line in enumerate(snapshot["lines"], 1):
            if (not isinstance(line, dict) or set(line) != line_keys or not integer(line["position"])
                or line["position"] != index or not integer(line["sku_id"])
                or line["source_line"] != f"procurement:receipt:{binding['receipt_id']}:{binding['version']}:{index}"
                or not text(line["sku"], 200) or not text(line["unit"], 32) or not text(line["lot"], 200)):
                raise ValueError("receipt_line")
            quantity(line["quantity"], source=True)
        if binding["position"] > len(snapshot["lines"]):
            raise ValueError("receipt_position")
        selected = snapshot["lines"][binding["position"] - 1]
        if (selected["source_line"] != source_ref or selected["sku_id"] != binding["sku_id"]
            or selected["sku"] != binding["sku_code"] or selected["unit"] != binding["unit"]
            or selected["lot"] != binding["lot_no"] or amount > quantity(selected["quantity"], source=True)):
            raise ValueError("receipt_selected")
        causal = result["physical_causal_v1"]
        if (not isinstance(causal, dict) or set(causal) != {"schema_version", "canonical_request", "births",
            "count_basis", "prior_frontiers"} or type(causal["schema_version"]) is not int
            or causal["schema_version"] != 2):
            raise ValueError("receipt_request")
        request = Receive.model_validate(causal["canonical_request"])
        if (canonical(request.model_dump(mode="json")) != canonical(causal["canonical_request"])
            or digest(causal["canonical_request"]) != row.request_hash
            or str(request.operation_id) != row.operation_id or request.warehouse_id != row.warehouse_id
            or request.source_ref != source_ref or request.sku_code != binding["sku_code"]
            or request.batch_id != binding["batch_id"] or request.qty != amount
            or request.depends_on is not None or getattr(request, "correction_of", None) is not None):
            raise ValueError("receipt_request_mapping")
        unit = result["unit"]
        if (not isinstance(unit, dict) or not operational.issubset(unit) or not _origin_uuid_text(unit["id"])
            or not integer(unit["warehouse_id"]) or unit["warehouse_id"] != row.warehouse_id
            or not integer(unit["location_id"]) or unit["location_id"] != request.location_id
            or unit["sku_code"] != request.sku_code or not integer(unit["batch_id"])
            or unit["batch_id"] != request.batch_id or unit["characteristic"] != request.characteristic
            or unit["purpose"] != request.purpose or unit["arrival_source"] != source_ref
            or unit["packaging_id"] != request.packaging_id
            or (unit["packaging_id"] is not None and not integer(unit["packaging_id"]))
            or unit["package_state"] != request.package_state or unit["quality"] != "quarantine"
            or type(unit["container"]) is not str or unit["container"] != ""
            or quantity(unit["qty"]) != amount or unit["reserved"] != "0"
            or quantity(unit["free"]) != amount or unit["blocked_reason"] is not None
            or causal["births"] != [{"unit_id": unit["id"], "basis": "received", "source_unit_id": None}]
            or causal["count_basis"] != [] or not isinstance(causal["prior_frontiers"], dict)):
            raise ValueError("receipt_unit")
        return {"protocol": 2, "source_ref": source_ref,
            "unit": {key: unit[key] for key in operational},
            "native_receipt_source_v1": {key: binding[key] for key in binding_keys}}
    except (ValueError, TypeError, KeyError, AttributeError, InvalidOperation) as exc:
        raise HTTPException(404, "Запись не найдена") from exc


def _origin_timestamp(value):
    return value.isoformat() if value is not None else None


def _origin_operation_row(row):
    return {
        "id": row[0],
        "organization_id": row[1],
        "operation_id": row[2],
        "warehouse_id": row[3],
        "kind": row[4],
        "request_hash": row[5],
        "result": row[6],
        "actor": row[7],
        "created_at": _origin_timestamp(row[8]),
    }


def _origin_frontier_key(frontier):
    if not isinstance(frontier, dict) or set(frontier) != {"ledgers", "observations", "opaque_counts"}:
        raise ValueError("physical_origin_frontier_shape")
    tokens = []
    for key, required in (
        ("ledgers", {"ledger_id", "row_digest"}),
        ("observations", {"operation_pk", "count_line_id", "observation_digest"}),
        ("opaque_counts", {"operation_pk", "operation_id", "row_digest"}),
    ):
        rows = frontier[key]
        if not isinstance(rows, list):
            raise ValueError("physical_origin_frontier_rows")
        seen = set()
        for token in rows:
            if not isinstance(token, dict):
                raise ValueError("physical_origin_frontier_token")
            placement_zero = key == "observations" and token.get("kind") == "placement_zero_v1"
            expected = {"kind", "operation_pk", "operation_id", "unit_id", "row_digest"} if placement_zero else required
            if set(token) != expected:
                raise ValueError("physical_origin_frontier_token")
            if placement_zero and (
                type(token["operation_pk"]) is not int or token["operation_pk"] <= 0
                or not _origin_uuid_text(token["operation_id"])
                or not _origin_uuid_text(token["unit_id"])
                or not _origin_digest_text(token["row_digest"])
            ):
                raise ValueError("physical_origin_observation_token")
            encoded = canonical(token)
            if encoded in seen:
                raise ValueError("physical_origin_frontier_duplicate")
            seen.add(encoded)
            tokens.append(encoded)
    return frozenset(tokens)


def _origin_count_observations(operation, unit_id):
    result = operation["result"]
    causal = result.get("physical_causal_v1") if isinstance(result, dict) else None
    if (
        not isinstance(causal, dict)
        or type(causal.get("schema_version")) is not int
        or causal.get("schema_version") not in {1, 2}
    ):
        return None
    request = causal.get("canonical_request")
    basis = causal.get("count_basis")
    expected_causal_keys = {"schema_version", "canonical_request", "births", "count_basis"}
    if causal.get("schema_version") == 2:
        expected_causal_keys.add("prior_frontiers")
    if (
        set(causal) != expected_causal_keys
        or not isinstance(request, dict)
        or request.get("kind") != "count_approve"
        or not isinstance(basis, list)
        or type(operation.get("id")) is not int
        or operation["id"] <= 0
        or type(operation.get("organization_id")) is not int
        or operation["organization_id"] <= 0
        or type(operation.get("warehouse_id")) is not int
        or operation["warehouse_id"] <= 0
        or not _origin_uuid_text(operation.get("operation_id"))
        or request.get("operation_id") != operation.get("operation_id")
        or request.get("warehouse_id") != operation.get("warehouse_id")
        or not _origin_uuid_text(request.get("count_id"))
        or result.get("operation_id") != operation.get("operation_id")
        or result.get("kind") != "count_approve"
        or result.get("warehouse_id") != operation.get("warehouse_id")
        or result.get("status") != "approved"
        or result.get("count_id") != request.get("count_id")
        or type(result.get("opening")) is not bool
        or not _origin_digest_text(request.get("review_digest"))
    ):
        return None
    try:
        if digest(request) != operation["request_hash"]:
            return None
    except (TypeError, ValueError):
        return None
    required = {
        "count_id", "count_kind", "count_line_id", "line_identity", "unit_id",
        "created_unit", "observed_qty", "observed_dimensions", "qty_delta", "review_digest",
    }
    observations = []
    expected_births = []
    expected_adjustments = []
    seen_line_ids = set()
    seen_line_identities = set()
    seen_unit_ids = set()
    observed_count_kind = None
    for row in basis:
        if not isinstance(row, dict) or set(row) != required:
            return None
        if (
            row["count_id"] != request["count_id"]
            or row["count_id"] != result["count_id"]
            or not isinstance(row["count_kind"], str)
            or row["count_kind"] not in {"opening", "cycle", "exception"}
            or (observed_count_kind is not None and row["count_kind"] != observed_count_kind)
            or type(row["count_line_id"]) is not int
            or row["count_line_id"] <= 0
            or not isinstance(row["line_identity"], str)
            or not row["line_identity"]
            or not _origin_uuid_text(row["unit_id"])
            or type(row["created_unit"]) is not bool
            or not isinstance(row["observed_qty"], str)
            or not isinstance(row["qty_delta"], str)
            or not _origin_dimension_snapshot_valid(row["observed_dimensions"])
            or row["observed_dimensions"]["warehouse_id"] != operation["warehouse_id"]
            or row["line_identity"] != (
                digest(row["observed_dimensions"]) if row["created_unit"] else row["unit_id"]
            )
            or row["review_digest"] != request["review_digest"]
            or not _origin_digest_text(row["review_digest"])
            or row["count_line_id"] in seen_line_ids
            or row["line_identity"] in seen_line_identities
            or row["unit_id"] in seen_unit_ids
        ):
            return None
        try:
            observed = Decimal(row["observed_qty"])
            delta = Decimal(row["qty_delta"])
            observed_quantized = observed.quantize(Decimal("0.000001"))
            delta_quantized = delta.quantize(Decimal("0.000001"))
        except (InvalidOperation, ValueError):
            return None
        if (
            not observed.is_finite() or not delta.is_finite() or observed < 0
            or observed != observed_quantized or delta != delta_quantized
            or not _origin_decimal_text(row["observed_qty"])
            or not _origin_decimal_text(row["qty_delta"])
            or (row["created_unit"] and observed != delta)
        ):
            return None
        observed_count_kind = row["count_kind"]
        seen_line_ids.add(row["count_line_id"])
        seen_line_identities.add(row["line_identity"])
        seen_unit_ids.add(row["unit_id"])
        if row["created_unit"]:
            expected_births.append({
                "unit_id": row["unit_id"], "basis": "approved_count", "source_unit_id": None,
            })
        if delta:
            expected_adjustments.append({
                **row["observed_dimensions"], "qty_delta": format(delta, "f"),
            })
        if row["unit_id"] != unit_id:
            continue
        observation_digest = digest({
            "operation_pk": operation["id"],
            "operation_id": operation["operation_id"],
            "request_hash": operation["request_hash"],
            "count_basis": basis,
        })
        observations.append({
            "operation_pk": operation["id"],
            "count_line_id": row["count_line_id"],
            "observation_digest": observation_digest,
        })
    if causal.get("births") != expected_births:
        return None
    if basis and result.get("opening") != (observed_count_kind == "opening"):
        return None
    if not isinstance(result.get("adjustments"), list):
        return None
    try:
        if canonical(result["adjustments"]) != canonical(expected_adjustments):
            return None
    except (TypeError, ValueError):
        return None
    if causal.get("schema_version") == 2:
        frontiers = causal.get("prior_frontiers")
        if (
            not isinstance(frontiers, dict)
            or set(frontiers) != seen_unit_ids
            or any(not _origin_uuid_text(key) for key in frontiers)
        ):
            return None
        try:
            for frontier in frontiers.values():
                _origin_frontier_key(frontier)
        except (TypeError, ValueError):
            return None
    return observations


def _origin_uuid_text(value):
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _origin_digest_text(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _origin_decimal_text(value):
    return (
        isinstance(value, str)
        and len(value) <= 22
        and re.fullmatch(r"-?(?:0|[1-9][0-9]{0,13})(?:\.[0-9]{1,6})?", value) is not None
    )


def _origin_dimension_snapshot_valid(value):
    keys = {
        "warehouse_id", "location_id", "sku_code", "batch_id", "characteristic", "purpose",
        "quality", "packaging_id", "package_state", "container", "last_checked", "next_service",
        "inspection_ref", "service_result", "arrived_at", "arrival_source",
    }
    if not isinstance(value, dict) or set(value) != keys:
        return False
    if any(type(value[key]) is not int or value[key] <= 0 for key in ("warehouse_id", "location_id", "batch_id")):
        return False
    if value["packaging_id"] is not None and (type(value["packaging_id"]) is not int or value["packaging_id"] <= 0):
        return False
    if any(not isinstance(value[key], str) for key in (
        "sku_code", "characteristic", "purpose", "package_state", "container", "inspection_ref", "arrival_source",
    )):
        return False
    if (
        not 1 <= len(value["sku_code"]) <= 64
        or len(value["characteristic"]) > 128
        or len(value["purpose"]) > 128
        or len(value["container"]) > 64
        or len(value["inspection_ref"]) > 1000
        or len(value["arrival_source"]) > 1000
        or not isinstance(value["quality"], str)
        or value["quality"] not in {"quarantine", "released", "blocked"}
    ):
        return False
    if value["package_state"] not in {"loose", "sealed", "open"}:
        return False
    service = value["service_result"]
    if service is not None:
        if (
            not isinstance(service, dict)
            or set(service) != {"result", "evidence", "measurements"}
            or type(service.get("result")) is not str
            or service.get("result") not in {"passed", "failed"}
            or not isinstance(service.get("evidence"), str)
            or not 1 <= len(service["evidence"]) <= 1000
            or service["evidence"] != service["evidence"].strip()
            or not isinstance(service.get("measurements"), dict)
            or not 1 <= len(service["measurements"]) <= 30
        ):
            return False
        for key, measurement in service["measurements"].items():
            if (
                not isinstance(key, str) or not 1 <= len(key) <= 64 or key != key.strip()
                or not isinstance(measurement, str) or not 1 <= len(measurement) <= 100
                or measurement != measurement.strip()
            ):
                return False
    for key in ("last_checked", "next_service"):
        item = value[key]
        if item is not None:
            if not isinstance(item, str):
                return False
            try:
                if date.fromisoformat(item).isoformat() != item:
                    return False
            except ValueError:
                return False
    arrived_at = value["arrived_at"]
    if arrived_at is not None:
        if not isinstance(arrived_at, str):
            return False
        try:
            parsed_arrival = datetime.fromisoformat(arrived_at)
        except ValueError:
            return False
        if (
            parsed_arrival.tzinfo is None
            or parsed_arrival.isoformat() != arrived_at
        ):
            return False
    return True


async def physical_unit_frontier(session, organization_id, unit_id, *, before_pk=None, historical=None):
    """Read the exact immutable history boundary for one unit without autoflush."""
    from modules.wms.physical_models import PhysicalLedger, PhysicalOperation

    if not session.in_transaction():
        raise RuntimeError("physical_origin_transaction_required")
    if historical is not None:
        if historical.organization_id != organization_id or before_pk is not None:
            raise ValueError("physical_origin_historical_scope")
        before_pk = historical.before_pk
    with session.no_autoflush:
        ledger_statement = (select(
                PhysicalLedger.id,
                PhysicalLedger.operation_pk,
                PhysicalLedger.unit_id,
                PhysicalLedger.qty_delta,
                PhysicalLedger.dimensions,
                PhysicalOperation.id,
                PhysicalOperation.organization_id,
                PhysicalOperation.operation_id,
                PhysicalOperation.warehouse_id,
                PhysicalOperation.kind,
                PhysicalOperation.request_hash,
                PhysicalOperation.result,
                PhysicalOperation.actor,
                PhysicalOperation.created_at,
            )
            .select_from(PhysicalLedger)
            .outerjoin(PhysicalOperation, PhysicalOperation.id == PhysicalLedger.operation_pk)
            .where(PhysicalLedger.unit_id == unit_id))
        if before_pk is not None:
            ledger_statement = ledger_statement.where((PhysicalOperation.id < before_pk) | PhysicalOperation.id.is_(None))
        ledger_rows = (await session.execute(ledger_statement)).all()
        count_statement = select(
                PhysicalOperation.id,
                PhysicalOperation.organization_id,
                PhysicalOperation.operation_id,
                PhysicalOperation.warehouse_id,
                PhysicalOperation.kind,
                PhysicalOperation.request_hash,
                PhysicalOperation.result,
                PhysicalOperation.actor,
                PhysicalOperation.created_at,
            ).where(
                PhysicalOperation.organization_id == organization_id,
                PhysicalOperation.kind == "count_approve",
            )
        if before_pk is not None:
            count_statement = count_statement.where(PhysicalOperation.id < before_pk)
        count_rows = (await session.execute(count_statement)).all()
        warehouse_id = (await session.scalar(select(StockUnit.warehouse_id).where(StockUnit.id == unit_id))
            if historical is None else historical.warehouse_id)
        placement_statement = select(PhysicalOperation).where(PhysicalOperation.organization_id == organization_id,
            PhysicalOperation.warehouse_id == warehouse_id).order_by(PhysicalOperation.id)
        if before_pk is not None:
            placement_statement = placement_statement.where(PhysicalOperation.id < before_pk)
        scoped = list((await session.scalars(placement_statement)).all())

    frontier = {"ledgers": [], "observations": [], "opaque_counts": []}
    ledger_records = []
    operation_records = {}
    for row in ledger_rows:
        operation = _origin_operation_row(row[5:]) if row[5] is not None else None
        ledger = {
            "id": row[0],
            "operation_pk": row[1],
            "unit_id": row[2],
            "qty_delta": str(row[3]),
            "dimensions": row[4],
            "operation": operation,
        }
        payload = {
            "ledger_id": ledger["id"],
            "operation_pk": ledger["operation_pk"],
            "unit_id": ledger["unit_id"],
            "qty_delta": ledger["qty_delta"],
            "dimensions": ledger["dimensions"],
            "operation_id": operation["operation_id"] if operation else None,
            "organization_id": operation["organization_id"] if operation else None,
            "request_hash": operation["request_hash"] if operation else None,
        }
        token = {"ledger_id": ledger["id"], "row_digest": digest(payload)}
        frontier["ledgers"].append(token)
        ledger["token"] = token
        ledger_records.append(ledger)
        if operation is not None:
            operation_records[operation["id"]] = operation

    count_records = []
    for raw in count_rows:
        operation = _origin_operation_row(raw)
        count_records.append(operation)
        observations = _origin_count_observations(operation, unit_id)
        if observations is None:
            opaque = {
                "operation_pk": operation["id"],
                "operation_id": operation["operation_id"],
                "row_digest": digest(operation),
            }
            frontier["opaque_counts"].append(opaque)
            operation["opaque_token"] = opaque
        else:
            for token in observations:
                frontier["observations"].append(token)
            operation["observation_tokens"] = observations
    for key in frontier:
        frontier[key].sort(key=canonical)
    from modules.wms.placement_models import placement_operation_indicator
    from modules.wms.placement_service import validate_placement_receipt
    zero_operations = []
    for row in scoped:
        if not placement_operation_indicator(row.kind, row.result):
            continue
        connection = await session.connection()
        operation = {column.name: getattr(row, column.name) for column in PhysicalOperation.__table__.columns}
        proof = await connection.run_sync(lambda conn, op=operation: validate_placement_receipt(conn, op))
        if proof["kind"] != "capacity_placement_complete":
            continue
        member = next((unit for unit in proof["source_before"]["members"] if unit["id"] == unit_id), None)
        if member is None or Decimal(member["qty"]) != 0:
            continue
        snapshot = _origin_operation_row(tuple(getattr(row, name) for name in
            ("id", "organization_id", "operation_id", "warehouse_id", "kind", "request_hash", "result", "actor", "created_at")))
        token = {"kind": "placement_zero_v1", "operation_pk": row.id, "operation_id": row.operation_id,
                 "unit_id": unit_id, "row_digest": digest(snapshot)}
        frontier["observations"].append(token)
        zero_operations.append({**snapshot, "placement_zero_token": token})
        operation_records[row.id] = snapshot
    frontier["observations"].sort(key=canonical)
    records = {
        "ledgers": ledger_records,
        "operations": operation_records,
        "count_operations": count_records,
        "placement_zero_operations": zero_operations,
    }
    return frontier, records


async def _require_physical_causal_snapshot(session):
    """Require a clean, supported transaction before physical history can flush."""
    from modules.wms.physical_capacity import require_capacity_read_committed

    if not session.in_transaction():
        reject("Доказательство остатка требует активную транзакцию", 409)
    if any(
        isinstance(row, (PhysicalOperation, PhysicalLedger))
        for collection in (session.new, session.dirty, session.deleted)
        for row in collection
    ):
        reject("В транзакции есть незавершённая история физических операций", 409)
    connection = await session.connection()
    await connection.run_sync(require_capacity_read_committed)


async def capture_physical_prior_frontiers(session, organization_id, unit_ids):
    """Freeze a complete same-transaction history boundary before receipt INSERT."""
    await _require_physical_causal_snapshot(session)
    result = {}
    with session.no_autoflush:
        for unit_id in sorted(set(unit_ids)):
            frontier, _ = await physical_unit_frontier(session, organization_id, unit_id)
            result[unit_id] = frontier
    return result


def reject(message, status=409):
    raise HTTPException(status, message)


def _require_on_hand(unit):
    try:
        require_current_on_hand(unit)
    except CurrentStockStateError:
        reject("Состояние складского остатка не подтверждено; требуется сверка")


def _on_hand_units(units):
    """Validate the whole fetched scope before projecting addressable stock."""
    present = []
    for unit in units:
        try:
            if unit.stock_state == "in_transit":
                require_current_in_transit(unit)
            else:
                require_current_on_hand(unit)
                present.append(unit)
        except (AttributeError, CurrentStockStateError):
            reject("Состояние складского остатка не подтверждено; требуется сверка")
    return present


def decimal_text(value):
    return format(Decimal(value), "f")


def timestamp_text(value):
    if value is None:
        return None
    return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()


def dimensions(unit):
    return {"warehouse_id": unit.warehouse_id, "location_id": unit.location_id,
        "sku_code": unit.sku_code, "batch_id": unit.batch_id,
        "characteristic": unit.characteristic, "purpose": unit.purpose,
        "quality": unit.quality, "packaging_id": unit.packaging_id,
        "package_state": unit.package_state, "container": unit.container,
        "last_checked": unit.last_checked.isoformat() if unit.last_checked else None,
        "next_service": unit.next_service.isoformat() if unit.next_service else None,
        "inspection_ref": unit.inspection_ref,
        "service_result": unit.service_result,
        "arrived_at": timestamp_text(unit.arrived_at),
        "arrival_source": unit.arrival_source}


def unit_output(unit, blocked=None):
    _require_on_hand(unit)
    return {"id": unit.id, **dimensions(unit), "qty": decimal_text(unit.qty),
        "reserved": decimal_text(unit.reserved), "free": decimal_text(unit.qty - unit.reserved),
        "batch_scan": f"LOT-{unit.batch_id}-{unit.id.upper()}",
        "blocked_reason": blocked,
        **({"assembly_output_id": unit.assembly_output_id} if unit.assembly_output_id else {})}


def transfer_dimensions(unit):
    if unit.stock_state == "in_transit":
        require_current_in_transit(unit)
    else:
        require_current_on_hand(unit)
    return {**dimensions(unit), "stock_state": unit.stock_state,
            "dispatch_operation_id": unit.dispatch_operation_id}


async def picked_output(session, stock_gateway, warehouse, pick, hold, clone, original):
    if clone is None or original is None:
        reject("Исходная партия подбора не найдена", 409)
    if clone.warehouse_id != warehouse.warehouse_id or original.warehouse_id != warehouse.warehouse_id:
        reject("Подбор относится к другому складу", 409)
    if (clone.sku_code != original.sku_code or clone.batch_id != original.batch_id
            or clone.packaging_id != original.packaging_id
            or clone.characteristic != original.characteristic
            or clone.purpose != original.purpose):
        reject("Исходная партия и подобранная единица имеют разные измерения", 409)
    lot = await effective_lot(session, stock_gateway, warehouse.organization_id, original)
    sku = await session.scalar(select(Sku).where(Sku.code == clone.sku_code))
    if sku is None:
        reject("Товар подбора не подтверждён", 409)
    packaging = await session.get(Packaging, clone.packaging_id) if clone.packaging_id is not None else None
    if clone.packaging_id is not None and (
        packaging is None or packaging.organization_id != warehouse.organization_id
        or packaging.sku_code != clone.sku_code
    ):
        reject("Упаковка подбора не подтверждена для товара и организации", 409)
    return {
        "id": pick.id,
        "unit_id": clone.id,
        "source": hold.order_ref,
        "container": hold.container,
        "sku_code": clone.sku_code,
        "qty": decimal_text(pick.qty - pick.shipped),
        "packed": pick.packed,
        "batch_id": original.batch_id,
        "batch_scan": f"LOT-{original.batch_id}-{original.id.upper()}",
        "item_scan": f"ERP-P-{packaging.id}" if packaging else f"SKU-{sku.id}",
        "packaging_id": clone.packaging_id,
        "package_state": clone.package_state,
        "package_factor": decimal_text(packaging.factor) if packaging else "1",
        "lot_no": lot["lot_no"],
    }


async def control(session, warehouse_id, organization_id, lock=True):
    query = select(WarehouseControl).where(WarehouseControl.warehouse_id == warehouse_id,
        WarehouseControl.organization_id == organization_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = await session.scalar(query)
    if row is None:
        reject("Склад не подготовлен для физического учёта ERP", 404)
    return row


_NATIVE_SELECTOR_I32 = re.compile(r"^[1-9][0-9]{0,9}$")


def native_selector_i32(value, name):
    """Accept only canonical positive database Integers supplied as raw text."""
    if type(value) is not str or not _NATIVE_SELECTOR_I32.fullmatch(value):
        reject(f"Некорректный идентификатор {name}", 409)
    parsed = int(value)
    if parsed > 2147483647:
        reject(f"Идентификатор {name} выходит за диапазон базы данных", 409)
    return parsed


def capture_native_erp_selector_authority(principal):
    """Freeze managed identity before the first await or stock constructor."""
    from core.services.auth import CurrentUser

    roles = principal.roles if isinstance(principal, CurrentUser) else None
    frozen = (
        principal.local_user_id if roles is not None else None,
        principal.keycloak_user_id if roles is not None else None,
        principal.issued_at if roles is not None else None,
        tuple(roles) if isinstance(roles, list) else (),
        principal.username if roles is not None else "",
        principal.deal_visibility if roles is not None else None,
        principal.crm_restricted if roles is not None else False,
    )
    if (type(frozen[0]) is not int or frozen[0] < 1
        or not isinstance(frozen[1], str) or not frozen[1].strip()
        or type(frozen[2]) is not int or not isinstance(roles, list)
        or not frozen[3] or any(not isinstance(role, str) for role in frozen[3])):
        reject("Требуется управляемая OIDC-учётная запись", 403)
    return frozen


def _native_erp_selector_identity(fresh, frozen, permitted):
    return (
        fresh.local_user_id == frozen[0]
        and fresh.keycloak_user_id == frozen[1]
        and fresh.issued_at == frozen[2]
        and tuple(fresh.roles) == frozen[3]
        and fresh.local_status == "active"
        and permitted
    )


async def revalidate_native_erp_selector_authority(session, core, frozen, organization_id):
    """Recheck signed managed identity, role and organization after awaited reads."""
    from core.services.auth import (
        CurrentUser,
        EffectiveIdentityLookupError,
        has_permission,
        resolve_effective_oidc_user,
    )

    accounting = getattr(getattr(core, "services", None), "accounting", None)
    if accounting is None:
        reject("Сервис проверки доступа организации недоступен", 503)

    def seed():
        return CurrentUser(
            frozen[4], list(frozen[3]), keycloak_user_id=frozen[1],
            local_status=None, deal_visibility=frozen[5],
            crm_restricted=frozen[6], issued_at=frozen[2], local_user_id=frozen[0],
        )

    try:
        fresh = await resolve_effective_oidc_user(seed(), session)
    except EffectiveIdentityLookupError as exc:
        raise HTTPException(503, "Текущую учётную запись нельзя проверить") from exc
    if not _native_erp_selector_identity(
        fresh, frozen, has_permission(core, fresh, "wms.operate"),
    ):
        reject("Текущая складская роль больше не разрешает чтение источников", 403)
    actor = await accounting.source_member(session, organization_id, fresh)
    try:
        fresh = await resolve_effective_oidc_user(seed(), session)
    except EffectiveIdentityLookupError as exc:
        raise HTTPException(503, "Текущую учётную запись нельзя проверить") from exc
    if (actor != frozen[1] or not _native_erp_selector_identity(
        fresh, frozen, has_permission(core, fresh, "wms.operate"),
    )):
        reject("Текущий доступ к организации или складу изменился", 403)
    return fresh, actor


async def native_receiving_source_warehouse(session, organization_id, warehouse_id, *, lock):
    """Lock an active directory row before the physical warehouse mutex."""
    from modules.wms.models import WarehouseDirectory

    directory = await session.scalar(select(WarehouseDirectory).where(
        WarehouseDirectory.id == warehouse_id,
        WarehouseDirectory.organization_id == organization_id,
        WarehouseDirectory.is_active.is_(True),
    ).with_for_update(read=True).execution_options(populate_existing=True))
    if directory is None:
        reject("Склад организации не найден", 404)
    row = await control(session, warehouse_id, organization_id, lock=lock)
    if row.mode != "erp" or row.onec_ref is not None:
        reject("Склад не использует нативный физический учёт ERP", 404)
    return row


async def location(session, warehouse_id, location_id, *, unlocked=True):
    row = await session.get(PhysicalLocation, location_id)
    if row is None or row.warehouse_id != warehouse_id:
        reject("Ячейка принадлежит другому складу или не подготовлена")
    original = await session.get(Location, location_id)
    if original is None or not original.is_active:
        reject("Ячейка неактивна")
    if unlocked and await session.get(CountLocationLock, location_id):
        reject("Ячейка заблокирована для пересчёта")
    return row


async def policy(session, warehouse_id, sku, qty=None):
    row = await session.get(StoragePolicy, (warehouse_id, sku))
    if row is None:
        reject(f"Не подтверждены правила хранения и единицы товара {sku}")
    if qty is not None and qty != qty.quantize(Decimal(1).scaleb(-row.precision)):
        reject(f"Количество {sku} должно соответствовать точности базовой единицы", 422)
    return row


async def batch(session, stock_gateway, organization_id, batch_id, sku):
    row = await stock_gateway.batch_by_id(session, organization_id, batch_id)
    if row is None or row["sku_code"] != sku:
        reject("Партия не подтверждена для товара и организации")
    return row


def assembly_lot_snapshot(lot, batch_id, sku):
    from modules.wms.assembly_quality import physical_assembly_proof
    from modules.wms.physical_schemas import AssemblyEffectiveLot

    return physical_assembly_proof(AssemblyEffectiveLot, {
        "batch_id": batch_id, "sku_code": sku, "lot_no": lot["lot_no"],
        "mfg_date": lot.get("mfg_date"), "expiry_date": lot.get("expiry_date"),
        "manufacture_precision": lot.get("manufacture_precision"), "date_source": lot.get("date_source"),
    }).model_dump(mode="json")


async def effective_lot(session, stock_gateway, organization_id, unit, *, expected_state="on_hand"):
    """All assembly date consumers use the same immutable, verified effective lot."""
    lot = await batch(session, stock_gateway, organization_id, unit.batch_id, unit.sku_code)
    if unit.assembly_output_id is None:
        return lot
    from modules.wms.assembly_quality import physical_assembly_identity

    identity, _proof, _birth = await physical_assembly_identity(session, organization_id, unit.assembly_output_id)
    if (identity.sku_code != unit.sku_code or identity.batch_id != unit.batch_id
            or assembly_lot_snapshot(lot, unit.batch_id, unit.sku_code) != identity.batch_snapshot):
        reject("Подтверждённая партия или даты идентичности изделия изменились")
    from modules.wms.item_identity import (
        _origin_frontier_tokens,
        _OriginUnproven,
        _prove_unit_boundary,
    )
    try:
        frontier, _records = await physical_unit_frontier(session, organization_id, unit.id)
        origin = await _prove_unit_boundary(session, organization_id, unit.id,
            _origin_frontier_tokens(frontier), current_unit=unit, allow_zero=unit.qty == 0,
            expected_state=expected_state)
        if (origin.get("qc_birth_operation_id") is None
                or origin.get("root_operation_id") != _birth.operation_id):
            raise _OriginUnproven
    except _OriginUnproven as exc:
        raise HTTPException(409, "Assembly effective lot has no proved physical origin") from exc
    return {**lot, **identity.effective_lot}


def block_reason(unit, rule, lot, shelf_days=0, *, quality=True, service=True):
    on_date = today()
    if quality and unit.quality != "released":
        return "quality_not_released"
    expiry = date.fromisoformat(lot["expiry_date"]) if lot.get("expiry_date") else None
    manufacture = date.fromisoformat(lot["mfg_date"]) if lot.get("mfg_date") else None
    if (rule.expiry_required or rule.min_shelf_days or shelf_days or rule.manufacture_required or rule.max_age_days) and not lot.get("date_source"):
        return "date_source_unknown"
    if (rule.expiry_required or rule.min_shelf_days or shelf_days) and expiry is None:
        return "expiry_unknown"
    if expiry is None and unit.arrival_source == "opening_count_no_receipt_date":
        return "fifo_receipt_date_unknown"
    if expiry and expiry < on_date + timedelta(days=max(rule.min_shelf_days, shelf_days)):
        return "expiry_or_customer_shelf_life"
    if (rule.manufacture_required or rule.max_age_days) and manufacture is None:
        return "manufacture_unknown"
    if manufacture and manufacture > on_date:
        return "manufacture_in_future"
    if rule.max_age_days:
        if lot.get("manufacture_precision") != "day":
            return "manufacture_precision_unconfirmed"
        if (on_date - manufacture).days > rule.max_age_days:
            return "maximum_battery_age"
    if service and rule.maintenance_days:
        if not unit.last_checked or not unit.next_service or not unit.service_result or unit.service_result.get("result") != "passed":
            return "maintenance_unknown"
        if unit.last_checked > on_date or min(unit.next_service, unit.last_checked + timedelta(days=rule.maintenance_days)) <= on_date:
            return "maintenance_due"
    return None


async def eligible(session, stock_gateway, warehouse, unit, shelf_days=0, **options):
    _require_on_hand(unit)
    rule = await policy(session, warehouse.warehouse_id, unit.sku_code)
    lot = await effective_lot(session, stock_gateway, warehouse.organization_id, unit)
    sku = await session.scalar(select(Sku).where(Sku.code == unit.sku_code))
    if sku is None or not sku.is_active:
        return "sku_not_active", lot
    return block_reason(unit, rule, lot, shelf_days, **options), lot


async def package_check(session, warehouse, sku, qty, packaging_id, state):
    if packaging_id is None:
        if state != "loose":
            reject("Для состояния коробки нужна подтверждённая упаковка", 422)
        return None
    pack = await session.get(Packaging, packaging_id)
    if pack is None or pack.organization_id != warehouse.organization_id or pack.sku_code != sku:
        reject("Упаковка не соответствует товару и организации")
    await policy(session, warehouse.warehouse_id, sku, pack.factor)
    if state == "sealed" and (not pack.sealed_allowed or qty % pack.factor != 0):
        reject("Закрытая упаковка должна иметь подтверждённое целое число коробок")
    return pack


async def capacity(session, destination, sku, qty):
    if destination.capacity_qty is None:
        return
    if destination.capacity_sku != sku:
        reject("Вместимость ячейки подтверждена для другого товара")
    rows = (await session.scalars(select(StockUnit).where(StockUnit.location_id == destination.location_id,
        StockUnit.qty > 0))).all()
    if any(unit.sku_code != sku for unit in rows):
        reject("Ячейка с подтверждённой вместимостью содержит другой товар")
    pending = (await session.scalars(select(PhysicalTask).where(
        PhysicalTask.destination_id == destination.location_id,
        PhysicalTask.status.in_(["open", "claimed"]), PhysicalTask.kind == "replenish"))).all()
    # Pending task capacity is checked by the planner, not counted twice on execution.
    if sum((u.qty for u in rows), Decimal(0)) + qty > destination.capacity_qty:
        reject("Превышена подтверждённая вместимость ячейки")
    return sum((task.qty for task in pending), Decimal(0))


async def protect_blind_projection(session, warehouse_ids, *, own_count_id=None):
    """Hide historical quantities; an active counter may recover own observations."""
    locked = (await session.scalars(select(CountLocationLock.count_id).join(PhysicalLocation,
        PhysicalLocation.location_id == CountLocationLock.location_id).where(
        PhysicalLocation.warehouse_id.in_(warehouse_ids)))).all()
    if own_count_id is not None and own_count_id in locked:
        return
    if locked:
        reject("Результат операции временно скрыт до завершения слепого пересчёта; сохраните исходный идентификатор", 423)


async def protect_blind_receipt(session, operation):
    if operation.kind == "cutover":
        return  # Preserve the existing opening-count recovery exception.
    result = operation.result
    if not isinstance(result, dict):
        reject("Повреждённый результат операции скрыт", 423)
    warehouse_ids = {operation.warehouse_id}
    from modules.wms.transfer_loss_protocol import (
        LossReceiptError,
        loss_operation_indicator,
        recorded_receipt,
    )

    if loss_operation_indicator(operation.kind, result):
        from modules.wms.item_identity import _origin_operation_snapshot

        try:
            receipt = recorded_receipt(_origin_operation_snapshot(operation))
        except LossReceiptError as exc:
            raise HTTPException(423, "Повреждённое доказательство расхождения скрыто") from exc
        warehouse_ids.update((receipt.source_warehouse_id, receipt.destination_warehouse_id))
    elif operation.kind in {"transfer_dispatch", "transfer_receive"}:
        from modules.wms.item_identity import (
            _origin_operation_snapshot,
            _origin_validate_operation,
            _OriginUnproven,
        )
        try:
            _, _, proven = _origin_validate_operation(_origin_operation_snapshot(operation), operation.organization_id)
        except _OriginUnproven as exc:
            raise HTTPException(423, "Повреждённый результат межскладской операции скрыт") from exc
        warehouse_ids.update((proven["transfer"]["source_warehouse_id"],
                              proven["transfer"]["destination_warehouse_id"]))
    if operation.kind == "move":
        # The immutable server receipt identifies the destination, never replay input.
        moved = result.get("unit")
        if (
            result.get("kind") != operation.kind
            or result.get("operation_id") != operation.operation_id
            or type(result.get("warehouse_id")) is not int
            or result["warehouse_id"] != operation.warehouse_id
            or not isinstance(moved, dict)
            or type(moved.get("warehouse_id")) is not int or moved["warehouse_id"] <= 0
            or type(moved.get("location_id")) is not int or moved["location_id"] <= 0
            or not _origin_uuid_text(moved.get("id"))
        ):
            reject("Повреждённый результат перемещения скрыт", 423)
        destination = await session.scalar(select(StockUnit.warehouse_id).join(PhysicalLocation,
            PhysicalLocation.location_id == StockUnit.location_id).join(WarehouseControl,
            WarehouseControl.warehouse_id == StockUnit.warehouse_id).where(
            StockUnit.id == moved["id"], StockUnit.warehouse_id == moved["warehouse_id"],
            StockUnit.location_id == moved["location_id"],
            PhysicalLocation.warehouse_id == StockUnit.warehouse_id,
            WarehouseControl.organization_id == operation.organization_id))
        if destination is None:
            reject("Не подтверждён склад результата перемещения", 423)
        warehouse_ids.add(destination)
    # Serialize projection with count creation/cancellation using existing mutexes.
    for warehouse_id in sorted(warehouse_ids):
        await control(session, warehouse_id, operation.organization_id)
    own_count_id = None
    if operation.kind.startswith("count_"):
        count_id = result.get("count_id")
        if not _origin_uuid_text(count_id):
            reject("Повреждённый результат пересчёта скрыт", 423)
        count = await session.get(PhysicalCount, count_id, populate_existing=True)
        if count is None or count.warehouse_id != operation.warehouse_id:
            reject("Не подтверждён пересчёт результата операции", 423)
        if count.status == "open":
            own_count_id = count.id
    await protect_blind_projection(session, warehouse_ids, own_count_id=own_count_id)


async def review_count(session, count):
    """Internal supervisor-only review; callers must verify organization/permission."""
    lines = (await session.scalars(select(PhysicalCountLine).where(PhysicalCountLine.count_id == count.id)
        .order_by(PhysicalCountLine.id))).all()
    rows = []
    for line in lines:
        expected = count.expected.get(line.unit_id, {}).get("qty") if line.unit_id else None
        if expected is None and count.kind != "opening":
            expected = "0"
        rows.append({"line_id": line.id, "unit_id": line.unit_id, "dimensions": line.dimensions,
            "expected_qty": expected, "counted_qty": decimal_text(line.qty) if line.qty is not None else None,
            "difference": decimal_text(line.qty - Decimal(expected)) if line.qty is not None and expected is not None else None})
    basis = {"count_id": count.id, "status": count.status, "expected": count.expected,
        "completed_locations": count.completed_locations, "lines": rows}
    return {"count_id": count.id, "status": count.status, "lines": rows,
        "missing_locations": sorted(set(count.locations) - set(count.completed_locations)),
        "review_digest": digest(basis)}


class PhysicalStock:
    def __init__(self, reference_gateway, event_bus=None, *, services=None, user=None, core=None,
                 mobile_transfer_authority=None, mobile_fulfillment_authority=None):
        from core.services.auth import CurrentUser

        self.reference = reference_gateway
        self.event_bus = event_bus
        self.services, self.user = services, user
        self._caller_seed = (
            (
                user.username, tuple(user.roles), user.keycloak_user_id,
                user.local_status, user.deal_visibility, user.crm_restricted,
                user.issued_at, user.local_user_id,
            )
            if isinstance(user, CurrentUser) else None
        )
        self.core = core
        self.mobile_transfer_authority = mobile_transfer_authority
        self.mobile_fulfillment_authority = mobile_fulfillment_authority
        self._deltas = []

    async def _fresh_mobile_pack_authority(self, session, organization_id, warehouse_id, actor):
        from modules.wms.mobile_access import (
            OriginalMobileFulfillmentSourceAuthority,
            revalidate_mobile_fulfillment_source_authority,
        )

        authority = self.mobile_fulfillment_authority
        seed = self._caller_seed
        if (not isinstance(authority, OriginalMobileFulfillmentSourceAuthority)
                or authority.purpose != "pack" or authority.proof is None
                or seed is None or seed[1] != ("wms_mobile",)
                or authority.identity.organization_id != organization_id
                or authority.identity.warehouse_id != warehouse_id
                or authority.identity.original_user_id != seed[7]
                or authority.identity.original_sub != seed[2]
                or authority.identity.bearer_issued_at != seed[6]
                or actor != seed[2] or session.in_nested_transaction()
                or not session.in_transaction()):
            reject("Недостаточно прав", 403)
        device, grant, fresh, current_actor = await revalidate_mobile_fulfillment_source_authority(
            session, self.core, self.user, authority, None,
        )
        if (current_actor != actor or grant.organization_id != organization_id
                or grant.warehouse_id != warehouse_id or not {"pack", "receipt"}.issubset(set(grant.actions))
                or grant.id != authority.proof.grant_id or device.id != authority.proof.device_id
                or fresh.local_user_id != seed[7] or fresh.keycloak_user_id != seed[2]
                or fresh.issued_at != seed[6] or tuple(fresh.roles or ()) != ("wms_mobile",)):
            reject("Недостаточно прав", 403)
        return {
            "protocol": 1, "user_id": seed[7], "sub": seed[2],
            "device_id": device.id, "grant_id": grant.id,
        }

    async def _require_mobile_pack_replay(self, session, organization_id, actor, existing):
        authority = self.mobile_fulfillment_authority
        owner = native_pack_owner(existing.result, authority, authority.proof if authority is not None else None)
        if existing.kind != "pack_v2" or owner is None:
            reject("Запись не найдена", 404)
        native_pack_v1_projection(existing)
        current_owner = await self._fresh_mobile_pack_authority(
            session, organization_id, existing.warehouse_id, actor,
        )
        if owner != current_owner:
            reject("Запись не найдена", 404)

    async def _fresh_capacity_authority(
        self, session, organization_id, actor, permissions,
    ):
        from core.services.auth import (
            CurrentUser,
            EffectiveIdentityLookupError,
            has_permission,
            resolve_effective_oidc_user,
        )

        seed = self._caller_seed
        if (
            self.core is None or seed is None
            or type(seed[7]) is not int or seed[7] <= 0
            or not isinstance(seed[2], str) or not seed[2].strip()
        ):
            reject("Требуется управляемая OIDC-учётная запись", 403)
        seed_uid, seed_subject, seed_issued_at = seed[7], seed[2], seed[6]

        accounting = getattr(
            getattr(self.core, "services", None), "accounting", None,
        )
        if accounting is None:
            reject("Сервис проверки доступа организации недоступен", 503)

        async def refresh():
            try:
                fresh = await resolve_effective_oidc_user(
                    CurrentUser(
                        seed[0], list(seed[1]),
                        keycloak_user_id=seed[2],
                        local_status=None,
                        deal_visibility=seed[4],
                        crm_restricted=seed[5],
                        issued_at=seed[6],
                        local_user_id=seed[7],
                    ),
                    session,
                )
            except EffectiveIdentityLookupError as exc:
                raise HTTPException(
                    503, "Текущую учётную запись нельзя проверить",
                ) from exc
            if (
                fresh.local_user_id != seed_uid
                or fresh.keycloak_user_id != seed_subject
                or fresh.issued_at != seed_issued_at
                or fresh.local_status != "active"
            ):
                reject("Текущая управляемая учётная запись неактивна", 403)
            return fresh

        fresh = await refresh()
        try:
            await accounting.source_member(session, organization_id, fresh)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                503, "Текущее членство организации нельзя проверить",
            ) from exc

        # Organization lock is now held; cached principal is not authority.
        fresh = await refresh()
        try:
            current_actor = await accounting.source_member(
                session, organization_id, fresh,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                503, "Текущее членство организации нельзя проверить",
            ) from exc

        # Re-resolve after the second membership await before deciding permission.
        fresh = await refresh()
        if current_actor != actor or not all(
            has_permission(self.core, fresh, permission)
            for permission in permissions
        ):
            reject("Текущий доступ не разрешает операцию вместимости", 403)
        self.user = fresh

    async def _responsibility_scope(self, session, organization_id, warehouse_id, actor):
        from modules.wms.physical_capacity import require_capacity_read_committed

        seed = self._caller_seed
        if (
            type(organization_id) is not int or not 1 <= organization_id <= 2147483647
            or type(warehouse_id) is not int or not 1 <= warehouse_id <= 2147483647
            or seed is None or type(seed[7]) is not int or not 1 <= seed[7] <= 2147483647
            or not isinstance(seed[2], str) or actor != seed[2]
        ):
            reject("Требуется точный склад и управляемая OIDC-учётная запись", 403)
        if not session.in_transaction():
            reject("Требуется транзакция назначения ответственного", 409)
        connection = await session.connection()
        await connection.run_sync(require_capacity_read_committed)
        await self._fresh_capacity_authority(session, organization_id, actor, ("wms.configure",))
        await control(session, warehouse_id, organization_id, lock=True)
        await self._fresh_capacity_authority(session, organization_id, actor, ("wms.configure",))

    async def _responsibility_row(self, session, organization_id, warehouse_id):
        from modules.wms.physical_models import WarehouseResponsible

        row = await session.scalar(select(WarehouseResponsible).where(
            WarehouseResponsible.organization_id == organization_id,
            WarehouseResponsible.warehouse_id == warehouse_id,
        ).with_for_update().execution_options(populate_existing=True))
        if row is None:
            return None
        operation = await session.scalar(select(PhysicalOperation).where(
            PhysicalOperation.organization_id == organization_id,
            PhysicalOperation.warehouse_id == warehouse_id,
            PhysicalOperation.operation_id == row.operation_id,
        ))
        receipt = responsible_recorded_receipt(operation)
        if (
            receipt.revision != row.revision or receipt.user_id != row.user_id
            or receipt.active is not row.active
            or receipt.warehouse_responsible_v1.assignee_subject != row.subject
        ):
            reject("Назначение ответственного не соответствует квитанции", 409)
        return row

    async def _responsibility_candidate(self, session, organization_id, user_id, expected_subject=None):
        from core.services.auth import (
            EffectiveIdentityLookupError,
            managed_identity_eligible,
            managed_identity_metadata,
        )

        async def candidate():
            try:
                result = await managed_identity_metadata(session, user_id)
            except EffectiveIdentityLookupError as exc:
                raise HTTPException(503, "Учётную запись ответственного нельзя проверить") from exc
            if result is None or not managed_identity_eligible(self.core, result, "wms.approve"):
                reject("Ответственный должен иметь действующую управляемую учётную запись и право подтверждения", 403)
            if expected_subject is not None and result.subject != expected_subject:
                reject("Учётная запись ответственного изменилась; уточните назначение", 409)
            return result

        initial = await candidate()
        accounting = self.core.services.accounting
        try:
            await accounting.source_assignee_member(
                session, organization_id, self.user, assignee_subject=initial.subject,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, "Членство ответственного в организации нельзя проверить") from exc
        current = await candidate()
        if current.subject != initial.subject or current.user_id != initial.user_id:
            reject("Учётная запись ответственного изменилась; уточните назначение", 409)
        return current

    async def warehouse_responsibility(self, session, organization_id, warehouse_id, actor):
        with session.no_autoflush:
            await self._responsibility_scope(session, organization_id, warehouse_id, actor)
            row = await self._responsibility_row(session, organization_id, warehouse_id)
            result = {
                "schema_version": 1, "organization_id": organization_id, "warehouse_id": warehouse_id,
                "revision": row.revision if row else 0, "active": row.active if row else False,
                "user_id": row.user_id if row else None, "operation_id": row.operation_id if row else None,
            }
            await self._fresh_capacity_authority(session, organization_id, actor, ("wms.configure",))
            return result

    async def set_warehouse_responsibility(self, session, organization_id, warehouse_id, data, actor):
        from modules.wms.physical_models import WarehouseResponsible
        from modules.wms.physical_schemas import WarehouseResponsibleSet

        if not isinstance(data, WarehouseResponsibleSet):
            reject("Недопустимая команда назначения ответственного", 422)
        # Retain the validated request before any awaited authorization/lock.
        data = WarehouseResponsibleSet.model_validate(data.model_dump(mode="json"))
        with session.no_autoflush:
            await self._responsibility_scope(session, organization_id, warehouse_id, actor)
            seed = self._caller_seed
            operation_id = str(data.operation_id)
            request_hash = digest({
                "organization_id": organization_id, "warehouse_id": warehouse_id,
                "actor_user_id": seed[7], "actor_sub": seed[2], "body": data.model_dump(mode="json"),
            })
            existing = await session.scalar(select(PhysicalOperation).where(
                PhysicalOperation.organization_id == organization_id,
                PhysicalOperation.operation_id == operation_id,
            ))
            if existing is not None:
                if existing.kind != RESPONSIBLE_KIND or existing.warehouse_id != warehouse_id or existing.request_hash != request_hash:
                    reject("Идентификатор операции уже использован с другими данными", 409)
                receipt = responsible_recorded_receipt(existing)
                await self._fresh_capacity_authority(session, organization_id, actor, ("wms.configure",))
                return receipt.model_dump(mode="json", exclude={RESPONSIBLE_PRIVATE})

            row = await self._responsibility_row(session, organization_id, warehouse_id)
            revision = row.revision if row else 0
            if revision != data.expected_revision or revision == 2147483647:
                reject("Назначение изменилось или достигнут предел версий; обновите данные", 409)
            if not data.active and row is None:
                reject("Назначение отсутствует; отзывать нечего", 409)
            if data.active:
                directory = await session.get(WarehouseDirectory, warehouse_id, populate_existing=True)
                if directory is None or directory.organization_id != organization_id or not directory.is_active:
                    reject("Склад организации не найден или неактивен", 404)
                target = await self._responsibility_candidate(session, organization_id, data.user_id)
                target_id, target_sub = target.user_id, target.subject
            else:
                # Revocation deliberately survives deletion/disable/relink of the old candidate.
                target_id, target_sub = row.user_id, row.subject
            await self._fresh_capacity_authority(session, organization_id, actor, ("wms.configure",))
            if data.active:
                await self._responsibility_candidate(session, organization_id, target_id, target_sub)

            result = {
                "schema_version": 1, "kind": RESPONSIBLE_KIND, "organization_id": organization_id,
                "warehouse_id": warehouse_id, "operation_id": operation_id,
                "previous_revision": revision, "revision": revision + 1,
                "user_id": target_id, "active": data.active,
                "evidence": data.evidence, "reason": data.reason,
                RESPONSIBLE_PRIVATE: {
                    "setter_user_id": seed[7], "setter_subject": seed[2], "assignee_subject": target_sub,
                    "request": data.model_dump(mode="json"),
                },
            }
            operation = PhysicalOperation(
                organization_id=organization_id, warehouse_id=warehouse_id, operation_id=operation_id,
                kind=RESPONSIBLE_KIND, request_hash=request_hash, result=result, actor=seed[2],
            )
            receipt = responsible_recorded_receipt(operation)
            session.add(operation)
            # Flush the new referenced immutable operation before a binding UPDATE.
            await session.flush()
            if row is None:
                row = WarehouseResponsible(organization_id=organization_id, warehouse_id=warehouse_id)
                session.add(row)
            row.user_id, row.subject, row.active = target_id, target_sub, data.active
            row.revision, row.operation_id = revision + 1, operation_id
            await session.flush()
            if data.active:
                await self._responsibility_candidate(session, organization_id, target_id, target_sub)
            await self._fresh_capacity_authority(session, organization_id, actor, ("wms.configure",))
            return receipt.model_dump(mode="json", exclude={RESPONSIBLE_PRIVATE})

    async def register_assembly_station(
        self, session, organization_id, warehouse_id, data, actor,
    ):
        from core.services.auth import CurrentUser
        from modules.wms.physical_capacity import require_capacity_read_committed
        from modules.wms.physical_schemas import AssemblyStationRegister

        seed = self._caller_seed
        if (
            seed is None or type(seed[7]) is not int or seed[7] <= 0
            or not isinstance(seed[2], str) or not seed[2].strip()
            or actor != seed[2]
        ):
            reject("Требуется управляемая OIDC-учётная запись", 403)
        if not isinstance(data, AssemblyStationRegister):
            reject("Недопустимая регистрация станции сборки", 422)
        if not session.in_transaction():
            reject("Требуется транзакция регистрации станции сборки", 409)

        seed_uid, seed_subject = seed[7], seed[2]
        request_hash = digest({
            "organization_id": organization_id,
            "warehouse_id": warehouse_id,
            "actor_user_id": seed_uid,
            "actor_sub": seed_subject,
            "body": data.model_dump(mode="json"),
        })
        receipt_keys = {
            "schema_version", "operation_id", "kind", "organization_id",
            "warehouse_id", "location_id", "code", "title", "status",
            "evidence", "reason",
        }

        async def refresh_station_authority():
            await self._fresh_capacity_authority(
                session, organization_id, actor, ("wms.configure",),
            )
            if (
                not isinstance(self.user, CurrentUser)
                or not {"admin", "director"}.intersection(self.user.roles)
            ):
                reject("Регистрация станции сборки требует роль admin или director", 403)

        with session.no_autoflush:
            connection = await session.connection()
            await connection.run_sync(require_capacity_read_committed)
            await refresh_station_authority()

            # This warehouse-control row is the first WMS domain lock.
            await control(session, warehouse_id, organization_id, lock=True)
            await refresh_station_authority()

            operation_id = str(data.operation_id)
            existing = await session.scalar(select(PhysicalOperation).where(
                PhysicalOperation.organization_id == organization_id,
                PhysicalOperation.operation_id == operation_id,
            ))
            if existing is not None:
                if (
                    existing.kind != "assembly_station_register"
                    or existing.warehouse_id != warehouse_id
                    or existing.actor != seed_subject
                    or existing.request_hash != request_hash
                ):
                    reject("Идентификатор операции уже использован с другими данными")
                result = existing.result
                if (
                    not isinstance(result, dict)
                    or set(result) != receipt_keys
                    or type(result.get("schema_version")) is not int
                    or result.get("schema_version") != 1
                    or not isinstance(result.get("operation_id"), str)
                    or result.get("operation_id") != operation_id
                    or result.get("kind") != "assembly_station_register"
                    or type(result.get("organization_id")) is not int
                    or result.get("organization_id") != organization_id
                    or type(result.get("warehouse_id")) is not int
                    or result.get("warehouse_id") != warehouse_id
                    or type(result.get("location_id")) is not int
                    or result.get("location_id") <= 0
                    or not isinstance(result.get("code"), str)
                    or result.get("code") != data.code
                    or not isinstance(result.get("title"), str)
                    or result.get("title") != data.title
                    or result.get("status") != "registered"
                    or not isinstance(result.get("evidence"), str)
                    or result.get("evidence") != data.evidence
                    or not isinstance(result.get("reason"), str)
                    or result.get("reason") != data.reason
                ):
                    reject("Сохранённая квитанция станции сборки повреждена")
                await refresh_station_authority()
                return result

            directory = await session.get(WarehouseDirectory, warehouse_id)
            if (
                directory is None
                or directory.organization_id != organization_id
                or not directory.is_active
            ):
                reject("Склад организации не найден или неактивен", 404)
            if await session.scalar(select(PhysicalCount.id).where(
                PhysicalCount.warehouse_id == warehouse_id,
                PhysicalCount.status == "open",
            )) is not None:
                reject("Регистрация станции запрещена во время открытого пересчёта")
            if await session.scalar(select(Location.id).where(
                Location.warehouse == directory.name,
                Location.code == data.code,
            )) is not None:
                reject("Код станции уже зарегистрирован в справочнике адресов")
            if await session.scalar(select(PhysicalLocation.location_id).where(
                PhysicalLocation.warehouse_id == warehouse_id,
                PhysicalLocation.code == data.code,
            )) is not None:
                reject("Код станции уже зарегистрирован на физическом складе")

            original = Location(
                warehouse=directory.name,
                zone="assembly",
                code=data.code,
                title=data.title,
                is_active=True,
            )
            session.add(original)
            await session.flush()
            session.add(PhysicalLocation(
                location_id=original.id,
                warehouse_id=warehouse_id,
                code=data.code,
                kind="assembly",
            ))
            result = {
                "schema_version": 1,
                "operation_id": operation_id,
                "kind": "assembly_station_register",
                "organization_id": organization_id,
                "warehouse_id": warehouse_id,
                "location_id": original.id,
                "code": data.code,
                "title": data.title,
                "status": "registered",
                "evidence": data.evidence,
                "reason": data.reason,
            }
            session.add(PhysicalOperation(
                organization_id=organization_id,
                operation_id=operation_id,
                warehouse_id=warehouse_id,
                kind="assembly_station_register",
                request_hash=request_hash,
                result=result,
                actor=seed_subject,
            ))
            await session.flush()
            await refresh_station_authority()
            return result

    async def _execute_equipment_state(self, session, organization_id, data, actor):
        from modules.wms.physical_capacity import (
            equipment_operation_receipt,
            observe_equipment_state,
            require_capacity_read_committed,
        )
        from modules.wms.physical_models import cap_freeze_equipment_receipt
        from modules.wms.physical_schemas import normalize_equipment_request
        permissions = ("wms.operate", "wms.configure", "wms.approve")
        if not session.in_transaction():
            reject("equipment_route_transaction_required")
        with session.no_autoflush:
            connection = await session.connection()
            await connection.run_sync(require_capacity_read_committed)
            await self._fresh_capacity_authority(session, organization_id, actor, permissions)
            uid, subject = self._caller_seed[7], self._caller_seed[2]
            if actor != subject:
                reject("equipment_actor_subject_mismatch", 403)
            request_hash = digest({"organization_id": organization_id, "actor_user_id": uid,
                "actor_sub": subject, "request": normalize_equipment_request(data)})
            await control(session, data.warehouse_id, organization_id)
            await self._fresh_capacity_authority(session, organization_id, actor, permissions)
            existing = await session.scalar(select(PhysicalOperation).where(
                PhysicalOperation.organization_id == organization_id, PhysicalOperation.operation_id == str(data.operation_id),
            ))
            async def authority():
                await self._fresh_capacity_authority(session, organization_id, actor, permissions)
            if existing is not None:
                if existing.warehouse_id != data.warehouse_id or existing.request_hash != request_hash:
                    reject("equipment_operation_identity_conflict")
                columns = {key: getattr(existing, key) for key in
                    ("organization_id", "warehouse_id", "operation_id", "kind", "request_hash", "result", "actor")}
                value = await connection.run_sync(lambda conn: equipment_operation_receipt(columns, conn))
                await protect_blind_receipt(session, existing)
                await authority()
                cap_freeze_equipment_receipt(session.sync_session, value, insert=False, authority=authority, persistent_pk=existing.id)
                return value
            if session.new or session.dirty or session.deleted:
                reject("equipment_foreign_pending_changes")
            value = await observe_equipment_state(session, (await control(session, data.warehouse_id, organization_id)), data, uid, subject)
            await authority()
            cap_freeze_equipment_receipt(session.sync_session, value, insert=True, authority=authority)
            session.add(PhysicalOperation(organization_id=organization_id, warehouse_id=data.warehouse_id,
                operation_id=str(data.operation_id), kind=data.kind, request_hash=request_hash, result=value, actor=subject))
            await session.flush()
            await authority()
            return value

    async def _execute_measured_context(self, session, organization_id, data, actor):
        from modules.wms.physical_capacity import (
            approve_measured_context,
            measurement_public_result,
            measurement_receipt,
            measurement_state_head,
            require_capacity_read_committed,
        )
        from modules.wms.physical_models import cap_freeze_measurement_receipt
        from modules.wms.physical_schemas import normalize_measurements_request
        seed = self._caller_seed
        if seed is None or type(seed[7]) is not int or seed[7] <= 0 or not isinstance(seed[2], str) or not seed[2].strip():
            reject("Требуется управляемая OIDC-учётная запись",403)
        uid, subject, issued_at, roles = seed[7], seed[2], seed[6], tuple(seed[1])
        if issued_at is not None and (type(issued_at) is not int or issued_at < 0):
            reject("Некорректный срок исходной учётной записи",403)
        if not session.in_transaction():
            reject("Требуется транзакция маршрута вместимости",409)
        permissions=("wms.operate","wms.configure","wms.approve")
        async def authority():
            await self._fresh_capacity_authority(session,organization_id,actor,permissions)
            if (actor != subject or self.user.local_user_id != uid or self.user.keycloak_user_id != subject
                or self.user.issued_at != issued_at or tuple(self.user.roles) != roles):
                reject("Исходная учётная запись операции изменилась",403)
        with session.no_autoflush:
            connection=await session.connection()
            await connection.run_sync(require_capacity_read_committed)
            await authority()
            request=normalize_measurements_request(data)
            request_hash=digest({"organization_id":organization_id,"actor_user_id":uid,"actor_sub":subject,"request":request})
            warehouse=await control(session,data.warehouse_id,organization_id)
            await authority()
            existing=await session.scalar(select(PhysicalOperation).where(PhysicalOperation.organization_id==organization_id,PhysicalOperation.operation_id==str(data.operation_id)))
            if existing is not None:
                if existing.warehouse_id != data.warehouse_id or existing.kind != data.kind or existing.request_hash != request_hash:
                    reject("Идентификатор операции уже использован с другими данными",409)
                columns={key:getattr(existing,key) for key in ("id","organization_id","warehouse_id","operation_id","kind","request_hash","result","actor")}
                value=await connection.run_sync(lambda conn:measurement_receipt(existing.result,columns,conn))
                await connection.run_sync(lambda conn:measurement_state_head(conn,organization_id,data.warehouse_id))
                await protect_blind_projection(session,[data.warehouse_id])
                await authority()
                cap_freeze_measurement_receipt(session.sync_session,value,insert=False,authority=authority,persistent_pk=existing.id)
                return measurement_public_result(value,existing.kind)
            if session.new or session.dirty or session.deleted:
                reject("Транзакция метаданных содержит посторонние несохранённые изменения",409)
            approver={"user_id":uid,"sub":subject,"issued_at":issued_at,"roles":list(roles),
                "approved_at":datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00","Z")}
            value=await approve_measured_context(session,warehouse,data,approver)
            await authority()
            cap_freeze_measurement_receipt(session.sync_session,value,insert=True,authority=authority)
            session.add(PhysicalOperation(organization_id=organization_id,warehouse_id=data.warehouse_id,operation_id=str(data.operation_id),kind=data.kind,request_hash=request_hash,result=value,actor=subject))
            await session.flush()
            await authority()
            public=measurement_public_result(value,data.kind)
            return public

    async def execute_placement(self, session, organization_id, data, actor):
        from modules.wms.placement_service import execute_work_approval, execute_placement_lifecycle
        if data.kind == "capacity_work_approve":
            return await execute_work_approval(session, self, organization_id, data, actor)
        return await execute_placement_lifecycle(session, self, organization_id, data, actor)

    async def propose_placement(self, session, organization_id, data, actor):
        from modules.wms.placement_service import preview_placement
        return await preview_placement(session, self, organization_id, data, actor)

    async def execute_capacity(self, session, organization_id, data, actor):
        """One metadata receipt in the route-owned root transaction; never commit."""
        from modules.wms.physical_schemas import CapacityMeasurementsApprove, EquipmentStateObserve
        if isinstance(data, CapacityMeasurementsApprove):
            try:
                return await self._execute_measured_context(session, organization_id, data, actor)
            except ValueError as exc:
                reject(str(exc), 409)
        if isinstance(data, EquipmentStateObserve):
            try:
                return await self._execute_equipment_state(session, organization_id, data, actor)
            except ValueError as exc:
                reject(str(exc))
        from modules.wms.physical_capacity import (
            approve_profile,
            capacity_request_snapshot,
            confirm_resource,
            require_capacity_read_committed,
        )
        from modules.wms.physical_schemas import (
            CarrierMetadataResult,
            CarrierObserve,
            ProfileApprove,
            ProfileMetadataResult,
            ResourceConfirm,
            ResourceMetadataResult,
        )

        if isinstance(data, (ResourceConfirm, ProfileApprove)):
            permissions = ("wms.operate", "wms.configure", "wms.approve")
        elif isinstance(data, CarrierObserve):
            permissions = (
                ("wms.operate", "wms.approve")
                if data.confirm_measurements else ("wms.operate",)
            )
        else:
            reject("Недопустимая команда метаданных вместимости", 422)
        if not session.in_transaction():
            reject("Требуется транзакция маршрута вместимости", 409)

        with session.no_autoflush:
            connection = await session.connection()
            await connection.run_sync(require_capacity_read_committed)
            await self._fresh_capacity_authority(
                session, organization_id, actor, permissions,
            )
            request = capacity_request_snapshot(data)
            request_hash = digest(request)
            await control(session, data.warehouse_id, organization_id, lock=False)
            existing = await session.scalar(select(PhysicalOperation).where(
                PhysicalOperation.organization_id == organization_id,
                PhysicalOperation.operation_id == str(data.operation_id),
            ))
            if existing is not None:
                if (
                    existing.warehouse_id != data.warehouse_id
                    or existing.request_hash != request_hash
                ):
                    reject("Идентификатор операции уже использован с другими данными")
                await protect_blind_receipt(session, existing)
                return existing.result

            # These three DTOs address exactly one WH. Org lock is already held.
            warehouse = await control(session, data.warehouse_id, organization_id)
            # Re-read managed identity/membership/permissions after WH-lock wait.
            await self._fresh_capacity_authority(
                session, organization_id, actor, permissions,
            )
            existing = await session.scalar(select(PhysicalOperation).where(
                PhysicalOperation.organization_id == organization_id,
                PhysicalOperation.operation_id == str(data.operation_id),
            ))
            if existing is not None:
                if (
                    existing.warehouse_id != data.warehouse_id
                    or existing.request_hash != request_hash
                ):
                    reject("Идентификатор операции уже использован с другими данными")
                await protect_blind_receipt(session, existing)
                return existing.result

            if not isinstance(data, CarrierObserve) and (session.new or session.dirty or session.deleted):
                reject("Транзакция метаданных содержит посторонние несохранённые изменения", 409)
            if isinstance(data, ResourceConfirm):
                result_type = ResourceMetadataResult
                handler_result = await confirm_resource(session, warehouse, data, actor)
            elif isinstance(data, ProfileApprove):
                result_type = ProfileMetadataResult
                handler_result = await approve_profile(session, warehouse, data, actor)
            else:
                from modules.wms.physical_capacity import observe_carrier
                result_type = CarrierMetadataResult
                handler_result = await observe_carrier(session, warehouse, data, actor)
            handler_result = result_type.model_validate(handler_result).model_dump(mode="json")
            await session.flush()
            result = {
                "protocol": 2,
                "operation_id": str(data.operation_id),
                "kind": data.kind,
                "warehouse_id": data.warehouse_id,
                **handler_result,
            }
            session.add(PhysicalOperation(
                organization_id=organization_id,
                operation_id=str(data.operation_id),
                warehouse_id=data.warehouse_id,
                kind=data.kind,
                request_hash=request_hash,
                result=result,
                actor=actor,
            ))
            await session.flush()
            return result

    async def _fresh_native_receipt_authority(self, session, organization_id, actor):
        from core.domain.models import User

        seed = self._caller_seed
        permissions = ("wms.mobile",) if seed is not None and seed[1] == ("wms_mobile",) else ("wms.operate",)
        await self._fresh_capacity_authority(session, organization_id, actor, permissions)
        managed = await session.scalar(select(User).where(User.id == seed[7])
            .with_for_update().execution_options(populate_existing=True))
        if (managed is None or managed.id != seed[7] or managed.keycloak_user_id != seed[2]
            or managed.status != "active" or managed.role not in self.user.roles):
            reject("Требуется управляемая OIDC-учётная запись", 403)
        await self._fresh_capacity_authority(session, organization_id, actor, permissions)
        return {"protocol": 1, "user_id": seed[7], "sub": seed[2]}

    async def _native_replay_authority(self, session, organization_id, actor, existing):
        result = existing.result
        pack_owner_present = isinstance(result, dict) and "native_pack_owner_v1" in result
        if self.mobile_fulfillment_authority is not None:
            await self._require_mobile_pack_replay(session, organization_id, actor, existing)
        elif pack_owner_present:
            seed = self._caller_seed
            if seed is None or "wms_mobile" in seed[1]:
                reject("Запись не найдена", 404)
            native_pack_v1_projection(existing)
        owner_present = isinstance(result, dict) and "native_receipt_owner_v1" in result
        old_native = existing.kind == "receive" and isinstance(result, dict) and "native_receipt_source_v1" in result
        if owner_present or old_native:
            seed = self._caller_seed
            require_native_receipt_owner(result, seed[7] if seed is not None else None,
                seed[2] if seed is not None else None)
            if owner_present:
                # Strict native preflight precedes awaited assembly/origin validation.
                native_receipt_v2_projection(existing)
        await self._assembly_replay_authority(session, organization_id, actor, existing)
        if not owner_present and not old_native:
            return
        await self._fresh_native_receipt_authority(session, organization_id, actor)

    async def execute(self, session, organization_id, data, actor):
        """One command, one receipt, one database transaction owned by the route."""
        self._deltas = []  # Instances are request-local, never kept in the core container.
        self._causal_count_basis = None
        self._causal_delegated_move = None
        native_owner = None
        request = data.model_dump(mode="json")
        if data.kind == "pack_v2" and request["shipping_packages"] is None:
            request.pop("shipping_packages")
        from modules.wms.physical_delivery import normalize_legacy_optional_fields

        request = normalize_legacy_optional_fields(request, data.kind)
        measured_bound_pack = (
            data.kind == "pack_v2" and data.binding_revision_id is not None
            and data.shipping_packages is not None
        )
        mobile_pack_user = self._caller_seed is not None and self._caller_seed[1] == ("wms_mobile",)
        if (self.mobile_fulfillment_authority is not None and not measured_bound_pack
                or measured_bound_pack and mobile_pack_user and self.mobile_fulfillment_authority is None):
            reject("Недостаточно прав", 403)
        mobile_pack_owner_value = None
        if measured_bound_pack and self.mobile_fulfillment_authority is not None:
            mobile_pack_owner_value = await self._fresh_mobile_pack_authority(
                session, organization_id, data.warehouse_id, actor,
            )
        if data.kind == "ship":
            # Keep pre-existing operation hashes recoverable. New shipments must
            # supply scans below; an old committed receipt remains replayable.
            for key, default in (("container_scans", {}), ("shipment_mode", "full"), ("partial_reason", "")):
                if request[key] == default:
                    request.pop(key)
        request_hash = digest(request)
        transfer_command = data.kind in {"transfer_dispatch", "transfer_receive"}
        if transfer_command:
            if self._caller_seed is not None and self._caller_seed[1] == ("wms_mobile",):
                from modules.wms.mobile_access import OriginalMobileTransferAuthority

                authority = self.mobile_transfer_authority
                if (not isinstance(authority, OriginalMobileTransferAuthority)
                        or authority.identity.organization_id != organization_id
                        or authority.identity.warehouse_id != data.warehouse_id
                        or authority.action != data.kind):
                    reject("Недостаточно прав", 403)
            await self._fresh_transfer_authority(session, organization_id, actor)
        if data.kind in {
            "assembly_kit_move", "assembly_consume", "assembly_unused_return", "assembly_output", "assembly_qc", "assembly_return",
            "receive", "return", "quality", "maintenance", "move", "open_package",
            "pick", "unpick", "pack_v2", "ship", "task_complete", "count_approve",
            "transfer_dispatch", "transfer_receive",
        }:
            await _require_physical_causal_snapshot(session)
        assembly_command = data.kind in {"assembly_bind", "assembly_kit_move", "assembly_consume", "assembly_unused_return", "assembly_output", "assembly_qc", "assembly_return"}
        identity_command = await self._assembly_command_touches_identity(session, organization_id, data)
        if assembly_command or identity_command:
            await self._fresh_identity_authority(session, organization_id, actor, typed=assembly_command)
        # A replay remains scoped to the current organization/warehouse even
        # when its original BOM or approval has since become stale.
        initial_warehouse = await control(session, data.warehouse_id, organization_id, lock=False)
        existing = await session.scalar(select(PhysicalOperation).where(
            PhysicalOperation.organization_id == organization_id,
            PhysicalOperation.operation_id == str(data.operation_id)))
        if existing is not None:
            await self._native_replay_authority(session, organization_id, actor, existing)
            if existing.warehouse_id != data.warehouse_id or existing.request_hash != request_hash:
                reject("Идентификатор операции уже использован с другими данными")
            if existing.kind == "delivery_bind":
                from modules.wms.physical_delivery import validate_binding_replay

                await validate_binding_replay(session, existing)
            await protect_blind_receipt(session, existing)
            await self._native_replay_authority(session, organization_id, actor, existing)
            if transfer_command:
                await self._fresh_transfer_authority(session, organization_id, actor)
            return public_operation_result(existing.result, operation_kind=existing.kind)
        if data.kind == "receive" and initial_warehouse.onec_ref is None:
            native_owner = await self._fresh_native_receipt_authority(session, organization_id, actor)
        # Source and reservation prelocks must be complete before the first
        # Logistics gate; bound-source preparation is followed by a separate
        # collection phase before any warehouse control locks are taken.
        receipt_context = await self._prelock_receipt_source(session, organization_id, data, initial_warehouse)
        assembly_context = await self._prelock_assembly_source(session, organization_id, data)
        invoice_warehouse_ids = await self._prelock_invoice_source(session, organization_id, data)
        delivery_context = None
        if data.kind == "delivery_bind":
            from modules.wms.physical_delivery import prepare_delivery_binding

            delivery_context = await prepare_delivery_binding(session, self, organization_id, data)
        elif data.kind in {"task_claim", "pick", "pack_v2", "ship"}:
            from modules.wms.physical_delivery import prelock_command_binding

            delivery_context = await prelock_command_binding(
                session, self, organization_id, data, actor,
                mobile_authority=self.mobile_fulfillment_authority,
            )
        if data.kind == "delivery_bind" and delivery_context is not None:
            from modules.wms.physical_delivery import collect_delivery_binding

            delivery_context = await collect_delivery_binding(session, delivery_context)
        elif delivery_context is not None:
            from modules.wms.physical_delivery import collect_live_binding

            await collect_live_binding(session, self, delivery_context)
        if mobile_pack_owner_value is not None:
            mobile_pack_owner_value = await self._fresh_mobile_pack_authority(
                session, organization_id, data.warehouse_id, actor,
            )
        transfer_source = None
        if data.kind == "transfer_receive":
            dispatch, _ = await self._transfer_dispatch_receipt(session, organization_id, data)
            transfer_source = dispatch.warehouse_id
        warehouse_ids = sorted({data.warehouse_id, getattr(data, "destination_warehouse_id", None),
            transfer_source, *invoice_warehouse_ids} - {None})
        controls = {key: await control(session, key, organization_id) for key in warehouse_ids}
        warehouse = controls[data.warehouse_id]
        if transfer_command:
            await self._fresh_transfer_authority(session, organization_id, actor)
        identity_command = identity_command or await self._assembly_command_touches_identity(session, organization_id, data)
        if assembly_command or identity_command:
            await self._fresh_identity_authority(session, organization_id, actor, typed=assembly_command)
        # Recheck after scope/domain locks so concurrent identical requests recover
        # one immutable receipt instead of applying a second physical effect.
        existing = await session.scalar(select(PhysicalOperation).where(
            PhysicalOperation.organization_id == organization_id,
            PhysicalOperation.operation_id == str(data.operation_id)))
        if existing is not None:
            await self._native_replay_authority(session, organization_id, actor, existing)
            if existing.warehouse_id != data.warehouse_id or existing.request_hash != request_hash:
                reject("Идентификатор операции уже использован с другими данными")
            if existing.kind == "delivery_bind":
                from modules.wms.physical_delivery import validate_binding_replay

                await validate_binding_replay(session, existing)
            await protect_blind_receipt(session, existing)
            await self._native_replay_authority(session, organization_id, actor, existing)
            if transfer_command:
                await self._fresh_transfer_authority(session, organization_id, actor)
            return public_operation_result(existing.result, operation_kind=existing.kind)
        if data.kind == "receive" and (warehouse.onec_ref is None) != (receipt_context is not None):
            reject("Режим склада изменился; повторите исходную команду", 409)
        preparation = {"count_start", "count_record", "count_location_complete", "count_approve", "count_cancel", "cutover", "reserve", "release", "adopt_reservation"}
        if warehouse.mode != "erp" and data.kind not in preparation:
            reject("Рабочие операции доступны после согласованного переключения склада")
        if data.kind in {"transfer_dispatch", "transfer_receive"}:
            if any(row.mode != "erp" or row.onec_ref is not None for row in controls.values()):
                reject("Этапы межскладского перемещения доступны в автономных складах ERP")
            await protect_blind_projection(session, warehouse_ids)
        if data.kind == "pack":
            reject("Новая упаковка требует доказательство протокола Pack v2", 409)
        if warehouse.onec_ref is None and getattr(data, "depends_on", None) is not None:
            reject("Зависимости native-операций пока не поддерживаются", 409)
        if data.kind == "move":
            target = controls.get(data.destination_warehouse_id or warehouse.warehouse_id)
            if target is not None and (target.onec_ref is None) != (warehouse.onec_ref is None):
                reject("Нельзя перемещать остаток между native-складом и складом, связанным с 1С", 409)
        if native_owner is not None:
            await self._fresh_native_receipt_authority(session, organization_id, actor)
        handler = getattr(self, f"_{data.kind}")
        handler_kwargs = {}
        if data.kind == "delivery_bind":
            handler_kwargs.update(delivery_context=delivery_context, request_hash=request_hash)
        elif data.kind in {"task_claim", "pick"}:
            handler_kwargs["prechecked_delivery_context"] = delivery_context
        handled = await handler(session, warehouse, data, actor, controls,
            **({"assembly_context": assembly_context} if assembly_context is not None else {}),
            **({"receipt_context": receipt_context} if data.kind == "receive" else {}),
            **handler_kwargs)
        birth_plan = None
        binding_draft = None
        if data.kind == "assembly_output":
            result, exchange, birth_plan = handled
        elif data.kind == "delivery_bind":
            result, exchange, binding_draft = handled
        else:
            result, exchange = handled
        # Indirect allocation commands can discover their exact participant only
        # after the existing handler. Any denial rolls back that same caller TX.
        identity_command = identity_command or any(unit.assembly_output_id is not None for unit, *_ in self._deltas)
        if isinstance(result.get("unit"), dict) and result["unit"].get("assembly_output_id") is not None:
            identity_command = True
        for hold in (result.get("holds", []) if data.kind in {"reserve", "adopt_reservation"} else []):
            if await session.scalar(select(StockUnit.assembly_output_id).where(StockUnit.id == hold["unit_id"])) is not None:
                identity_command = True
        if data.kind == "replenish":
            task_ids = [task["id"] for task in result["tasks"]]
            if task_ids and await session.scalar(select(StockUnit.assembly_output_id).join(PhysicalTask,
                    PhysicalTask.unit_id == StockUnit.id).where(PhysicalTask.id.in_(task_ids),
                        StockUnit.assembly_output_id.is_not(None)).limit(1)) is not None:
                identity_command = True
        if identity_command:
            await self._fresh_identity_authority(session, organization_id, actor, typed=assembly_command)
        if transfer_command:
            await self._fresh_transfer_authority(session, organization_id, actor)
        result = {"operation_id": str(data.operation_id), "kind": data.kind, "warehouse_id": data.warehouse_id, **result}
        if exchange and warehouse.onec_ref is None:
            result["native_metadata"] = exchange

        stored_result = result
        if self._deltas or data.kind == "count_approve":
            count_basis = self._causal_count_basis or []
            affected_unit_ids = {str(change[0].id) for change in self._deltas}
            if data.kind == "assembly_return":
                affected_unit_ids.add(result["assembly_return_v1"]["shipped_unit_id"])
            affected_unit_ids.update(
                row["unit_id"] for row in count_basis if isinstance(row.get("unit_id"), str)
            )
            prior_frontiers = await capture_physical_prior_frontiers(
                session, organization_id, affected_unit_ids,
            )
            births = []
            if data.kind in {"assembly_output", "assembly_return"}:
                unit = result["unit"]
                births.append({"unit_id": unit["id"], "basis": data.kind, "source_unit_id": None})
            elif data.kind == "assembly_kit_move":
                link = await session.get(AssemblyInputLink, result["input_link_id"])
                births.append({"unit_id": result["unit"]["id"], "basis": "clone", "source_unit_id": link.original_unit_id})
            elif data.kind == "assembly_unused_return":
                for returned in result["returned"]:
                    link = await session.get(AssemblyInputLink, returned["input_link_id"])
                    births.append({"unit_id": returned["returned_unit_id"], "basis": "clone", "source_unit_id": link.actual_kit_unit_id})
            elif data.kind == "assembly_qc":
                births.append({"unit_id": result["unit"]["id"], "basis": "clone", "source_unit_id": str(data.unit_id)})
            elif data.kind == "receive":
                unit = result.get("unit")
                if unit is not None:
                    births.append({"unit_id": unit["id"], "basis": "received", "source_unit_id": None})
            elif data.kind in {"transfer_dispatch", "transfer_receive"}:
                moved = result["transfer"]
                child_id = moved["transit_unit_id"] if data.kind == "transfer_dispatch" else moved["received_unit_id"]
                births.append({"unit_id": child_id, "basis": "clone", "source_unit_id": str(data.unit_id)})
            elif data.kind in {"quality", "maintenance", "open_package", "move"}:
                unit = result.get("unit")
                if unit is not None:
                    births.append({"unit_id": unit["id"], "basis": "clone", "source_unit_id": str(data.unit_id)})
            elif data.kind == "task_complete":
                unit = result.get("unit")
                delegated_move = self._causal_delegated_move
                if unit is not None and delegated_move is not None:
                    births.append({"unit_id": unit["id"], "basis": "clone", "source_unit_id": delegated_move["unit_id"]})
            elif data.kind == "count_approve":
                births.extend({"unit_id": row["unit_id"], "basis": "approved_count", "source_unit_id": None}
                    for row in count_basis if row["created_unit"])

            causal = {"schema_version": 3 if data.kind in {"transfer_dispatch", "transfer_receive"} else 2, "canonical_request": request,
                "births": births, "count_basis": count_basis,
                "prior_frontiers": prior_frontiers}
            if data.kind == "task_complete":
                causal["delegated_move"] = self._causal_delegated_move
            stored_result = {**result, "physical_causal_v1": causal}

        if native_owner is not None:
            stored_result = {**stored_result, "native_receipt_owner_v1": native_owner}
        if mobile_pack_owner_value is not None:
            mobile_pack_owner_value = await self._fresh_mobile_pack_authority(
                session, organization_id, data.warehouse_id, actor,
            )
            stored_result = {**stored_result, "native_pack_owner_v1": mobile_pack_owner_value}
        op = PhysicalOperation(organization_id=organization_id, operation_id=str(data.operation_id),
            warehouse_id=data.warehouse_id, kind=data.kind, request_hash=request_hash, result=stored_result, actor=actor)
        session.add(op)
        await session.flush()
        if binding_draft is not None:
            binding = DeliveryBindingRevision(
                id=str(data.operation_id), binding_key=binding_draft["binding_key"],
                organization_id=organization_id, warehouse_id=data.warehouse_id,
                execution_id=binding_draft["execution_id"], revision=1,
                previous_revision_id=None, operation_pk=op.id,
                snapshot=binding_draft["snapshot"], digest=binding_draft["digest"],
            )
            session.add(binding)
            for task_id in binding_draft["task_ids"]:
                task = await session.get(PhysicalTask, task_id)
                if task is None or task.binding_revision_id is not None:
                    reject("Задание изменилось во время создания привязки", 409)
                task.binding_revision_id = binding.id
        if birth_plan is not None:
            identity_values, born_unit = birth_plan
            identity = AssemblyOutputIdentity(birth_operation_pk=op.id, **identity_values)
            session.add(identity)
            await session.flush()
            born_unit.assembly_output_id = identity.id
            await session.flush()
        for change in self._deltas:
            unit, delta = change[:2]
            snapshot = change[2] if len(change) == 3 else dimensions(unit)
            session.add(PhysicalLedger(operation_pk=op.id, unit_id=unit.id, qty_delta=delta, dimensions=snapshot))
        if exchange and warehouse.onec_ref is not None:
            dependencies = sorted(set(exchange.pop("dependencies", [])))
            if getattr(data, "depends_on", None):
                dependencies = sorted(set(dependencies) | {str(data.depends_on)})
            if op.operation_id in dependencies:
                reject("Операция обмена не может зависеть от самой себя", 422)
            for dependency in dependencies:
                if not await session.scalar(select(ExchangeJob.id).where(ExchangeJob.organization_id == organization_id,
                        ExchangeJob.operation_id == dependency)):
                    reject("Зависимость обмена должна указывать зарегистрированную операцию этой организации", 422)
            payload = {"protocol": 1, "operation_id": op.operation_id, "organization_id": organization_id,
                "warehouse_ref": warehouse.onec_ref, "kind": data.kind, "actor": actor,
                "operation_date": today().isoformat(), **exchange}
            session.add(ExchangeJob(operation_pk=op.id, organization_id=organization_id,
                operation_id=op.operation_id, payload=payload, payload_hash=digest(payload),
                dependencies=dependencies,
                depends_on=str(data.depends_on) if getattr(data, "depends_on", None) else None))
        if self.event_bus:
            self.event_bus.emit(session, "wms.physical.confirmed", {**public_operation_result(result, operation_kind=data.kind), "organization_id": organization_id})
        await session.flush()
        if warehouse.mode == "erp":
            await refresh_tasks(session, self.reference, warehouse)
            await plan_replenishment(session, self.reference, warehouse)
        if native_owner is not None:
            await self._fresh_native_receipt_authority(session, organization_id, actor)
        await self._assembly_whole_identity(session, organization_id)
        if assembly_command or identity_command:
            await self._fresh_identity_authority(session, organization_id, actor, typed=assembly_command)
            if data.kind == "assembly_qc":
                await self._assembly_qc_authority(session, organization_id, data, actor)
                await self._fresh_assembly_authority(session, organization_id, actor)
        if transfer_command:
            await self._fresh_transfer_authority(session, organization_id, actor)
        if mobile_pack_owner_value is not None:
            final_owner = await self._fresh_mobile_pack_authority(
                session, organization_id, data.warehouse_id, actor,
            )
            if final_owner != mobile_pack_owner_value:
                reject("Недостаточно прав", 403)
        return public_operation_result(result, operation_kind=data.kind)

    async def _assembly_replay_authority(self, session, organization_id, actor, existing):
        # Row.kind is authoritative even when a corrupt result pretends to be
        # legacy stock and would otherwise expose a server-private proof.
        public_operation_result(existing.result, operation_kind=existing.kind)
        kinds = {"assembly_bind", "assembly_kit_move", "assembly_consume", "assembly_unused_return", "assembly_output", "assembly_qc", "assembly_return"}
        if existing.kind not in kinds:
            if existing.kind in {"release", "replenish"} or await self._assembly_receipt_touches_identity(session, organization_id, existing):
                await self._fresh_identity_authority(session, organization_id, actor)
            return
        from modules.wms.assembly_quality import physical_assembly_proof
        from modules.wms.physical_schemas import (
            AssemblyBindProof,
            AssemblyConsumeProof,
            AssemblyOutputProof,
            AssemblyQcProof,
            AssemblyReturnProof,
        )

        await self._fresh_assembly_authority(session, organization_id, actor)
        definitions = {
            "assembly_bind": ("assembly_bind_proof_v1", AssemblyBindProof),
            "assembly_consume": ("assembly_consume_proof_v1", AssemblyConsumeProof),
            "assembly_output": ("assembly_output_v1", AssemblyOutputProof),
            "assembly_qc": ("assembly_qc_v1", AssemblyQcProof),
            "assembly_return": ("assembly_return_v1", AssemblyReturnProof),
        }
        if existing.kind in definitions:
            key, model = definitions[existing.kind]
            if key not in existing.result and existing.kind in {"assembly_bind", "assembly_consume"}:
                return  # Historical exact UUID recovery only; no fresh configured effects.
            proof = physical_assembly_proof(model, existing.result.get(key))
            original = proof.executor.original
            if (original.user_id, original.sub) != (self.user.local_user_id, self.user.keycloak_user_id):
                reject("Запись не найдена", 404)
        if existing.kind == "assembly_qc":
            from modules.wms.physical_models import AssemblyProcessRun
            identity, _prepared = await self._assembly_qc_authority(session, organization_id, proof.command, actor, recovery=True)
            pinned = await session.get(AssemblyProcessRun, identity.anchor["prepared_revision_id"])
            if (pinned is None or proof.anchor_digest != digest(identity.anchor)
                    or str(proof.command.confirmed_revision_id) != identity.anchor["confirmed_revision_id"]
                    or proof.criteria_digest != digest(pinned.content["qc"]["criteria"])
                    or str(proof.command.operation_id) != existing.operation_id
                    or proof.command.warehouse_id != existing.warehouse_id
                    or digest(proof.command.model_dump(mode="json")) != existing.request_hash
                    or existing.actor != original.sub
                    or (original.user_id, original.sub) != (
                        pinned.content["responsibility"]["qc_responsible_user_id"],
                        pinned.content["responsibility"]["qc_responsible_sub"])):
                reject("Историческое доказательство QC повреждено", 409)
            await self._fresh_assembly_authority(session, organization_id, actor)

    async def _fresh_identity_authority(self, session, organization_id, actor, *, typed=False):
        # The signed mobile route separately locks its exact device/grant/action.
        # A frozen role selects the existing fresh authority helper; it grants
        # neither a new action nor permission to bypass physical invariants.
        if not typed and self._caller_seed is not None and self._caller_seed[1] == ("wms_mobile",):
            await self._fresh_capacity_authority(session, organization_id, actor, ("wms.mobile",))
        else:
            await self._fresh_assembly_authority(session, organization_id, actor)

    async def _assembly_receipt_touches_identity(self, session, organization_id, operation):
        """Historical participants remain valid after move, pack, ship or task completion."""
        result = operation.result
        unit_ids = set(await session.scalars(select(PhysicalLedger.unit_id).where(
            PhysicalLedger.operation_pk == operation.id)))
        if "physical_causal_v1" in result:
            from modules.wms.item_identity import (
                _origin_operation_snapshot,
                _origin_validate_operation,
                _OriginUnproven,
            )
            try:
                _origin_validate_operation(_origin_operation_snapshot(operation), organization_id)
            except _OriginUnproven as exc:
                raise HTTPException(409, "Сохранённая причинная операция повреждена") from exc
        if isinstance(result.get("unit"), dict) and isinstance(result["unit"].get("id"), str):
            unit_ids.add(result["unit"]["id"])
        for hold in result.get("holds", []):
            if isinstance(hold, dict) and isinstance(hold.get("unit_id"), str):
                unit_ids.add(hold["unit_id"])
        if result.get("task_id") is not None:
            unit_ids.update(await session.scalars(select(PhysicalTask.unit_id).where(
                PhysicalTask.id == result["task_id"], PhysicalTask.warehouse_id == operation.warehouse_id)))
        if result.get("count_id") is not None:
            unit_ids.update(await session.scalars(select(PhysicalCountLine.unit_id).join(PhysicalCount,
                PhysicalCountLine.count_id == PhysicalCount.id).where(PhysicalCount.id == result["count_id"],
                    PhysicalCount.warehouse_id == operation.warehouse_id, PhysicalCountLine.unit_id.is_not(None))))
        if not unit_ids:
            return False
        return bool(await session.scalar(select(StockUnit.id).join(AssemblyOutputIdentity,
            StockUnit.assembly_output_id == AssemblyOutputIdentity.id).where(
                AssemblyOutputIdentity.organization_id == organization_id, StockUnit.id.in_(unit_ids)).limit(1)))

    async def _assembly_command_touches_identity(self, session, organization_id, data):
        """Addressed participants only; a historical zero elsewhere is unrelated."""
        unit_ids = set()
        if getattr(data, "unit_id", None) is not None:
            unit_ids.add(str(data.unit_id))
        if getattr(data, "hold_id", None) is not None:
            unit_id = await session.scalar(select(PhysicalHold.unit_id).where(PhysicalHold.id == data.hold_id,
                PhysicalHold.warehouse_id == data.warehouse_id))
            if unit_id is not None:
                unit_ids.add(unit_id)
        pick_ids = ([data.pick_id] if getattr(data, "pick_id", None) is not None else [])
        pick_ids.extend(line.pick_id for line in getattr(data, "lines", []) if hasattr(line, "pick_id"))
        if pick_ids:
            unit_ids.update(await session.scalars(select(PickedAllocation.unit_id).join(PhysicalHold,
                PickedAllocation.hold_id == PhysicalHold.id).where(PickedAllocation.id.in_(pick_ids),
                    PhysicalHold.warehouse_id == data.warehouse_id)))
        if getattr(data, "task_id", None) is not None:
            unit_id = await session.scalar(select(PhysicalTask.unit_id).where(PhysicalTask.id == str(data.task_id),
                PhysicalTask.warehouse_id == data.warehouse_id))
            if unit_id is not None:
                unit_ids.add(unit_id)
        if data.kind == "release":
            unit_ids.update(await session.scalars(select(PhysicalHold.unit_id).where(
                PhysicalHold.warehouse_id == data.warehouse_id, PhysicalHold.source == data.source,
                PhysicalHold.qty > PhysicalHold.released + PhysicalHold.picked)))
        if data.kind == "count_start":
            unit_ids.update(await session.scalars(select(StockUnit.id).where(
                StockUnit.warehouse_id == data.warehouse_id, StockUnit.location_id.in_(data.location_ids), StockUnit.qty > 0)))
        if data.kind == "count_approve":
            unit_ids.update(await session.scalars(select(PhysicalCountLine.unit_id).where(
                PhysicalCountLine.count_id == str(data.count_id), PhysicalCountLine.unit_id.is_not(None))))
        if data.kind in {"count_record", "return"} and getattr(data, "unit_id", None) is None:
            if await session.scalar(select(AssemblyOutputIdentity.id).where(
                AssemblyOutputIdentity.organization_id == organization_id,
                AssemblyOutputIdentity.sku_code == data.sku_code, AssemblyOutputIdentity.batch_id == data.batch_id).limit(1)):
                return True
        if not unit_ids:
            return False
        return bool(await session.scalar(select(StockUnit.id).join(AssemblyOutputIdentity,
            StockUnit.assembly_output_id == AssemblyOutputIdentity.id).where(
                AssemblyOutputIdentity.organization_id == organization_id, StockUnit.id.in_(unit_ids)).limit(1)))

    async def _assembly_whole_identity(self, session, organization_id):
        # Organization FOR UPDATE is already held by the actual route's scoped
        # membership guard (and every configured assembly freshness helper).
        rows = (await session.execute(select(StockUnit, AssemblyOutputIdentity, WarehouseControl.organization_id).join(
            AssemblyOutputIdentity, StockUnit.assembly_output_id == AssemblyOutputIdentity.id,
        ).join(WarehouseControl, StockUnit.warehouse_id == WarehouseControl.warehouse_id
        ).where(AssemblyOutputIdentity.organization_id == organization_id)
            .execution_options(populate_existing=True))).all()
        positive = defaultdict(int)
        for unit, identity, warehouse_organization_id in rows:
            if (warehouse_organization_id != organization_id
                    or unit.qty not in {Decimal(0), Decimal(1)} or unit.reserved not in {Decimal(0), Decimal(1)}
                    or unit.reserved > unit.qty or unit.sku_code != identity.sku_code or unit.batch_id != identity.batch_id):
                reject("Идентичность изделия допускает только целую единицу 0/1")
            positive[identity.id] += int(unit.qty > 0)
        if any(count > 1 for count in positive.values()):
            reject("Идентичность изделия уже имеет положительную физическую единицу")

    async def _assembly_configured(self, session, warehouse, context, *, binding=True):
        from modules.wms.assembly_quality import (
            physical_assembly_binding,
            physical_assembly_process,
        )

        specification = context["specification"]
        _source, approval = await self._assembly_authority(session, warehouse.organization_id, specification)
        anchor, inputs, prepared = await physical_assembly_process(session, warehouse.organization_id, specification.order_id)
        refs = prepared.content["source_refs"]
        if (refs["specification"] != {"id": specification.id, "revision": specification.revision, "digest": specification.digest}
                or refs["approval"]["id"] != approval.id):
            reject("Подтверждённый процесс не соответствует текущему источнику исполнения")
        if binding:
            await physical_assembly_binding(session, context["execution"], anchor)
        return anchor, inputs, prepared

    async def _assembly_qc_authority(self, session, organization_id, data, actor, *, recovery=False):
        from core.services.auth import has_permission
        from modules.wms.assembly_quality import (
            _head_prepared,
            _history,
            physical_assembly_identity,
            physical_assembly_process,
        )

        identity, _proof, _birth = await physical_assembly_identity(session, organization_id, str(data.identity_id))
        if not recovery:
            from modules.production.assembly_specifications import AssemblySpecification
            execution = await session.get(AssemblyExecution, identity.execution_id)
            specification = await session.get(AssemblySpecification, execution.specification_id)
            if specification is None:
                reject("Подтверждённый источник QC отсутствует")
            await self._assembly_authority(session, organization_id, specification)
        if recovery:
            _rows, head, by_id = await _history(session, organization_id, identity.order_id, lock=True)
            prepared = _head_prepared(head, by_id)
            if (head is None or head.kind != "confirmed" or prepared is None or prepared.kind != "prepared"
                    or head.prepared_revision_id != prepared.id):
                reject("Текущий назначенный ответственный требует подтверждённую историю процесса", 409)
        else:
            anchor, _inputs, prepared = await physical_assembly_process(session, organization_id, identity.order_id)
            if anchor != identity.anchor or str(data.confirmed_revision_id) != anchor["confirmed_revision_id"]:
                reject("Редакция процесса изменилась; прежнюю идентичность нельзя переинтерпретировать")
        nominee = (prepared.content["responsibility"]["qc_responsible_user_id"],
            prepared.content["responsibility"]["qc_responsible_sub"])
        await self._fresh_assembly_authority(session, organization_id, actor)
        if (not has_permission(self.core, self.user, "wms.approve")
                or (self.user.local_user_id, self.user.keycloak_user_id) != nominee):
            reject("QC требует текущего назначенного ответственного", 403)
        if not recovery and prepared.content["independence"]["rule"] == "independent_required" and any(
            (self.user.local_user_id, self.user.keycloak_user_id)
                == (executor["original"]["user_id"], executor["original"]["sub"])
            for executor in (identity.output_executor, identity.consume_executor)
        ):
            reject("QC должен выполнить независимый от фактического производства ответственный", 403)
        return identity, prepared

    async def _assembly_output(self, session, warehouse, data, actor, controls, *, assembly_context):
        from modules.wms.assembly_quality import (
            physical_assembly_executor,
            physical_assembly_first_gate,
            physical_assembly_genealogy,
            physical_assembly_proof,
        )
        from modules.wms.physical_schemas import AssemblyEffectiveLot, AssemblyOutputProof

        execution, specification, _source = await self._assembly_execution_authority(
            session, warehouse, assembly_context, allow_closed=True)
        anchor, inputs, _prepared = await self._assembly_configured(session, warehouse, assembly_context)
        if str(data.confirmed_revision_id) != anchor["confirmed_revision_id"] or data.policy_inputs_digest != anchor["policy_inputs_digest"]:
            reject("Команда выпуска требует точную подтверждённую редакцию правил")
        await physical_assembly_first_gate(session, anchor, data.output_key)
        await self._assembly_quota_state(session, warehouse.organization_id, execution.order_id)
        if data.output_key not in execution.consumed_output_keys:
            reject("Изделие требует полностью списанного комплекта")
        if await session.scalar(select(AssemblyOutputIdentity.id).where(
            AssemblyOutputIdentity.organization_id == warehouse.organization_id,
            AssemblyOutputIdentity.order_id == execution.order_id, AssemblyOutputIdentity.output_key == data.output_key,
        )) is not None:
            reject("Для ключа изделия выпуск уже зарегистрирован")
        genealogy, consumption, consume_operation = await physical_assembly_genealogy(session, execution, data.output_key, anchor)
        totals = defaultdict(Decimal)
        for row in genealogy:
            totals[row["specification_line_id"]] += Decimal(row["qty"])
        required = {str(line["legacy_bom_item_id"]): Decimal(line["quantity_per_output"])
            for line in specification.snapshot["exact_lines"]}
        if dict(totals) != required:
            reject("Генеалогия не доказывает точную норму каждого компонента")
        sku = specification.snapshot["output"]["sku_code"]
        lot = await batch(session, self.reference, warehouse.organization_id, data.batch_id, sku)
        snapshot = assembly_lot_snapshot(lot, data.batch_id, sku)
        effective = dict(snapshot)
        operator_report = False
        for field, configured in (("mfg_date", inputs.manufacturing_policy), ("expiry_date", inputs.expiry_policy)):
            raw = getattr(data, field)
            value = raw.isoformat() if raw is not None else None
            rules = configured.rules
            if rules.date_requirement == "not_applicable":
                if value is not None:
                    reject("Не применяемое правило требует null даты", 422)
            elif rules.date_source == "operator_report":
                if value is None:
                    reject("Требуется явно сообщённая оператором дата", 422)
                if (snapshot[field] is not None and snapshot[field] != value) or (
                    field == "mfg_date" and snapshot[field] is not None and snapshot["manufacture_precision"] != "day"
                ):
                    reject("Дата оператора противоречит подтверждённой партии", 422)
                effective[field] = value
                if field == "mfg_date":
                    effective["manufacture_precision"] = "day"
                operator_report = True
            else:
                if value is not None or snapshot[field] is None:
                    reject("Правило output_batch требует подтверждённую дату партии и null вход", 422)
        if effective["mfg_date"] and effective["expiry_date"] and effective["mfg_date"] > effective["expiry_date"]:
            reject("Дата изготовления позже окончания годности", 422)
        if operator_report:
            effective["date_source"] = f"assembly_operator_report:{data.operation_id}"
        effective = physical_assembly_proof(AssemblyEffectiveLot, effective).model_dump(mode="json")
        serial_rules = inputs.serial_policy.rules
        serial, scope = data.serial_number, serial_rules.uniqueness_scope
        if serial_rules.requirement == "not_applicable":
            if serial is not None:
                reject("Не применяемая сериализация требует null номера", 422)
        elif serial is None:
            reject("Требуется серийный номер изделия", 422)
        if serial is not None:
            # Every birth path holds accounting.organization FOR UPDATE. The
            # mixed-scope predicate includes shipped/zero immutable identities.
            conflict = select(AssemblyOutputIdentity.id).where(
                AssemblyOutputIdentity.organization_id == warehouse.organization_id,
                AssemblyOutputIdentity.serial_number == serial,
            )
            if scope != "organization":
                conflict = conflict.where(or_(AssemblyOutputIdentity.serial_scope == "organization", AssemblyOutputIdentity.sku_code == sku))
            if await session.scalar(conflict.limit(1)) is not None:
                reject("Серийный номер уже закреплён за неизменной идентичностью изделия")
        place = await location(session, warehouse.warehouse_id, data.location_id)
        if place.kind != "quarantine":
            reject("Выпуск требует активную карантинную зону склада")
        await policy(session, warehouse.warehouse_id, sku, Decimal(1))
        await capacity(session, place, sku, Decimal(1))
        await self._fresh_assembly_authority(session, warehouse.organization_id, actor)
        executor = physical_assembly_executor(self._caller_seed, self.user)
        identity_id = str(uuid4())
        unit = StockUnit(id=str(uuid4()), stock_state="on_hand", dispatch_operation_id=None,
            warehouse_id=warehouse.warehouse_id, location_id=place.location_id,
            sku_code=sku, batch_id=data.batch_id, characteristic="", purpose="", qty=Decimal(1), reserved=Decimal(0),
            quality="quarantine", packaging_id=None, package_state="loose", container="", last_checked=None,
            next_service=None, inspection_ref="", service_result=None, external_operations=[],
            arrived_at=datetime.now(timezone.utc), arrival_source=f"assembly_output:{data.operation_id}")
        session.add(unit)
        await session.flush()
        self._deltas.append((unit, Decimal(1)))
        proof = physical_assembly_proof(AssemblyOutputProof, {"protocol": 1, "identity_id": identity_id,
            "execution_id": execution.id, "output_key": data.output_key, "anchor": anchor,
            "genealogy_digest": digest(genealogy), "consume_operation_id": consume_operation.operation_id,
            "executor": executor, "effective_lot": effective}).model_dump(mode="json")
        identity_values = {"id": identity_id, "organization_id": warehouse.organization_id,
            "order_id": execution.order_id, "execution_id": execution.id, "output_key": data.output_key,
            "sku_code": sku, "batch_id": data.batch_id, "serial_number": serial, "serial_scope": scope,
            "anchor": anchor, "genealogy": genealogy, "genealogy_digest": digest(genealogy),
            "batch_snapshot": snapshot, "effective_lot": effective, "output_executor": executor,
            "consume_executor": consumption.executor.model_dump(mode="json")}
        return {"unit": {**unit_output(unit), "assembly_output_id": identity_id}, "assembly_output_v1": proof}, None, (identity_values, unit)

    async def _assembly_qc(self, session, warehouse, data, actor, controls, *, assembly_context):
        from modules.wms.assembly_quality import (
            physical_assembly_executor,
            physical_assembly_proof,
            physical_assembly_qc_records,
            physical_assembly_qc_verdict,
        )
        from modules.wms.item_identity import assembly_unit_qc_birth
        from modules.wms.physical_schemas import AssemblyQcProof

        self._assembly_only_native(warehouse)
        identity, prepared = await self._assembly_qc_authority(session, warehouse.organization_id, data, actor)
        unit = await self._unit(session, warehouse, data.unit_id)
        if unit.assembly_output_id != identity.id or unit.qty != 1 or unit.reserved != 0 or unit.quality != "quarantine":
            reject("QC требует свободную целую карантинную единицу этой идентичности")
        birth_operation_id = await assembly_unit_qc_birth(session, warehouse.organization_id, unit)
        if str(data.qc_birth_operation_id) != birth_operation_id:
            reject("QC относится к другой исходной или возвращённой единице")
        if await physical_assembly_qc_records(session, identity.id, warehouse.organization_id, birth_operation_id):
            reject("Для этой физической регистрации уже сохранён терминальный QC")
        criteria = prepared.content["qc"]["criteria"]
        verdict = physical_assembly_qc_verdict(data, criteria)
        await effective_lot(session, self.reference, warehouse.organization_id, unit)
        checked = unit
        if verdict == "PASS":
            reason, _lot = await eligible(session, self.reference, warehouse, unit, quality=False)
            if reason:
                reject(f"QC не отменяет ограничения хранения: {reason}")
            unit.qty -= Decimal(1)
            checked = self._clone(session, unit, Decimal(1), quality="released", inspection_ref=data.evidence)
            self._deltas.extend([(unit, Decimal(-1)), (checked, Decimal(1))])
            await session.flush()
        await self._assembly_qc_authority(session, warehouse.organization_id, data, actor)
        proof = physical_assembly_proof(AssemblyQcProof, {"protocol": 1, "command": data.model_dump(mode="json"),
            "anchor_digest": digest(identity.anchor), "executor": physical_assembly_executor(self._caller_seed, self.user),
            "criteria_digest": digest(criteria), "verdict": verdict,
            "released_unit_id": checked.id if verdict == "PASS" else None}).model_dump(mode="json")
        return {"unit": unit_output(checked), "assembly_qc_v1": proof}, None

    async def _assembly_return(self, session, warehouse, data, actor, controls, *, assembly_context):
        from modules.wms.assembly_quality import (
            physical_assembly_executor,
            physical_assembly_identity,
            physical_assembly_proof,
        )
        from modules.wms.item_identity import prove_assembly_shipment_return
        from modules.wms.physical_schemas import AssemblyReturnProof

        self._assembly_only_native(warehouse)
        identity, _output, _birth = await physical_assembly_identity(session, warehouse.organization_id, str(data.identity_id))
        proof_values = await prove_assembly_shipment_return(session, warehouse.organization_id, warehouse.warehouse_id,
            identity, str(data.ship_operation_id), data.pick_id)
        previous = await session.scalar(select(PhysicalOperation.id).where(
            PhysicalOperation.organization_id == warehouse.organization_id, PhysicalOperation.kind == "assembly_return",
            PhysicalOperation.result["assembly_return_v1"]["identity_id"].as_string() == identity.id,
            PhysicalOperation.result["assembly_return_v1"]["ship_operation_id"].as_string() == str(data.ship_operation_id),
            PhysicalOperation.result["assembly_return_v1"]["pick_id"].as_integer() == data.pick_id,
        ).limit(1))
        if previous is not None:
            reject("Возврат этой точной отгрузки уже зарегистрирован")
        if await session.scalar(select(StockUnit.id).where(StockUnit.assembly_output_id == identity.id, StockUnit.qty > 0).limit(1)):
            reject("Идентичность уже имеет положительный физический остаток")
        place = await location(session, warehouse.warehouse_id, data.location_id)
        if place.kind != "returns":
            reject("Возврат требует отдельную зону контроля")
        await policy(session, warehouse.warehouse_id, identity.sku_code, Decimal(1))
        await capacity(session, place, identity.sku_code, Decimal(1))
        await self._fresh_assembly_authority(session, warehouse.organization_id, actor)
        unit = StockUnit(id=str(uuid4()), stock_state="on_hand", dispatch_operation_id=None,
            assembly_output_id=identity.id, warehouse_id=warehouse.warehouse_id,
            location_id=place.location_id, sku_code=identity.sku_code, batch_id=identity.batch_id,
            characteristic="", purpose="", qty=Decimal(1), reserved=Decimal(0), quality="quarantine",
            packaging_id=None, package_state="loose", container="", last_checked=None, next_service=None,
            inspection_ref="", service_result=None, external_operations=[], arrived_at=datetime.now(timezone.utc),
            arrival_source=f"assembly_return:{data.operation_id}")
        session.add(unit)
        await session.flush()
        current_lot = await batch(session, self.reference, warehouse.organization_id, identity.batch_id, identity.sku_code)
        if assembly_lot_snapshot(current_lot, identity.batch_id, identity.sku_code) != identity.batch_snapshot:
            reject("Подтверждённая партия возвращаемого изделия изменилась")
        self._deltas.append((unit, Decimal(1)))
        proof = physical_assembly_proof(AssemblyReturnProof, {"protocol": 1, **proof_values,
            "returned_unit_id": unit.id, "executor": physical_assembly_executor(self._caller_seed, self.user)}).model_dump(mode="json")
        return {"unit": unit_output(unit), "assembly_return_v1": proof}, None

    async def _fresh_assembly_authority(self, session, organization_id, actor):
        from core.services.auth import (
            CurrentUser,
            EffectiveIdentityLookupError,
            has_permission,
            resolve_effective_oidc_user,
        )

        seed = self._caller_seed
        if (
            self.core is None or seed is None
            or type(seed[7]) is not int or seed[7] <= 0
            or not isinstance(seed[2], str) or not seed[2].strip()
        ):
            reject("Для физической сборки требуется проверенная управляемая OIDC-учётная запись", 403)
        seed_uid, seed_subject, seed_issued_at = seed[7], seed[2], seed[6]

        async def refresh():
            try:
                fresh = await resolve_effective_oidc_user(
                    CurrentUser(
                        seed[0], list(seed[1]),
                        keycloak_user_id=seed[2],
                        local_status=None,
                        deal_visibility=seed[4],
                        crm_restricted=seed[5],
                        issued_at=seed[6],
                        local_user_id=seed[7],
                    ),
                    session,
                )
            except EffectiveIdentityLookupError as exc:
                raise HTTPException(
                    503, "Текущую учётную запись сборки нельзя проверить",
                ) from exc
            if (
                fresh.local_user_id != seed_uid
                or fresh.keycloak_user_id != seed_subject
                or fresh.issued_at != seed_issued_at
                or fresh.local_status != "active"
            ):
                reject("Текущая управляемая OIDC-учётная запись сборки неактивна", 403)
            return fresh

        fresh = await refresh()
        accounting = getattr(getattr(self.core, "services", None), "accounting", None)
        if accounting is None:
            reject("Сервис организации недоступен для проверки сборки", 503)
        try:
            await accounting.source_member(session, organization_id, fresh)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, "Текущее членство организации нельзя проверить") from exc

        # Re-read the managed principal after the organization lock. A deletion,
        # deactivation, or role change while resolving membership must also fence
        # historical UUID recovery, not only new physical effects.
        fresh = await refresh()
        try:
            current_actor = await accounting.source_member(
                session, organization_id, fresh,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, "Текущее членство организации нельзя проверить") from exc

        # Re-resolve after the second membership await before deciding permission.
        fresh = await refresh()
        if current_actor != actor or not has_permission(self.core, fresh, "wms.operate"):
            reject("Текущая складская роль больше не разрешает физическую сборку", 403)
        self.user = fresh

    async def _native_receipt_history(self, session, organization_id, source, *, owners=None, legacy_caller=False):
        """Validate receive ownership before applying the requested source quota."""
        from modules.procurement.source_gateway import native_receipt_snapshot
        from modules.wms.models import Receipt, ReceiptLine
        from modules.wms.physical_schemas import Receive
        from modules.wms.primary_receipts import PrimaryReceiptBinding, PrimaryReceiptCommand

        service = self.services.procurement_source
        prelock = owners is None
        owners = {} if prelock else dict(owners)
        totals, sources, request_keys, unit_ids, line_ids = {}, {}, set(), set(), set()
        requested_receipt_id = source["receipt_id"]
        has_native_for_source = False

        def quantity(value, *, compact=False):
            if not isinstance(value, str):
                raise ValueError("history_quantity")
            number = Decimal(value)
            if not number.is_finite() or number <= 0 or number != number.quantize(Decimal("0.000001")):
                raise ValueError("history_quantity")
            if compact and decimal_text(number) != value:
                raise ValueError("history_quantity")
            return number

        async def archived(receipt_id, version):
            if any(type(value) is not int or not 1 <= value <= 2147483647 for value in (receipt_id, version)):
                raise ValueError("history_owner")
            key = (receipt_id, version)
            if key not in owners:
                if not prelock:
                    raise ValueError("history_owner_not_prelocked")
                owners[key] = await service.native_receipt_revision_source(
                    session, organization_id, receipt_id, version,
                )
            if any(type(fact.get("sku_id")) is not int or not 1 <= fact["sku_id"] <= 2147483647
                for fact in owners[key]["lines"]):
                raise ValueError("history_identity")
            return owners[key]

        def account(snapshot, position, amount):
            receipt_id = snapshot["receipt_id"]
            previous = sources.setdefault(receipt_id, snapshot)
            if canonical(previous) != canonical(snapshot):
                raise ValueError("history_revision_conflict")
            if type(position) is not int or not 1 <= position <= len(snapshot["lines"]):
                raise ValueError("history_position")
            key = (receipt_id, position)
            totals[key] = totals.get(key, Decimal(0)) + amount
            if totals[key] > quantity(snapshot["lines"][position - 1]["quantity"]):
                raise ValueError("history_overallocated")

        try:
            rows = (await session.scalars(select(PhysicalOperation).where(
                PhysicalOperation.organization_id == organization_id, PhysicalOperation.kind == "receive",
            ).order_by(PhysicalOperation.id).execution_options(populate_existing=True))).all()
            for row in rows:
                result = row.result
                binding = result.get("native_receipt_source_v1") if isinstance(result, dict) else None
                required = {"protocol", "organization_id", "warehouse_id", "source_ref", "receipt_id",
                    "version", "position", "source_snapshot", "sku_id", "sku_code", "unit",
                    "batch_id", "lot_no", "coefficient", "allocated_qty"}
                if not isinstance(binding, dict) or set(binding) != required:
                    raise ValueError("unresolved_receive_history")
                for key in ("protocol", "organization_id", "warehouse_id", "receipt_id", "version", "position", "sku_id", "batch_id"):
                    if type(binding[key]) is not int or not 1 <= binding[key] <= 2147483647:
                        raise ValueError("history_identity")
                snapshot = await archived(binding["receipt_id"], binding["version"])
                if canonical(binding["source_snapshot"]) != canonical(snapshot):
                    raise ValueError("history_owner_facts")
                position = binding["position"]
                if not 1 <= position <= len(snapshot["lines"]):
                    raise ValueError("history_position")
                fact = snapshot["lines"][position - 1]
                if (binding["protocol"] != 1 or binding["organization_id"] != organization_id
                    or binding["warehouse_id"] != row.warehouse_id or snapshot["warehouse_id"] != row.warehouse_id
                    or binding["source_ref"] != fact["source_line"] or binding["coefficient"] != "1"
                    or binding["sku_id"] != fact["sku_id"] or binding["sku_code"] != fact["sku"]
                    or binding["unit"] != fact["unit"] or binding["lot_no"] != fact["lot"]):
                    raise ValueError("history_mapping")
                amount = quantity(binding["allocated_qty"], compact=True)
                old_keys = {"operation_id", "kind", "warehouse_id", "unit", "source_ref",
                    "native_metadata", "native_receipt_source_v1", "physical_causal_v1"}
                if set(result) not in (old_keys, old_keys | {"native_receipt_owner_v1"}):
                    raise ValueError("history_receipt_shape")
                if "native_receipt_owner_v1" in result:
                    owner = native_receipt_owner(result)
                    if owner["sub"] != row.actor:
                        raise ValueError("history_receipt_owner")
                causal = result["physical_causal_v1"]
                if (not isinstance(causal, dict) or set(causal) != {"schema_version", "canonical_request", "births", "count_basis", "prior_frontiers"}
                    or type(causal["schema_version"]) is not int or causal["schema_version"] != 2):
                    raise ValueError("history_causal")
                request = Receive.model_validate(causal["canonical_request"])
                if (request.kind != "receive" or canonical(request.model_dump(mode="json")) != canonical(causal["canonical_request"])
                    or digest(causal["canonical_request"]) != row.request_hash
                    or str(request.operation_id) != row.operation_id or request.warehouse_id != row.warehouse_id
                    or request.source_ref != binding["source_ref"] or request.sku_code != binding["sku_code"]
                    or request.batch_id != binding["batch_id"] or request.qty != amount or request.depends_on is not None):
                    raise ValueError("history_request")
                unit = result["unit"]
                if not isinstance(unit, dict) or not _origin_uuid_text(unit.get("id")) or unit["id"] in unit_ids:
                    raise ValueError("history_unit")
                unit_ids.add(unit["id"])
                actual = await session.scalar(select(StockUnit).where(StockUnit.id == unit["id"])
                    .execution_options(populate_existing=True))
                if actual is None or actual.warehouse_id != row.warehouse_id:
                    raise ValueError("history_unit")
                dims = dimensions(actual)
                expected_unit = {**unit_output(actual), "qty": unit["qty"],
                    "reserved": "0", "free": unit["free"]}
                if (quantity(unit["qty"]) != amount or quantity(unit["free"]) != amount):
                    raise ValueError("history_unit_quantity")
                if (canonical(unit) != canonical(expected_unit)
                    or unit["location_id"] != request.location_id or unit["sku_code"] != request.sku_code
                    or unit["batch_id"] != request.batch_id or unit["characteristic"] != request.characteristic
                    or unit["purpose"] != request.purpose or unit["packaging_id"] != request.packaging_id
                    or unit["package_state"] != request.package_state or unit["quality"] != "quarantine"
                    or result["operation_id"] != row.operation_id or result["kind"] != row.kind
                    or type(result["warehouse_id"]) is not int or result["warehouse_id"] != row.warehouse_id
                    or result["source_ref"] != request.source_ref):
                    raise ValueError("history_unit_facts")
                sku = await session.scalar(select(Sku).where(Sku.id == binding["sku_id"])
                    .execution_options(populate_existing=True))
                lot = await self.reference.batch_by_id(session, organization_id, binding["batch_id"])
                if (sku is None or sku.code != binding["sku_code"] or sku.unit != binding["unit"]
                    or not isinstance(lot, dict) or lot.get("organization_id") != organization_id
                    or lot.get("sku_code") != binding["sku_code"] or lot.get("lot_no") != binding["lot_no"]):
                    raise ValueError("history_catalogue_mapping")
                expected_metadata = {"source_ref": request.source_ref, "dependencies": [],
                    "lines": [{**dims, "qty": unit["qty"], "lot": binding["lot_no"]}]}
                ledgers = (await session.scalars(select(PhysicalLedger).where(
                    PhysicalLedger.operation_pk == row.id))).all()
                if (canonical(result["native_metadata"]) != canonical(expected_metadata)
                    or causal["births"] != [{"unit_id": unit["id"], "basis": "received", "source_unit_id": None}]
                    or causal["count_basis"] != [] or not isinstance(causal["prior_frontiers"], dict)
                    or len(ledgers) != 1 or ledgers[0].unit_id != unit["id"] or ledgers[0].qty_delta != amount
                    or canonical(ledgers[0].dimensions) != canonical(dims) or row.operation_id in request_keys):
                    raise ValueError("history_physical_allocation")
                request_keys.add(row.operation_id)
                # Exclude another owner only after its complete allocation proof.
                if snapshot["receipt_id"] == requested_receipt_id:
                    has_native_for_source = True
                    account(snapshot, position, amount)

            if legacy_caller and not has_native_for_source:
                return {"owners": owners, "allocated": {}, "has_native_for_source": False}
            if legacy_caller:
                source = native_receipt_snapshot(source)
            sources.setdefault(source["receipt_id"], source)
            if canonical(sources[source["receipt_id"]]) != canonical(source):
                raise ValueError("history_current_revision_changed")
            bindings = (await session.scalars(select(PrimaryReceiptBinding).where(
                PrimaryReceiptBinding.organization_id == organization_id,
                PrimaryReceiptBinding.source_receipt_id == requested_receipt_id,
            ).order_by(PrimaryReceiptBinding.receipt_id).execution_options(populate_existing=True))).all()
            for binding in bindings:
                snapshot = await archived(binding.source_receipt_id, binding.source_version)
                expected_source = sources[binding.source_receipt_id]
                saved = binding.source_snapshot
                if (canonical(snapshot) != canonical(expected_source) or not isinstance(saved, dict)
                    or set(saved) != {"organization_id", "receipt_id", "version", "source", "document", "lines"}
                    or canonical(native_receipt_snapshot(saved)) != canonical(snapshot)
                    or type(saved["organization_id"]) is not int or saved["organization_id"] != organization_id
                    or type(saved["receipt_id"]) is not int or saved["receipt_id"] != binding.source_receipt_id
                    or type(saved["version"]) is not int or saved["version"] != binding.source_version):
                    raise ValueError("legacy_owner_facts")
                expected_lines = [{key: fact[key] for key in ("source_line", "position", "sku", "lot", "quantity", "unit")}
                    for fact in snapshot["lines"]]
                saved_lines = saved["lines"]
                if not isinstance(saved_lines, list) or len(saved_lines) != len(expected_lines):
                    raise ValueError("legacy_source_lines")
                for original, expected in zip(saved_lines, expected_lines, strict=True):
                    if (not isinstance(original, dict) or set(original) != set(expected)
                        or quantity(original["quantity"]) != quantity(expected["quantity"])
                        or canonical({**original, "quantity": expected["quantity"]}) != canonical(expected)):
                        raise ValueError("legacy_source_lines")
                command = PrimaryReceiptCommand.model_validate(binding.command)
                if (canonical(command.model_dump(mode="json")) != canonical(binding.command)
                    or command.expected_version != binding.source_version or str(command.request_key) != binding.request_key
                    or type(command.warehouse_id) is not int or not 1 <= command.warehouse_id <= 2147483647
                    or binding.request_key in request_keys):
                    raise ValueError("legacy_command_or_duplicate_accounting")
                # The physical target is the saved command warehouse; owner WH
                # is independent. Historical inactive allocations still consume quota.
                target_query = select(WarehouseDirectory).where(
                    WarehouseDirectory.id == command.warehouse_id,
                    WarehouseDirectory.organization_id == organization_id,
                    WarehouseDirectory.name == command.warehouse,
                ).execution_options(populate_existing=True)
                if prelock:
                    target_query = target_query.with_for_update(read=True)
                if await session.scalar(target_query) is None:
                    raise ValueError("legacy_target_warehouse")
                request_keys.add(binding.request_key)
                receipt = await session.get(Receipt, binding.receipt_id, populate_existing=True)
                lines = (await session.scalars(select(ReceiptLine).where(ReceiptLine.receipt_id == binding.receipt_id)
                    .execution_options(populate_existing=True))).all()
                entries = binding.line_bindings
                if (receipt is None or receipt.organization_id != organization_id or receipt.source != "procurement"
                    or receipt.warehouse != command.warehouse
                    or receipt.entity_ref != f"procurement:receipt:{binding.source_receipt_id}:{binding.source_version}"
                    or not isinstance(entries, list) or len(entries) != len(command.lines) or len(lines) != len(entries)):
                    raise ValueError("legacy_physical_receipt")
                by_id, selected = {line.id: line for line in lines}, {line.position: line for line in command.lines}
                positions = set()
                for entry in entries:
                    if (not isinstance(entry, dict) or set(entry) != {"receipt_line_id", "source_line", "position", "sku_code", "quantity", "unit"}
                        or type(entry["receipt_line_id"]) is not int or type(entry["position"]) is not int
                        or entry["receipt_line_id"] in line_ids or entry["position"] in positions):
                        raise ValueError("legacy_line_binding")
                    position = entry["position"]
                    chosen = selected.get(position)
                    actual_line = by_id.get(entry["receipt_line_id"])
                    if chosen is None or actual_line is None or not 1 <= position <= len(snapshot["lines"]):
                        raise ValueError("legacy_line_binding")
                    fact = snapshot["lines"][position - 1]
                    amount = quantity(entry["quantity"])
                    sku = await session.scalar(select(Sku).where(Sku.id == fact["sku_id"])
                        .execution_options(populate_existing=True))
                    if (entry["source_line"] != fact["source_line"] or entry["unit"] != fact["unit"]
                        or entry["sku_code"] != fact["sku"] or chosen.sku_code != fact["sku"]
                        or amount != chosen.quantity or actual_line.expected_qty != amount
                        or actual_line.sku_code != fact["sku"] or actual_line.batch_ref != fact["lot"]
                        or sku is None or sku.code != fact["sku"] or sku.unit != fact["unit"]):
                        raise ValueError("legacy_line_mapping")
                    line_ids.add(entry["receipt_line_id"])
                    positions.add(position)
                    account(snapshot, position, amount)
            return {"owners": owners, "has_native_for_source": has_native_for_source,
                "allocated": {position: amount for (receipt_id, position), amount in totals.items()
                    if receipt_id == requested_receipt_id}}
        except (ValueError, KeyError, TypeError, IndexError, InvalidOperation):
            reject("Native-поступление пока не готово: требуется проверка истории источников", 409)


    async def _prelock_receipt_source(self, session, organization_id, data, initial_warehouse):
        if data.kind != "receive" or initial_warehouse.onec_ref is not None:
            return None
        match = re.fullmatch(r"procurement:receipt:([1-9][0-9]*):([1-9][0-9]*):([1-9][0-9]*)", data.source_ref)
        service = getattr(self.services, "procurement_source", None) if self.services is not None else None
        if match is None or service is None:
            reject("Native-поступление требует точную строку первичного документа закупки", 409)
        receipt_id, version, position = map(int, match.groups())
        try:
            if any(type(value) is not int or not 1 <= value <= 2147483647 for value in (receipt_id, version, position)):
                raise ValueError("Native source reference must fit database Integer")
            snapshot = await service.native_warehouse_receipt_source(
                session, organization_id, receipt_id, version,
            )
            if position > len(snapshot["lines"]):
                raise ValueError("Primary receipt position does not exist")
            if snapshot["warehouse_id"] != data.warehouse_id:
                raise ValueError("Primary receipt belongs to another warehouse")
            # Detached JSON facts remain local; request/DTO/hash are never rewritten.
            frozen = json.loads(canonical(snapshot))
            history = await self._native_receipt_history(session, organization_id, frozen)
            return {"source_snapshot": frozen, "position": position, "owners": history["owners"]}
        except ValueError as exc:
            reject(str(exc), 409)

    async def _prelock_invoice_source(self, session, organization_id, data):
        if data.kind != "ship" or not data.source.startswith("invoice:"):
            return set()
        if (not re.fullmatch(r"invoice:[1-9][0-9]*", data.source)
                or self.services is None or getattr(self.services, "sales_source", None) is None):
            reject("Отгрузка счёта требует проверенный источник продаж", 409)
        from modules.wms.invoice_reservations import (
            InvoiceReservation,
            _locked_reservation,
            _remaining,
        )

        document_id = int(data.source.split(":", 1)[1])
        reservation = await session.get(InvoiceReservation, document_id)
        if reservation is None or reservation.organization_id != organization_id:
            reject("Счёт не имеет подтверждённого резерва организации", 409)
        try:
            expected_document_id = reservation.document_id
            expected_organization_id = reservation.organization_id
            expected_legacy_digest = reservation.digest
            expected_snapshot = reservation.snapshot
            expected_version = expected_snapshot["version"]
            expected_content_sha256 = expected_snapshot["content_sha256"]
            expected_snapshot_digest = digest(expected_snapshot)
        except (KeyError, TypeError, ValueError):
            reject("Сохранённое распределение счёта повреждено", 409)
        if expected_document_id != document_id or expected_organization_id != organization_id:
            reject("Счёт не имеет подтверждённого резерва организации", 409)
        try:
            source = await self.services.sales_source.invoice_shipping_source(session, document_id,
                organization_id=organization_id, expected_version=expected_version,
                expected_content_sha256=expected_content_sha256, operation="fulfill")
        except ValueError as exc:
            reject(str(exc), 409)
        locked, allocations = await _locked_reservation(session, organization_id, source)
        try:
            locked_document_id = locked.document_id
            locked_organization_id = locked.organization_id
            locked_legacy_digest = locked.digest
            locked_snapshot = locked.snapshot
            locked_version = locked_snapshot["version"]
            locked_content_sha256 = locked_snapshot["content_sha256"]
            locked_snapshot_digest = digest(locked_snapshot)
        except (KeyError, TypeError, ValueError):
            reject("Сохранённое распределение счёта изменилось", 409)
        if (locked_document_id != expected_document_id
                or locked_organization_id != expected_organization_id
                or locked_legacy_digest != expected_legacy_digest
                or locked_version != expected_version
                or locked_content_sha256 != expected_content_sha256
                or locked_snapshot_digest != expected_snapshot_digest):
            reject("Сохранённое распределение счёта изменилось", 409)
        await _remaining(session, locked, allocations)

        codes = sorted({row["sku_code"] for row in allocations})
        names = {row["warehouse"] for row in allocations}
        names.update((await session.scalars(select(StockMovement.warehouse).where(
            StockMovement.organization_id == organization_id,
            StockMovement.sku_code.in_(codes), StockMovement.reason.not_in(["reserve", "release"])
        ).distinct())).all())
        names.update((await session.scalars(select(ReservationVersion.warehouse).where(
            ReservationVersion.organization_id == organization_id,
            ReservationVersion.sku_code.in_(codes)
        ).distinct())).all())
        from modules.wms.physical_models import WarehouseControl

        names.update((await session.scalars(select(WarehouseDirectory.name).join(
            WarehouseControl, WarehouseControl.warehouse_id == WarehouseDirectory.id,
        ).where(WarehouseControl.organization_id == organization_id,
            WarehouseControl.mode == "erp"))).all())
        warehouse_ids = set((await session.scalars(select(WarehouseControl.warehouse_id).join(
            WarehouseDirectory, WarehouseDirectory.id == WarehouseControl.warehouse_id,
        ).where(WarehouseControl.organization_id == organization_id,
            WarehouseDirectory.name.in_(names)))).all()) if names else set()
        return warehouse_ids

    async def _prelock_assembly_source(self, session, organization_id, data):
        if data.kind not in {"assembly_bind", "assembly_kit_move", "assembly_consume", "assembly_unused_return", "assembly_output", "assembly_qc", "assembly_return"}:
            return None
        from modules.production.accounting_ownership import ProductionOrderOwnership
        from modules.production.assembly_specifications import (
            AssemblyApproval,
            AssemblyNomination,
            AssemblySpecification,
        )
        from modules.production.models import ProductionBom, ProductionBomItem, ProductionOrder

        if data.kind == "assembly_bind":
            spec_id = data.specification_id
            execution = None
        else:
            identity = None
            if data.kind in {"assembly_qc", "assembly_return"}:
                identity = await session.scalar(select(AssemblyOutputIdentity).where(
                    AssemblyOutputIdentity.id == str(data.identity_id),
                    AssemblyOutputIdentity.organization_id == organization_id,
                ))
                if identity is None:
                    reject("Идентичность изделия не найдена", 404)
            execution = await session.scalar(select(AssemblyExecution).where(
                AssemblyExecution.id == (identity.execution_id if identity is not None else str(data.execution_id)),
                AssemblyExecution.organization_id == organization_id,
            ))
            if execution is None or (identity is None and execution.warehouse_id != data.warehouse_id):
                reject("Исполнение сборки не найдено в выбранной организации и складе", 404)
            spec_id = execution.specification_id
        spec_identity = await session.scalar(select(AssemblySpecification).where(
            AssemblySpecification.id == spec_id,
            AssemblySpecification.organization_id == organization_id,
        ))
        if spec_identity is None:
            reject("Точная спецификация сборки не найдена в организации", 404)
        order = await session.scalar(select(ProductionOrder).join(
            ProductionOrderOwnership, ProductionOrderOwnership.order_id == ProductionOrder.id,
        ).where(ProductionOrder.id == spec_identity.order_id,
            ProductionOrderOwnership.organization_id == organization_id)
            .with_for_update().execution_options(populate_existing=True))
        if order is None:
            reject("Наряд сборки не найден в организации", 404)
        spec = await session.scalar(select(AssemblySpecification).where(
            AssemblySpecification.id == spec_id,
            AssemblySpecification.organization_id == organization_id,
            AssemblySpecification.order_id == order.id,
        ).with_for_update().execution_options(populate_existing=True))
        if spec is None:
            reject("Точная спецификация сборки не найдена", 404)
        bom_id = spec.snapshot.get("legacy_bom", {}).get("id")
        bom = await session.scalar(select(ProductionBom).where(ProductionBom.id == bom_id)
            .with_for_update().execution_options(populate_existing=True)) if bom_id else None
        if bom is not None:
            await session.scalars(select(ProductionBomItem).where(ProductionBomItem.bom_id == bom.id)
                .order_by(ProductionBomItem.id).with_for_update().execution_options(populate_existing=True))
        if data.kind != "assembly_unused_return":
            # These decisions are part of the source authority. Acquire them in
            # the order -> specification/BOM -> nomination -> approval chain,
            # before any warehouse or physical-unit row is locked. The handler
            # may reread the same rows while these locks are already held.
            await session.scalar(select(AssemblyNomination).where(
                AssemblyNomination.specification_id == spec.id,
            ).order_by(AssemblyNomination.revision.desc()).limit(1)
                .with_for_update().execution_options(populate_existing=True))
            await session.scalar(select(AssemblyApproval).where(
                AssemblyApproval.specification_id == spec.id,
            ).order_by(AssemblyApproval.revision.desc()).limit(1)
                .with_for_update().execution_options(populate_existing=True))
        if execution is not None:
            execution = await session.scalar(select(AssemblyExecution).where(
                AssemblyExecution.id == execution.id,
                AssemblyExecution.organization_id == organization_id,
            ).with_for_update().execution_options(populate_existing=True))
        return {"specification": spec, "execution": execution, "order": order}

    async def _assembly_authority(self, session, organization_id, specification):
        from modules.production.assembly_specifications import (
            AssemblyApproval,
            AssemblyNomination,
            AssemblySpecification,
        )

        source_service = getattr(self.services, "production_output", None) if self.services is not None else None
        if source_service is None:
            reject("Сервис точной спецификации сборки недоступен", 503)
        source = await source_service.validate_assembly_source(session, organization_id, specification)
        latest_spec_revision = await session.scalar(select(func.max(AssemblySpecification.revision)).where(
            AssemblySpecification.organization_id == organization_id,
            AssemblySpecification.order_id == specification.order_id))
        if latest_spec_revision != specification.revision:
            reject("Создана новая редакция точной спецификации; старое исполнение нельзя продолжать")
        nomination = await session.scalar(select(AssemblyNomination).where(
            AssemblyNomination.specification_id == specification.id)
            .order_by(AssemblyNomination.revision.desc()).limit(1)
            .with_for_update().execution_options(populate_existing=True))
        approval = await session.scalar(select(AssemblyApproval).where(
            AssemblyApproval.specification_id == specification.id)
            .order_by(AssemblyApproval.revision.desc()).limit(1)
            .with_for_update().execution_options(populate_existing=True))
        if (nomination is None or approval is None
                or approval.nomination_id != nomination.id
                or approval.appointment_revision != nomination.revision
                or approval.approver_sub != nomination.responsible_approver_sub
                or approval.specification_digest != specification.digest
                or approval.order_digest != source["order_digest"]
                or approval.bom_digest != source["bom_digest"]):
            reject("Текущая редакция спецификации и назначения не имеет действующего утверждения")
        return source, approval

    async def _assembly_quota_state(self, session, organization_id, order_id):
        executions = (await session.scalars(select(AssemblyExecution).where(
            AssemblyExecution.organization_id == organization_id,
            AssemblyExecution.order_id == order_id).order_by(AssemblyExecution.id)
            .execution_options(populate_existing=True))).all()
        if not executions:
            return set(), set(), {}
        by_id = {row.id: row for row in executions}
        ids = sorted(by_id)
        traces = (await session.scalars(select(AssemblyConsumption).where(
            AssemblyConsumption.execution_id.in_(ids)).order_by(AssemblyConsumption.id)
            .execution_options(populate_existing=True))).all()
        links = (await session.scalars(select(AssemblyInputLink).where(
            AssemblyInputLink.execution_id.in_(ids)).order_by(AssemblyInputLink.id)
            .execution_options(populate_existing=True))).all()
        holds = (await session.scalars(select(PhysicalHold).where(
            PhysicalHold.order_ref.in_([f"assembly:{key}" for key in ids])
        ).order_by(PhysicalHold.id).execution_options(populate_existing=True))).all()
        holds_by_id = {row.id: row for row in holds}
        links_by_execution = defaultdict(list)
        links_by_hold = defaultdict(list)
        links_by_id = {}
        for link in links:
            execution = by_id.get(link.execution_id)
            hold = holds_by_id.get(link.hold_id)
            if (execution is None or hold is None or link.qty <= 0 or link.consumed_qty < 0
                    or link.returned_qty < 0 or link.consumed_qty + link.returned_qty > link.qty
                    or hold.order_ref != f"assembly:{link.execution_id}"
                    or hold.warehouse_id != execution.warehouse_id):
                reject("История комплекта сборки повреждена; перепривязка изделия запрещена")
            links_by_execution[link.execution_id].append(link)
            links_by_hold[link.hold_id].append(link)
            links_by_id[link.id] = link

        trace_keys = defaultdict(set)
        trace_qty_by_link = defaultdict(lambda: Decimal(0))
        trace_owners = {}
        for trace in traces:
            execution = by_id.get(trace.execution_id)
            link = links_by_id.get(trace.input_link_id)
            if (execution is None or link is None or link.execution_id != trace.execution_id
                    or link.specification_line_id != trace.specification_line_id
                    or trace.output_key not in execution.output_keys or trace.qty <= 0):
                reject("Неизменяемая трасса расхода сборки повреждена")
            owner = trace_owners.setdefault(trace.output_key, trace.execution_id)
            if owner != trace.execution_id:
                reject("Ключ изделия уже встречается в трассе другого исполнения")
            trace_keys[trace.execution_id].add(trace.output_key)
            trace_qty_by_link[trace.input_link_id] += trace.qty

        holds_by_execution = defaultdict(list)
        for hold in holds:
            key = hold.order_ref.removeprefix("assembly:")
            execution = by_id.get(key)
            if (execution is None or hold.warehouse_id != execution.warehouse_id
                    or hold.container != execution.container or hold.qty < 0
                    or min(hold.shipped, hold.consumed, hold.released, hold.picked) < 0):
                reject("Резерв комплекта сборки не совпадает с неизменяемой привязкой")
            rows = links_by_hold.get(hold.id, [])
            if (sum((row.consumed_qty for row in rows), Decimal(0)) != hold.consumed
                    or sum((row.qty - row.returned_qty for row in rows), Decimal(0)) != hold.picked):
                reject("Количество комплекта не совпадает с его неизменяемой трассой")
            holds_by_execution[key].append(hold)
        for link in links:
            if trace_qty_by_link[link.id] != link.consumed_qty:
                reject("Списание комплекта не совпадает с неизменяемой трассой")

        active_keys, consumed_keys = set(), set()
        active_owner = {}
        settled = {}
        for execution in executions:
            keys = execution.output_keys or []
            used = execution.consumed_output_keys or []
            if (len(keys) != len(set(keys)) or execution.output_qty != Decimal(len(keys))
                    or len(used) != len(set(used)) or set(used) != trace_keys[execution.id]
                    or not set(used).issubset(keys)):
                reject("План и фактическая трасса сборки не совпадают")
            if execution.status == "active":
                for key in keys:
                    owner = active_owner.setdefault(key, execution.id)
                    if owner != execution.id:
                        reject("Ключ изделия одновременно принадлежит двум активным исполнениям")
                    active_keys.add(key)
            elif execution.status != "closed":
                reject("Неизвестное состояние исполнения сборки")
            consumed_keys.update(trace_keys[execution.id])
            execution_holds = holds_by_execution[execution.id]
            execution_links = links_by_execution[execution.id]
            is_settled = bool(execution_holds) and all(
                row.qty == row.shipped + row.consumed + row.released and row.shipped == 0
                and row.qty - row.shipped - row.consumed - row.released == 0
                for row in execution_holds
            ) and all(row.qty == row.consumed_qty + row.returned_qty for row in execution_links)
            settled[execution.id] = is_settled
            if execution.status == "closed" and not is_settled:
                reject("Закрытое исполнение не имеет полного доказательства возврата и расхода")
        if any(key in active_owner and active_owner[key] != owner
                for key, owner in trace_owners.items()):
            reject("Ключ изделия повторно закреплён после подтверждённого расхода")
        return active_keys, consumed_keys, settled

    @staticmethod
    def _assembly_only_native(warehouse):
        if warehouse.mode != "erp" or warehouse.onec_ref is not None:
            reject("Сборка на этой операции доступна только после переключения native ERP-склада")

    async def _assembly_bind(self, session, warehouse, data, actor, controls, *, assembly_context):
        from modules.production.order_completion import OrderCompletion
        from modules.production.output_documents import OutputConfirmation
        from modules.wms.production_material_issues import ProductionMaterialIssue

        self._assembly_only_native(warehouse)
        from modules.wms.assembly_quality import physical_assembly_executor, physical_assembly_proof
        from modules.wms.physical_schemas import AssemblyBindProof

        anchor, _inputs, _prepared = await self._assembly_configured(session, warehouse, assembly_context, binding=False)
        specification = assembly_context["specification"]
        source, approval = await self._assembly_authority(session, warehouse.organization_id, specification)
        order = source["order"]
        if await session.scalar(select(OutputConfirmation.document_id).where(
            OutputConfirmation.organization_id == warehouse.organization_id,
            OutputConfirmation.order_id == order.id).limit(1)) is not None:
            reject("Наряд уже имеет подтверждённый legacy-выпуск; физическую сборку нельзя дублировать")
        if await session.get(OrderCompletion, order.id) is not None:
            reject("Наряд уже завершён legacy-процессом")
        if await session.scalar(select(ProductionMaterialIssue.id).where(
            ProductionMaterialIssue.organization_id == warehouse.organization_id,
            ProductionMaterialIssue.order_id == order.id).limit(1)) is not None:
            reject("Для наряда уже проведена legacy-выдача материалов")
        output_keys = set(data.output_keys)
        if len(output_keys) != len(data.output_keys):
            reject("Ключ готового изделия не может повторяться в одном исполнении", 422)
        active_keys, consumed_keys, _settled = await self._assembly_quota_state(
            session, warehouse.organization_id, order.id)
        occupied = active_keys | consumed_keys
        if output_keys.intersection(occupied):
            reject("Ключ изделия уже занят активным исполнением или подтверждённым расходом")
        output_qty = Decimal(len(data.output_keys))
        if len(occupied) + output_qty > Decimal(order.qty):
            reject("План исполнения превышает остаток количества производственного наряда")
        snapshot = specification.snapshot
        output = snapshot["output"]
        output_policy = await policy(session, warehouse.warehouse_id, output["sku_code"])
        if output_policy.base_unit != output["base_unit"] or output_policy.precision != output["precision"]:
            reject("Политика склада для готового товара отличается от утверждённой спецификации")
        line_requirements = []
        for line in snapshot["exact_lines"]:
            rule = await policy(session, warehouse.warehouse_id, line["sku_code"])
            if rule.base_unit != line["base_unit"] or rule.precision != line["precision"]:
                reject("Политика склада компонента отличается от точной нормы спецификации")
            total = Decimal(line["quantity_per_output"]) * output_qty
            if total != total.quantize(Decimal(1).scaleb(-line["precision"])):
                reject("Плановое количество компонента нельзя представить точно в его базовой единице", 422)
            line_requirements.append((line, total))
        execution_id = str(uuid4())
        execution_snapshot = {"specification_id": specification.id,
            "specification_revision": specification.revision,
            "specification_digest": specification.digest,
            "approval_id": approval.id,
            "approval_revision": approval.revision,
            "order_digest": source["order_digest"], "bom_digest": source["bom_digest"],
            "output": output}
        execution = AssemblyExecution(id=execution_id, organization_id=warehouse.organization_id,
            warehouse_id=warehouse.warehouse_id, order_id=order.id,
            specification_id=specification.id, specification_digest=specification.digest,
            source_snapshot=execution_snapshot, container=data.container,
            output_keys=list(data.output_keys), consumed_output_keys=[], output_qty=output_qty,
            bind_operation_id=str(data.operation_id), status="active", actor=actor)
        session.add(execution)
        await session.flush()
        holds = []
        for line, total in line_requirements:
            line_id = str(line["legacy_bom_item_id"])
            rows = await allocate(session, self.reference, warehouse, line["sku_code"], total,
                f"assembly:{execution_id}:{line_id}", data.container, shelf_days=0,
                order_ref=f"assembly:{execution_id}", task_kind="assemblykit")
            holds.extend({"specification_line_id": line_id, "hold_id": row.id,
                "unit_id": row.unit_id, "qty": decimal_text(row.qty), "source": row.source}
                for row in rows)
        await self._fresh_assembly_authority(session, warehouse.organization_id, actor)
        bind_proof = physical_assembly_proof(AssemblyBindProof, {"protocol": 1, "execution_id": execution_id,
            "anchor": anchor, "executor": physical_assembly_executor(self._caller_seed, self.user)}).model_dump(mode="json")
        return {"execution_id": execution_id, "specification_id": specification.id,
            "specification_digest": specification.digest, "output_keys": list(data.output_keys),
            "output_qty": decimal_text(output_qty), "holds": holds, "assembly_bind_proof_v1": bind_proof}, None

    async def _assembly_item_scan(self, session, warehouse, unit, qty, item_scan, lot):
        await package_check(session, warehouse, unit.sku_code, qty, unit.packaging_id, unit.package_state)
        sku = await session.scalar(select(Sku).where(Sku.code == unit.sku_code))
        if sku is None or not sku.is_active:
            reject("Товар компонента больше не активен в каталоге")
        from modules.wms.item_identity import resolve_item_scan

        resolved = await resolve_item_scan(
            session, warehouse.organization_id, item_scan, warehouse_id=warehouse.warehouse_id,
        )
        if resolved["sku_id"] != sku.id or resolved["packaging_id"] != unit.packaging_id:
            reject("Скан товара или упаковки не совпадает с подтверждённым компонентом")
        gs1 = resolved["gs1"]
        if gs1:
            if gs1["lot_no"] and gs1["lot_no"] != lot["lot_no"]:
                reject("GS1 обозначает другую партию")
            for gs1_date, field in ((gs1["manufacture"], "mfg_date"), (gs1["expiry"], "expiry_date")):
                if gs1_date and (gs1_date["value"] != lot.get(field)
                        or (field == "mfg_date" and gs1_date["precision"] != lot.get("manufacture_precision"))):
                    reject("Дата GS1 не совпадает с подтверждёнными данными партии")

    async def _assembly_execution_authority(self, session, warehouse, assembly_context, *, allow_closed=False):
        self._assembly_only_native(warehouse)
        execution = assembly_context["execution"]
        specification = assembly_context["specification"]
        if (execution is None or execution.status not in ({"active", "closed"} if allow_closed else {"active"})
                or execution.specification_id != specification.id
                or execution.organization_id != warehouse.organization_id
                or execution.warehouse_id != warehouse.warehouse_id
                or execution.specification_digest != specification.digest):
            reject("Исполнение сборки не соответствует выбранному складу и неизменной спецификации")
        source, approval = await self._assembly_authority(session, warehouse.organization_id, specification)
        if (execution.source_snapshot.get("order_digest") != source["order_digest"]
                or execution.source_snapshot.get("bom_digest") != source["bom_digest"]
                or execution.source_snapshot.get("approval_id") != approval.id):
            reject("Источник или утверждение исполнения изменились; требуется новая редакция сборки")
        return execution, specification, source

    async def _assembly_kit_move(self, session, warehouse, data, actor, controls, *, assembly_context):
        execution, specification, source = await self._assembly_execution_authority(
            session, warehouse, assembly_context)
        await self._assembly_configured(session, warehouse, assembly_context)
        task = await session.scalar(select(PhysicalTask).where(
            PhysicalTask.id == str(data.task_id), PhysicalTask.warehouse_id == warehouse.warehouse_id,
        ).with_for_update().execution_options(populate_existing=True))
        hold = await session.scalar(select(PhysicalHold).where(
            PhysicalHold.id == data.hold_id, PhysicalHold.warehouse_id == warehouse.warehouse_id,
        ).with_for_update().execution_options(populate_existing=True))
        if (task is None or task.kind != "assemblykit" or task.hold_id != data.hold_id
                or task.status != "claimed" or task.assignee != actor or hold is None
                or hold.order_ref != f"assembly:{execution.id}" or task.source != hold.source):
            reject("Возьмите задание комплекта сборки и проверьте его привязку")
        line_id = hold.source.rsplit(":", 1)[-1]
        line = next((row for row in specification.snapshot["exact_lines"]
            if str(row["legacy_bom_item_id"]) == line_id), None)
        if line is None:
            reject("Резерв не принадлежит строке утверждённой спецификации")
        unit = await self._unit(session, warehouse, hold.unit_id)
        place = await location(session, warehouse.warehouse_id, unit.location_id)
        destination = await location(session, warehouse.warehouse_id, data.destination_id)
        if place.kind not in {"pick", "storage"} or destination.kind != "service":
            reject("Комплект перемещается из хранения в подтверждённую зону сборки")
        if (data.location_scan != f"LOC-{place.location_id}"
                or data.batch_scan != f"LOT-{unit.batch_id}-{unit.id.upper()}"
                or data.container_scan != f"BOX-{hold.container}"):
            reject("Не совпадает скан ячейки, партии или контейнера заказа сборки")
        reason, lot = await eligible(session, self.reference, warehouse, unit, hold.min_shelf_days)
        if reason:
            reject(f"Компонент комплекта заблокирован: {reason}")
        await self._assembly_item_scan(session, warehouse, unit, data.qty, data.item_scan, lot)
        await policy(session, warehouse.warehouse_id, unit.sku_code, data.qty)
        if (data.qty > task.qty or data.qty > hold.qty - hold.picked - hold.released
                or data.qty > unit.reserved or data.qty > unit.qty):
            reject("Количество комплекта превышает задание или доступный резерв")
        await capacity(session, destination, unit.sku_code, data.qty)
        original_dimensions = dimensions(unit)
        unit.qty -= data.qty
        unit.reserved -= data.qty
        kit = self._clone(session, unit, data.qty, reserved=data.qty,
            location_id=destination.location_id, container=execution.container)
        hold.picked += data.qty
        task.qty = hold.qty - hold.picked - hold.released
        if task.qty == 0:
            task.status = "done"
        await session.flush()
        link = AssemblyInputLink(execution_id=execution.id, specification_line_id=line_id,
            hold_id=hold.id, original_unit_id=unit.id, actual_kit_unit_id=kit.id,
            kit_move_operation_id=str(data.operation_id), qty=data.qty,
            consumed_qty=Decimal(0), returned_qty=Decimal(0),
            source_dimensions=original_dimensions, kit_dimensions=dimensions(kit))
        session.add(link)
        self._deltas.extend([(unit, -data.qty), (kit, data.qty)])
        await session.flush()
        return {"execution_id": execution.id, "input_link_id": link.id,
            "specification_line_id": line_id, "hold_id": hold.id,
            "qty": decimal_text(data.qty), "unit": unit_output(kit),
            "task_status": task.status}, None

    async def _assembly_consume(self, session, warehouse, data, actor, controls, *, assembly_context):
        from modules.wms.assembly_quality import (
            physical_assembly_executor,
            physical_assembly_first_gate,
            physical_assembly_proof,
        )
        from modules.wms.physical_schemas import AssemblyConsumeProof

        execution, specification, _source = await self._assembly_execution_authority(
            session, warehouse, assembly_context)
        anchor, _inputs, _prepared = await self._assembly_configured(session, warehouse, assembly_context)
        await physical_assembly_first_gate(session, anchor, data.output_key)
        await self._assembly_quota_state(session, warehouse.organization_id, execution.order_id)
        if data.output_key not in execution.output_keys:
            reject("Ключ изделия не входит в план исполнения сборки")
        if data.output_key in (execution.consumed_output_keys or []):
            reject("Для этого ключа изделия компоненты уже списаны")
        consumed = []
        scan_by_link = {row.input_link_id: row for row in data.scans}
        used_scan_ids = set()
        for line in specification.snapshot["exact_lines"]:
            required = Decimal(line["quantity_per_output"])
            links = (await session.scalars(select(AssemblyInputLink).where(
                AssemblyInputLink.execution_id == execution.id,
                AssemblyInputLink.specification_line_id == str(line["legacy_bom_item_id"]),
            ).order_by(AssemblyInputLink.id).with_for_update()
                .execution_options(populate_existing=True))).all()
            available = sum((row.qty - row.consumed_qty - row.returned_qty for row in links), Decimal(0))
            if available < required:
                reject("Комплект неполон: нельзя списать изделие до комплектации всех норм")
            remaining = required
            for link in links:
                take = min(remaining, link.qty - link.consumed_qty - link.returned_qty)
                if take <= 0:
                    continue
                unit = await self._unit(session, warehouse, link.actual_kit_unit_id)
                if (unit.location_id != link.kit_dimensions["location_id"]
                        or unit.sku_code != line["sku_code"] or unit.reserved < take or unit.qty < take):
                    reject("Фактический компонент комплекта изменился или недоступен")
                scan = scan_by_link.get(link.id)
                if scan is None:
                    reject("Перед списанием отсканируйте каждый используемый компонент комплекта")
                place = await location(session, warehouse.warehouse_id, unit.location_id)
                if (place.kind != "service"
                        or scan.location_scan != f"LOC-{place.location_id}"
                        or scan.batch_scan != f"LOT-{unit.batch_id}-{unit.id.upper()}"
                        or scan.container_scan != f"BOX-{execution.container}"):
                    reject("Не совпадает скан ячейки, партии или контейнера компонента")
                reason, lot = await eligible(session, self.reference, warehouse, unit)
                if reason:
                    reject(f"Списание компонента заблокировано: {reason}")
                await policy(session, warehouse.warehouse_id, unit.sku_code, take)
                await self._assembly_item_scan(session, warehouse, unit, take, scan.item_scan, lot)
                used_scan_ids.add(link.id)
                hold = await session.scalar(select(PhysicalHold).where(PhysicalHold.id == link.hold_id)
                    .with_for_update().execution_options(populate_existing=True))
                if hold is None or hold.order_ref != f"assembly:{execution.id}":
                    reject("Компонент не связан с резервом исполнения")
                before = dimensions(unit)
                unit.qty -= take
                unit.reserved -= take
                hold.consumed += take
                link.consumed_qty += take
                session.add(AssemblyConsumption(execution_id=execution.id, input_link_id=link.id,
                    specification_line_id=link.specification_line_id, output_key=data.output_key,
                    operation_id=str(data.operation_id), qty=take,
                    dimensions={**before, "lot_no": lot["lot_no"], "qty": decimal_text(take)}))
                self._deltas.append((unit, -take, before))
                consumed.append({"specification_line_id": link.specification_line_id,
                    "input_link_id": link.id, "output_key": data.output_key,
                    "sku_code": unit.sku_code, "lot_no": lot["lot_no"], "qty": decimal_text(take)})
                remaining -= take
                if remaining == 0:
                    break
            if remaining:
                reject("Комплект неполон для точной нормы изделия")
        if used_scan_ids != set(scan_by_link):
            reject("Переданы лишние сканы, не относящиеся к списываемому комплекту", 422)
        execution.consumed_output_keys = sorted([*(execution.consumed_output_keys or []), data.output_key])
        await self._fresh_assembly_authority(session, warehouse.organization_id, actor)
        consume_proof = physical_assembly_proof(AssemblyConsumeProof, {"protocol": 1,
            "execution_id": execution.id, "bind_operation_id": execution.bind_operation_id,
            "anchor_digest": digest(anchor), "output_key": data.output_key,
            "executor": physical_assembly_executor(self._caller_seed, self.user)}).model_dump(mode="json")
        return {"execution_id": execution.id, "output_key": data.output_key,
            "consumed": consumed, "consumed_output_keys": execution.consumed_output_keys,
            "assembly_consume_proof_v1": consume_proof}, None

    async def _assembly_unused_return(self, session, warehouse, data, actor, controls, *, assembly_context):
        self._assembly_only_native(warehouse)
        execution = assembly_context["execution"]
        specification = assembly_context["specification"]
        if (execution is None or execution.status != "active"
                or execution.specification_id != specification.id
                or execution.organization_id != warehouse.organization_id
                or execution.warehouse_id != warehouse.warehouse_id
                or execution.specification_digest != specification.digest):
            reject("Возврат не связан с неизменным исполнением и выбранным складом")
        returned = []
        for item in data.returns:
            link = await session.scalar(select(AssemblyInputLink).where(
                AssemblyInputLink.id == item.input_link_id,
                AssemblyInputLink.execution_id == execution.id,
            ).with_for_update().execution_options(populate_existing=True))
            if link is None:
                reject("Строка возврата не принадлежит этому исполнению", 404)
            unit = await self._unit(session, warehouse, link.actual_kit_unit_id)
            source = await self._unit(session, warehouse, link.original_unit_id)
            place = await location(session, warehouse.warehouse_id, unit.location_id)
            target = await location(session, warehouse.warehouse_id, link.source_dimensions["location_id"])
            if (place.kind != "service" or target.kind not in {"storage", "pick"}
                    or source.location_id != target.location_id or unit.quality != "released"):
                reject("Неиспользованный компонент возвращается в исходное доступное место хранения")
            if (item.location_scan != f"LOC-{place.location_id}"
                    or item.batch_scan != f"LOT-{unit.batch_id}-{unit.id.upper()}"
                    or item.container_scan != f"BOX-{execution.container}"):
                reject("Не совпадает скан ячейки, партии или контейнера при возврате")
            reason, lot = await eligible(session, self.reference, warehouse, unit)
            if reason:
                reject(f"Возврат компонента заблокирован: {reason}")
            await self._assembly_item_scan(session, warehouse, unit, item.qty, item.item_scan, lot)
            await policy(session, warehouse.warehouse_id, unit.sku_code, item.qty)
            available = link.qty - link.consumed_qty - link.returned_qty
            if item.qty > available or item.qty > unit.qty or item.qty > unit.reserved:
                reject("Возвращаемое количество превышает неиспользованный комплект")
            await capacity(session, target, unit.sku_code, item.qty)
            before = dimensions(unit)
            unit.qty -= item.qty
            unit.reserved -= item.qty
            restored = self._clone(session, unit, item.qty, location_id=target.location_id,
                reserved=Decimal(0), container=source.container)
            hold = await session.scalar(select(PhysicalHold).where(PhysicalHold.id == link.hold_id)
                .with_for_update().execution_options(populate_existing=True))
            if hold is None or hold.order_ref != f"assembly:{execution.id}" or item.qty > hold.picked - hold.consumed:
                reject("Возвращаемое количество не принадлежит неиспользованной части резерва")
            hold.picked -= item.qty
            hold.released += item.qty
            link.returned_qty += item.qty
            self._deltas.extend([(unit, -item.qty, before), (restored, item.qty)])
            returned.append({"input_link_id": link.id, "hold_id": hold.id,
                "qty": decimal_text(item.qty), "returned_unit_id": restored.id,
                "location_id": target.location_id, "lot_no": lot["lot_no"]})
        released = []
        for item in data.hold_releases:
            hold = await session.scalar(select(PhysicalHold).where(
                PhysicalHold.id == item.hold_id, PhysicalHold.warehouse_id == warehouse.warehouse_id,
            ).with_for_update().execution_options(populate_existing=True))
            if hold is None or hold.order_ref != f"assembly:{execution.id}":
                reject("Освобождаемый резерв не принадлежит этому исполнению", 404)
            unit = await self._unit(session, warehouse, hold.unit_id)
            unpicked = hold.qty - hold.picked - hold.released
            if item.qty > unpicked or item.qty > unit.reserved:
                reject("Количество освобождения превышает неперемещённую часть резерва")
            unit.reserved -= item.qty
            hold.released += item.qty
            tasks = (await session.scalars(select(PhysicalTask).where(
                PhysicalTask.hold_id == hold.id, PhysicalTask.kind == "assemblykit",
                PhysicalTask.status.in_(["open", "claimed"]),
            ).with_for_update().execution_options(populate_existing=True))).all()
            for task in tasks:
                task.qty = max(Decimal(0), hold.qty - hold.picked - hold.released)
                if task.qty == 0:
                    task.status, task.assignee = "canceled", ""
            released.append({"hold_id": hold.id, "qty": decimal_text(item.qty),
                "unpicked_remaining": decimal_text(hold.qty - hold.picked - hold.released)})
        _active_keys, _consumed_keys, settled = await self._assembly_quota_state(
            session, warehouse.organization_id, execution.order_id)
        if settled.get(execution.id):
            execution.status = "closed"
        return {"execution_id": execution.id, "returned": returned,
            "released_unpicked": released, "status": execution.status}, None

    async def _unit(self, session, warehouse, unit_id):
        unit = await session.scalar(select(StockUnit).where(StockUnit.id == str(unit_id))
            .with_for_update().execution_options(populate_existing=True))
        if unit is None or unit.warehouse_id != warehouse.warehouse_id:
            reject("Остаток не найден в выбранном складе", 404)
        _require_on_hand(unit)
        await location(session, warehouse.warehouse_id, unit.location_id)
        return unit

    def _clone(self, session, unit, qty, *, stock_state="on_hand", dispatch_operation_id=None, **changes):
        if unit.assembly_output_id is not None and qty != 1:
            reject("Идентичность изделия нельзя разделить", 422)
        values = dimensions(unit)
        for name in ("last_checked", "next_service"):
            values[name] = getattr(unit, name)
        reserved = changes.pop("reserved", Decimal(0))
        values.update(changes)
        values["external_operations"] = list(unit.external_operations or [])
        values["arrived_at"] = unit.arrived_at
        values["assembly_output_id"] = unit.assembly_output_id
        new = StockUnit(id=str(uuid4()), stock_state=stock_state, dispatch_operation_id=dispatch_operation_id,
            qty=qty, reserved=reserved, **values)
        session.add(new)
        return new

    async def _receive(self, session, warehouse, data, actor, controls, *, receipt_context=None):
        place = await location(session, warehouse.warehouse_id, data.location_id)
        if place.kind != "receiving":
            reject("Поступление регистрируется в зоне приёмки")
        if warehouse.onec_ref is not None:
            if receipt_context is not None:
                reject("Режим склада изменился; повторите исходную команду", 409)
            return await self._arrival(session, warehouse, data)
        if receipt_context is None:
            reject("Native-поступление требует подтверждённый первичный источник", 409)
        snapshot, position = receipt_context["source_snapshot"], receipt_context["position"]
        if snapshot["organization_id"] != warehouse.organization_id or snapshot["warehouse_id"] != warehouse.warehouse_id:
            reject("Первичный источник не соответствует складу организации", 409)
        if type(data.batch_id) is not int or not 1 <= data.batch_id <= 2147483647:
            reject("Native-поступление требует допустимый идентификатор партии", 409)
        fact = snapshot["lines"][position - 1]
        rule = await policy(session, warehouse.warehouse_id, data.sku_code, data.qty)
        sku = await session.scalar(select(Sku).where(Sku.id == fact["sku_id"])
            .execution_options(populate_existing=True))
        lot = await batch(session, self.reference, warehouse.organization_id, data.batch_id, data.sku_code)
        if (sku is None or not sku.is_active or sku.code != data.sku_code or fact["sku"] != data.sku_code
            or sku.unit != fact["unit"] or rule.base_unit != fact["unit"] or lot["lot_no"] != fact["lot"]
            or lot.get("organization_id") != warehouse.organization_id or data.source_ref != fact["source_line"]):
            reject("Поступление требует точное соответствие товара, базовой единицы и партии источнику", 409)
        history = await self._native_receipt_history(session, warehouse.organization_id, snapshot,
            owners=receipt_context["owners"])
        if data.qty + history["allocated"].get(position, Decimal(0)) > Decimal(fact["quantity"]):
            reject("Поступление превышает оставшуюся квоту первичного источника", 409)
        # Allocation evidence is entirely server-derived and journalled with the unit.
        binding = {"protocol": 1, "organization_id": warehouse.organization_id,
            "warehouse_id": warehouse.warehouse_id, "source_ref": fact["source_line"],
            "receipt_id": snapshot["receipt_id"], "version": snapshot["version"], "position": position,
            "source_snapshot": snapshot, "sku_id": sku.id, "sku_code": sku.code, "unit": sku.unit,
            "batch_id": data.batch_id, "lot_no": lot["lot_no"], "coefficient": "1",
            "allocated_qty": format(data.qty, "f").rstrip("0").rstrip(".") if "." in format(data.qty, "f") else format(data.qty, "f")}
        result, exchange = await self._arrival(session, warehouse, data)
        if Decimal(result["unit"]["qty"]) != Decimal(binding["allocated_qty"]):
            reject("Не подтверждено количество поступления", 409)
        return {**result, "native_receipt_source_v1": binding}, exchange

    async def _return(self, session, warehouse, data, actor, controls):
        place = await location(session, warehouse.warehouse_id, data.location_id)
        if place.kind != "returns":
            reject("Возврат регистрируется в отдельной зоне контроля")
        original = await session.scalar(select(PhysicalOperation).where(
            PhysicalOperation.organization_id == warehouse.organization_id,
            PhysicalOperation.operation_id == data.source_ref, PhysicalOperation.kind == "ship"))
        if original is None:
            reject("Происхождение возврата не подтверждено отгрузкой ERP; сначала сверьте исходный документ")
        for line in original.result.get("shipped", []):
            picked = await session.get(PickedAllocation, line.get("pick_id"))
            shipped_unit = await session.get(StockUnit, picked.unit_id) if picked is not None else None
            if shipped_unit is not None and shipped_unit.assembly_output_id is not None:
                reject("Возврат изделия требует точную identity/ship/pick команду assembly_return")
        if warehouse.onec_ref is None and original.warehouse_id != warehouse.warehouse_id:
            reject("Native-возврат должен ссылаться на отгрузку этого же склада", 409)
        shipped = sum((Decimal(row["qty"]) for row in original.result["shipped"]
            if row["sku_code"] == data.sku_code and row["batch_id"] == data.batch_id
            and row["characteristic"] == data.characteristic and row["purpose"] == data.purpose), Decimal(0))
        previous = (await session.scalars(select(PhysicalOperation).where(
            PhysicalOperation.organization_id == warehouse.organization_id, PhysicalOperation.kind == "return"))).all()
        returned = sum((Decimal(row.result["unit"]["qty"]) for row in previous
            if row.result.get("source_ref") == data.source_ref and row.result["unit"]["sku_code"] == data.sku_code
            and row.result["unit"]["batch_id"] == data.batch_id and row.result["unit"]["characteristic"] == data.characteristic
            and row.result["unit"]["purpose"] == data.purpose), Decimal(0))
        if data.qty > shipped - returned:
            reject("Возврат превышает подтверждённое количество исходной партии")
        return await self._arrival(session, warehouse, data)

    async def _arrival(self, session, warehouse, data):
        await policy(session, warehouse.warehouse_id, data.sku_code, data.qty)
        lot = await batch(session, self.reference, warehouse.organization_id, data.batch_id, data.sku_code)
        await package_check(session, warehouse, data.sku_code, data.qty, data.packaging_id, data.package_state)
        place = await location(session, warehouse.warehouse_id, data.location_id)
        await capacity(session, place, data.sku_code, data.qty)
        unit = StockUnit(id=str(uuid4()), stock_state="on_hand", dispatch_operation_id=None,
            warehouse_id=warehouse.warehouse_id,
            location_id=data.location_id, sku_code=data.sku_code, batch_id=data.batch_id,
            characteristic=data.characteristic, purpose=data.purpose, qty=data.qty, reserved=Decimal(0),
            quality="quarantine", packaging_id=data.packaging_id, package_state=data.package_state,
            container="", inspection_ref="",
            arrival_source=data.source_ref if warehouse.onec_ref is None and data.kind == "receive" else "erp_receive",
            external_operations=[str(data.operation_id)] if warehouse.onec_ref is not None else [])
        session.add(unit)
        await session.flush()
        self._deltas.append((unit, data.qty))
        return {"unit": unit_output(unit), "source_ref": data.source_ref}, {"source_ref": data.source_ref,
            "dependencies": [data.source_ref] if data.kind == "return" else [],
            "lines": [{**dimensions(unit), "qty": decimal_text(data.qty), "lot": lot["lot_no"]}]}

    async def _quality(self, session, warehouse, data, actor, controls):
        unit = await self._unit(session, warehouse, data.unit_id)
        if unit.assembly_output_id is not None:
            reject("Изделие допускается только собственным назначенным assembly_qc")
        await policy(session, warehouse.warehouse_id, unit.sku_code, data.qty)
        if data.qty > unit.qty - unit.reserved:
            reject("Проверяемое количество занято резервом или превышает остаток")
        if data.quality == "released":
            reason, _ = await eligible(session, self.reference, warehouse, unit, quality=False)
            if reason:
                reject(f"Допуск к подбору запрещён: {reason}")
        unit.qty -= data.qty
        checked = self._clone(session, unit, data.qty, quality=data.quality, inspection_ref=data.evidence)
        self._deltas.extend([(unit, -data.qty), (checked, data.qty)])
        await session.flush()
        return {"unit": unit_output(checked)}, None

    async def _maintenance(self, session, warehouse, data, actor, controls):
        unit = await self._unit(session, warehouse, data.unit_id)
        rule = await policy(session, warehouse.warehouse_id, unit.sku_code, data.qty)
        if data.qty > unit.qty - unit.reserved:
            reject("Для обслуживания сначала освободите соответствующий резерв")
        if data.checked_on > today() or data.checked_on < (unit.last_checked or date.min):
            reject("Дата проверки не может быть будущей или предшествовать предыдущей")
        if not rule.maintenance_days:
            reject("Интервал обслуживания не подтверждён инструкцией производителя")
        limit = data.checked_on + timedelta(days=rule.maintenance_days)
        if data.result == "passed" and (not data.next_service or not data.checked_on < data.next_service <= limit):
            reject("Следующая проверка должна соответствовать подтверждённому интервалу")
        if data.result == "passed":
            reason, _ = await eligible(session, self.reference, warehouse, unit, quality=False, service=False)
            if reason:
                reject(f"Подзарядка не устраняет ограничение: {reason}")
        unit.qty -= data.qty
        checked = self._clone(session, unit, data.qty, quality=unit.quality if data.result == "passed" else "blocked",
            last_checked=data.checked_on, next_service=data.next_service if data.result == "passed" else None,
            service_result={"evidence": data.evidence, "measurements": data.measurements, "result": data.result})
        self._deltas.extend([(unit, -data.qty), (checked, data.qty)])
        await session.flush()
        return {"unit": unit_output(checked)}, None

    async def _move(self, session, warehouse, data, actor, controls):
        unit = await self._unit(session, warehouse, data.unit_id)
        target = controls.get(data.destination_warehouse_id or warehouse.warehouse_id)
        if target.warehouse_id != warehouse.warehouse_id and warehouse.onec_ref is None:
            reject("Межскладское перемещение требует отдельной отправки и приёмки")
        if target.mode != "erp":
            reject("Склад назначения ещё не переключён")
        destination = await location(session, target.warehouse_id, data.destination_id)
        if destination.location_id == unit.location_id:
            reject("Исходная ячейка совпадает с назначением", 422)
        if destination.kind in {"packing", "dispatch", "receiving", "returns"}:
            reject("Для этой зоны используйте отдельную операцию складского цикла")
        await policy(session, target.warehouse_id, unit.sku_code, data.qty)
        await package_check(session, target, unit.sku_code, data.qty, unit.packaging_id, unit.package_state)
        if data.qty > unit.qty - unit.reserved - await pending_move_qty(session, unit.id):
            reject("Перемещаемое количество зарезервировано или отсутствует")
        if destination.kind in {"storage", "pick"}:
            reason, _ = await eligible(session, self.reference, target, unit)
            if reason:
                reject(f"Размещение в свободном запасе запрещено: {reason}")
        await capacity(session, destination, unit.sku_code, data.qty)
        unit.qty -= data.qty
        moved = self._clone(session, unit, data.qty, warehouse_id=target.warehouse_id, location_id=destination.location_id)
        self._deltas.extend([(unit, -data.qty), (moved, data.qty)])
        await session.flush()
        exchange = None
        if target.warehouse_id != warehouse.warehouse_id:
            if warehouse.onec_ref is not None:
                moved.external_operations = sorted(set(moved.external_operations) | {str(data.operation_id)})
            exchange = {"destination_warehouse_ref": target.onec_ref, "evidence": data.evidence,
                "dependencies": list(unit.external_operations or []),
                "lines": [{**dimensions(moved), "qty": decimal_text(data.qty)}]}
        return {"unit": unit_output(moved)}, exchange


    async def _fresh_transfer_authority(self, session, organization_id, actor, *, receipt=False):
        if self._caller_seed is not None and self._caller_seed[1] == ("wms_mobile",):
            from modules.wms.mobile_access import (
                OriginalMobileTransferAuthority,
                revalidate_mobile_transfer_authority,
            )

            authority = self.mobile_transfer_authority
            seed = self._caller_seed
            if (not isinstance(authority, OriginalMobileTransferAuthority)
                    or authority.identity.original_user_id != seed[7]
                    or authority.identity.original_sub != seed[2]
                    or authority.identity.bearer_issued_at != seed[6]
                    or authority.identity.roles != seed[1]
                    or authority.identity.organization_id != organization_id):
                reject("Недостаточно прав", 403)
            _, _, fresh, current_actor = await revalidate_mobile_transfer_authority(
                session, self.core, authority, receipt=receipt,
            )
            if current_actor != actor:
                reject("Недостаточно прав", 403)
            self.user = fresh
        else:
            await self._fresh_capacity_authority(session, organization_id, actor, ("wms.operate",))

    async def incoming_transfer_manifest(self, session, organization_id, warehouse_id, actor):
        from modules.wms.item_identity import (
            _origin_frontier_tokens,
            _origin_operation_snapshot,
            _origin_validate_operation,
            _OriginUnproven,
            _prove_unit_boundary,
        )
        from modules.wms.mobile_access import OriginalMobileTransferAuthority

        authority = self.mobile_transfer_authority
        if (self._caller_seed is None or self._caller_seed[1] != ("wms_mobile",)
                or not isinstance(authority, OriginalMobileTransferAuthority)
                or authority.action != "transfer_receive"
                or authority.identity.organization_id != organization_id
                or authority.identity.warehouse_id != warehouse_id):
            reject("Недостаточно прав", 403)
        await self._fresh_transfer_authority(session, organization_id, actor, receipt=True)

        async def selected_dispatches():
            rows = (await session.scalars(select(PhysicalOperation).where(
                PhysicalOperation.organization_id == organization_id,
                PhysicalOperation.kind == "transfer_dispatch",
            ).order_by(PhysicalOperation.id).execution_options(populate_existing=True))).all()
            selected = []
            for row in rows:
                try:
                    _, request, result = _origin_validate_operation(
                        _origin_operation_snapshot(row), organization_id,
                    )
                except _OriginUnproven as exc:
                    raise HTTPException(409, "Доказательство отправки повреждено") from exc
                if request["destination_warehouse_id"] == warehouse_id:
                    selected.append((row, result["transfer"]))
            return selected

        selected = await selected_dispatches()
        source_ids = {sent["source_warehouse_id"] for _, sent in selected}
        controls = {}
        # Acquire the entire union once: pairwise locks can invert the order.
        for identifier in sorted(source_ids | {warehouse_id}):
            warehouse = await control(session, identifier, organization_id)
            if warehouse.mode != "erp" or warehouse.onec_ref is not None:
                reject("Межскладской учёт требует собственных складов ERP", 409)
            controls[identifier] = warehouse
        await self._fresh_transfer_authority(session, organization_id, actor, receipt=True)
        selected = await selected_dispatches()
        if any(sent["source_warehouse_id"] not in controls for _, sent in selected):
            reject("Состав отправок изменился; обновите список поступлений", 409)

        items = []
        for dispatch, sent in selected:
            await protect_blind_receipt(session, dispatch)
            transit = await session.scalar(select(StockUnit).where(
                StockUnit.id == sent["transit_unit_id"],
                StockUnit.warehouse_id == sent["source_warehouse_id"],
            ).execution_options(populate_existing=True))
            try:
                require_current_in_transit(transit)
            except CurrentStockStateError as exc:
                raise HTTPException(409, "Остаток в пути не подтверждён") from exc
            if transit.dispatch_operation_id != dispatch.operation_id:
                reject("Остаток относится к другой отправке", 409)
            frontier, _ = await physical_unit_frontier(session, organization_id, transit.id)
            try:
                proven = await _prove_unit_boundary(
                    session, organization_id, transit.id, _origin_frontier_tokens(frontier),
                    current_unit=transit, expected_state="in_transit", allow_zero=True,
                )
            except _OriginUnproven as exc:
                raise HTTPException(409, "История остатка в пути не подтверждена") from exc
            # Even exhausted cargo must be proved before it can be omitted.
            if proven["qty"] == 0:
                continue
            items.append({
                "dispatch_operation_id": dispatch.operation_id,
                "source_warehouse_id": sent["source_warehouse_id"],
                "destination_warehouse_id": warehouse_id,
                "transit_unit_id": transit.id,
                "remaining": decimal_text(proven["qty"]),
                "sku_code": transit.sku_code, "batch_id": transit.batch_id,
                "packaging_id": transit.packaging_id, "package_state": transit.package_state,
                "quality": transit.quality, "container": sent["container"],
                "batch_scan": f"LOT-{transit.batch_id}-{transit.id.upper()}",
                "container_scan": f"BOX-{sent['container']}",
            })
        await self._fresh_transfer_authority(session, organization_id, actor, receipt=True)
        return {"protocol": "wms.mobile.transfer.manifest.v1", "warehouse_id": warehouse_id, "items": items}

    async def _transfer_dispatch_receipt(self, session, organization_id, data):
        from modules.wms.item_identity import (
            _origin_operation_snapshot,
            _origin_validate_operation,
            _OriginUnproven,
        )
        dispatch = await session.scalar(select(PhysicalOperation).where(
            PhysicalOperation.organization_id == organization_id,
            PhysicalOperation.operation_id == str(data.dispatch_operation_id),
        ).execution_options(populate_existing=True))
        if dispatch is None or dispatch.kind != "transfer_dispatch":
            reject("Подтверждённая межскладская отправка не найдена")
        try:
            _, request, result = _origin_validate_operation(_origin_operation_snapshot(dispatch), organization_id)
        except _OriginUnproven as exc:
            raise HTTPException(409, "Доказательство отправки повреждено") from exc
        if (request["destination_warehouse_id"] != data.warehouse_id
                or result["transfer"]["transit_unit_id"] != str(data.unit_id)):
            reject("Отправка предназначена другому складу или относится к другому остатку")
        return dispatch, result["transfer"]

    async def _transfer_dispatch(self, session, warehouse, data, actor, controls):
        from modules.wms.item_identity import (
            _origin_frontier_tokens,
            _OriginUnproven,
            _prove_unit_boundary,
        )
        from modules.wms.physical_models import PhysicalCarrier
        from modules.wms.physical_schemas import TransferReceipt

        unit = await self._unit(session, warehouse, data.unit_id)
        target = controls[data.destination_warehouse_id]
        if target.organization_id != warehouse.organization_id or target.mode != "erp":
            reject("Склад назначения не соответствует организации или ещё не переключён")
        if (data.location_scan != f"LOC-{unit.location_id}"
                or data.batch_scan != f"LOT-{unit.batch_id}-{unit.id.upper()}"
                or data.container_scan != f"BOX-{data.container}"):
            reject("Не совпадает скан ячейки, партии или транспортного контейнера")
        lot = await effective_lot(session, self.reference, warehouse.organization_id, unit)
        await self._assembly_item_scan(session, warehouse, unit, data.qty, data.item_scan, lot)
        await policy(session, warehouse.warehouse_id, unit.sku_code, data.qty)
        if data.qty > unit.qty - unit.reserved - await pending_move_qty(session, unit.id):
            reject("Отправляемое количество зарезервировано или отсутствует")
        if unit.assembly_output_id is not None and data.qty != 1:
            reject("Изделие сборки перемещается только целиком")
        frontier, _ = await physical_unit_frontier(session, warehouse.organization_id, unit.id)
        try:
            await _prove_unit_boundary(session, warehouse.organization_id, unit.id,
                _origin_frontier_tokens(frontier), current_unit=unit)
        except _OriginUnproven as exc:
            raise HTTPException(409, "Происхождение отправляемого остатка не подтверждено") from exc
        carrier = None
        if unit.carrier_id is not None:
            carrier = await session.scalar(select(PhysicalCarrier).where(
                PhysicalCarrier.id == unit.carrier_id, PhysicalCarrier.warehouse_id == warehouse.warehouse_id,
                PhysicalCarrier.location_id == unit.location_id,
            ).with_for_update().execution_options(populate_existing=True))
            if carrier is None:
                reject("Не подтверждён носитель отправляемого товара")
        transit = self._clone(session, unit, data.qty, location_id=None, container=data.container,
            stock_state="in_transit", dispatch_operation_id=str(data.operation_id))
        unit.qty -= data.qty
        if carrier is not None:
            carrier.content_revision += 1
            carrier.confirmed_content_revision = None
        self._deltas.extend([(unit, -data.qty, transfer_dimensions(unit)),
                             (transit, data.qty, transfer_dimensions(transit))])
        receipt = TransferReceipt(protocol="wms.transfer.v1", dispatch_operation_id=data.operation_id,
            source_warehouse_id=warehouse.warehouse_id, destination_warehouse_id=target.warehouse_id,
            source_unit_id=unit.id, transit_unit_id=transit.id, received_unit_id=None,
            qty=data.qty, remaining=data.qty, container=data.container)
        return {"transfer": receipt.model_dump(mode="json")}, None

    async def _transfer_receive(self, session, warehouse, data, actor, controls):
        from modules.wms.item_identity import (
            _origin_frontier_tokens,
            _OriginUnproven,
            _prove_unit_boundary,
        )
        from modules.wms.physical_schemas import TransferReceipt

        dispatch, sent = await self._transfer_dispatch_receipt(session, warehouse.organization_id, data)
        if dispatch.warehouse_id not in controls:
            reject("Источник отправки изменился после выбора складских блокировок")
        transit = await session.scalar(select(StockUnit).where(
            StockUnit.id == str(data.unit_id), StockUnit.warehouse_id == dispatch.warehouse_id,
        ).with_for_update().execution_options(populate_existing=True))
        try:
            require_current_in_transit(transit)
        except CurrentStockStateError as exc:
            raise HTTPException(409, "Остаток в пути не подтверждён") from exc
        if transit.dispatch_operation_id != dispatch.operation_id:
            reject("Остаток относится к другой отправке")
        frontier, _ = await physical_unit_frontier(session, warehouse.organization_id, transit.id)
        try:
            proven = await _prove_unit_boundary(session, warehouse.organization_id, transit.id,
                _origin_frontier_tokens(frontier), current_unit=transit, expected_state="in_transit")
        except _OriginUnproven as exc:
            raise HTTPException(409, "История остатка в пути не подтверждена") from exc
        if data.qty > proven["qty"]:
            reject("Принимаемое количество превышает подтверждённый остаток в пути")
        destination = await location(session, warehouse.warehouse_id, data.destination_id)
        if destination.kind != "receiving":
            reject("Межскладской товар принимается в зоне приёмки")
        if (data.location_scan != f"LOC-{destination.location_id}"
                or data.batch_scan != f"LOT-{transit.batch_id}-{transit.id.upper()}"
                or data.container_scan != f"BOX-{sent['container']}"):
            reject("Не совпадает скан зоны приёмки, партии или транспортного контейнера")
        lot = await effective_lot(session, self.reference, warehouse.organization_id, transit,
            expected_state="in_transit")
        await self._assembly_item_scan(session, warehouse, transit, data.qty, data.item_scan, lot)
        if transit.assembly_output_id is not None and data.qty != 1:
            reject("Изделие сборки принимается только целиком")
        await policy(session, dispatch.warehouse_id, transit.sku_code, data.qty)
        await policy(session, warehouse.warehouse_id, transit.sku_code, data.qty)
        await capacity(session, destination, transit.sku_code, data.qty)
        received = self._clone(session, transit, data.qty,
            warehouse_id=warehouse.warehouse_id, location_id=destination.location_id)
        transit.qty -= data.qty
        self._deltas.extend([(transit, -data.qty, transfer_dimensions(transit)),
                             (received, data.qty, transfer_dimensions(received))])
        receipt = TransferReceipt(protocol="wms.transfer.v1", dispatch_operation_id=dispatch.operation_id,
            source_warehouse_id=dispatch.warehouse_id, destination_warehouse_id=warehouse.warehouse_id,
            source_unit_id=sent["source_unit_id"], transit_unit_id=transit.id, received_unit_id=received.id,
            qty=data.qty, remaining=transit.qty, container=sent["container"])
        return {"transfer": receipt.model_dump(mode="json"), "unit": unit_output(received)}, None

    async def _open_package(self, session, warehouse, data, actor, controls):
        unit = await self._unit(session, warehouse, data.unit_id)
        if unit.package_state != "sealed" or data.qty > unit.qty - unit.reserved:
            reject("Вскрытие доступно для свободных закрытых коробок")
        await package_check(session, warehouse, unit.sku_code, data.qty, unit.packaging_id, "sealed")
        unit.qty -= data.qty
        opened = self._clone(session, unit, data.qty, package_state="open")
        self._deltas.extend([(unit, -data.qty), (opened, data.qty)])
        await session.flush()
        return {"unit": unit_output(opened)}, None

    async def _reserve(self, session, warehouse, data, actor, controls):
        if data.source.startswith(("invoice:", "assembly:")):
            reject("Связанный резерв создаётся только через подтверждённый источник заказа")
        holds = await allocate(session, self.reference, warehouse, data.sku_code, data.qty, data.source,
            data.container, data.characteristic, data.purpose, data.min_shelf_days, data.due_at)
        return {"holds": [{"id": row.id, "unit_id": row.unit_id, "qty": decimal_text(row.qty),
            "container": row.container, "source": row.source} for row in holds]}, None

    async def _adopt_reservation(self, session, warehouse, data, actor, controls):
        if warehouse.mode != "preparing" or not warehouse.opening_count_id:
            reject("Перенос резерва выполняется между начальным пересчётом и переключением")
        old = await session.get(ReservationVersion, data.reservation_id)
        directory = await session.get(WarehouseDirectory, warehouse.warehouse_id)
        if old is None or old.organization_id != warehouse.organization_id or old.warehouse != directory.name:
            reject("Резерв не принадлежит выбранной организации и складу")
        if old.source.startswith("assembly:"):
            reject("Резерв сборки нельзя переносить generic-операцией")
        latest = await session.scalar(select(ReservationVersion).where(ReservationVersion.source == old.source,
            ReservationVersion.organization_id == warehouse.organization_id).order_by(ReservationVersion.version.desc()))
        if latest.id != old.id or old.version != data.expected_version or old.qty != data.expected_qty:
            reject("Версия или количество переносимого резерва изменились")
        characteristic, purpose, shelf_days, due_at = data.characteristic, data.purpose, 0, None
        order_ref = old.source
        if old.source.startswith("invoice:"):
            from modules.wms.invoice_reservations import InvoiceReservation
            parts = old.source.split(":")
            if len(parts) != 4 or not parts[1].isdigit():
                reject("Перенос счёта требует подтверждённое исходное распределение продаж")
            confirmed = await session.get(InvoiceReservation, int(parts[1]))
            allocation = next((r for r in confirmed.snapshot["allocations"] if r["source"] == old.source), None) if confirmed else None
            if (confirmed is None or confirmed.organization_id != warehouse.organization_id or allocation is None
                    or allocation["sku_code"] != old.sku_code or allocation["warehouse"] != old.warehouse
                    or old.qty > Decimal(allocation["qty"])):
                reject("Перенос счёта требует подтверждённое исходное распределение продаж")
            if any(getattr(data, key) and getattr(data, key) != allocation.get(key, "") for key in ("characteristic", "purpose")):
                reject("Характеристика или назначение отличается от подтверждённого счёта")
            characteristic, purpose = allocation.get("characteristic", ""), allocation.get("purpose", "")
            shelf_days = allocation.get("min_shelf_days", 0)
            due_at = datetime.fromisoformat(allocation["due_at"]) if allocation.get("due_at") else None
            order_ref = ":".join(parts[:2])
        holds = await allocate(session, self.reference, warehouse, old.sku_code, old.qty, old.source,
            data.container, characteristic, purpose, shelf_days, due_at, order_ref=order_ref)
        return {"reservation_id": old.id, "source": old.source,
            "holds": [{"id": h.id, "unit_id": h.unit_id, "qty": decimal_text(h.qty),
                "container": h.container, "source": h.source} for h in holds]}, None

    async def _release(self, session, warehouse, data, actor, controls):
        if data.source.startswith(("invoice:", "assembly:")):
            reject("Связанный резерв снимается только исходным подтверждённым документом")
        rows = (await session.scalars(select(PhysicalHold).where(PhysicalHold.warehouse_id == warehouse.warehouse_id,
            PhysicalHold.source == data.source).order_by(PhysicalHold.id))).all()
        if not rows:
            reject("Резерв не найден", 404)
        if any(row.picked > row.shipped for row in rows):
            reject("Сначала верните подобранный товар из контейнера операцией отмены подбора")
        total = Decimal(0)
        for row in rows:
            remaining = row.qty - row.picked - row.released
            unit = await self._unit(session, warehouse, row.unit_id)
            unit.reserved -= remaining
            row.released += remaining
            total += remaining
        tasks = (await session.scalars(select(PhysicalTask).where(PhysicalTask.warehouse_id == warehouse.warehouse_id,
            PhysicalTask.source == data.source, PhysicalTask.status.in_(["open", "claimed"]), PhysicalTask.kind == "pick"))).all()
        for task in tasks:
            task.status = "canceled"
        return {"released": decimal_text(total)}, None

    async def _pick(self, session, warehouse, data, actor, controls, *, prechecked_delivery_context=None):
        from modules.wms.physical_delivery import bound_task_remaining, pick_remaining

        hold = await session.get(PhysicalHold, data.hold_id)
        if hold is None or hold.warehouse_id != warehouse.warehouse_id:
            reject("Строка резерва не найдена", 404)
        if hold.order_ref.startswith("assembly:"):
            reject("Комплект сборки перемещается только typed-операцией исполнения сборки")
        if data.task_id is not None:
            task = await session.scalar(select(PhysicalTask).where(
                PhysicalTask.id == str(data.task_id),
                PhysicalTask.hold_id == hold.id,
                PhysicalTask.warehouse_id == warehouse.warehouse_id,
            ).with_for_update().execution_options(populate_existing=True))
        else:
            tasks = (await session.scalars(select(PhysicalTask).where(
                PhysicalTask.hold_id == hold.id,
                PhysicalTask.kind == "pick", PhysicalTask.status == "claimed",
                PhysicalTask.assignee == actor,
            ).order_by(PhysicalTask.id).with_for_update().execution_options(populate_existing=True))).all()
            task = tasks[0] if len(tasks) == 1 else None
        if (task is None or task.kind != "pick" or task.status != "claimed" or task.assignee != actor):
            reject("Сначала возьмите задание на подбор в работу")
        immutable_target_remaining = None
        if task.binding_revision_id is not None:
            from modules.wms.physical_delivery import binding_snapshot, validate_binding_row

            checked_binding = (prechecked_delivery_context or {}).get("binding")
            if checked_binding is None or checked_binding.id != task.binding_revision_id:
                reject("Привязка задания изменилась; повторите подбор с её свежей проверкой", 409)

            binding = await session.get(DeliveryBindingRevision, task.binding_revision_id)
            operation = await session.get(PhysicalOperation, binding.operation_pk) if binding is not None else None
            if binding is None or operation is None:
                reject("Задание привязки доставки повреждено", 409)
            snapshot = validate_binding_row(binding, operation)
            immutable_target_remaining = await bound_task_remaining(session, task, snapshot)
            if task.qty is None or task.qty != immutable_target_remaining:
                reject("Остаток задания не совпадает с неизменяемой целью доставки", 409)
        unit = await self._unit(session, warehouse, hold.unit_id)
        source = await location(session, warehouse.warehouse_id, unit.location_id)
        destination = await location(session, warehouse.warehouse_id, data.destination_id)
        if destination.kind != "packing" or source.kind not in {"pick", "storage"}:
            reject("Подбор выполняется из хранения/отбора в упаковку")
        reason, lot = await eligible(session, self.reference, warehouse, unit, hold.min_shelf_days)
        if reason:
            reject(f"Партия заблокирована: {reason}")
        if data.location_scan != f"LOC-{source.location_id}" or data.batch_scan != f"LOT-{unit.batch_id}-{unit.id.upper()}":
            reject("Не совпадает скан ячейки или партии")
        await package_check(session, warehouse, unit.sku_code, data.qty, unit.packaging_id, unit.package_state)
        sku = await session.scalar(select(Sku).where(Sku.code == unit.sku_code))
        if sku is None or not sku.is_active:
            reject("Товар подбора не подтверждён")
        from modules.wms.item_identity import resolve_item_scan

        resolved = await resolve_item_scan(
            session, warehouse.organization_id, data.item_scan, warehouse_id=warehouse.warehouse_id,
        )
        if (resolved["sku_id"] != sku.id or resolved["packaging_id"] != unit.packaging_id
                or data.container_scan != f"BOX-{hold.container}"):
            reject("Не совпадает скан товара/упаковки или контейнера заказа")
        gs1 = resolved["gs1"]
        if gs1:
            if gs1["lot_no"] and gs1["lot_no"] != lot["lot_no"]:
                reject("GS1 обозначает другую партию")
            for scanned, field in ((gs1["manufacture"], "mfg_date"), (gs1["expiry"], "expiry_date")):
                if scanned and (scanned["value"] != lot.get(field)
                        or (field == "mfg_date" and scanned["precision"] != lot.get("manufacture_precision"))):
                    reject("Дата GS1 не совпадает с подтверждёнными данными партии")
        await policy(session, warehouse.warehouse_id, unit.sku_code, data.qty)
        hold_remaining = hold.qty - hold.picked - hold.released
        bound_other = (await session.scalars(select(PhysicalTask).where(
            PhysicalTask.hold_id == hold.id,
            PhysicalTask.kind == "pick",
            PhysicalTask.binding_revision_id.is_not(None),
            PhysicalTask.status.in_(["open", "claimed"]),
            PhysicalTask.id != task.id,
        ))).all()
        bound_protected = Decimal("0")
        for other_task in bound_other:
            bound_protected += await bound_task_remaining(session, other_task)
        if task.qty is None or data.qty > unit.reserved:
            reject("Количество превышает остаток строки резерва")
        task_remaining = immutable_target_remaining if immutable_target_remaining is not None else task.qty
        new_task_qty = pick_remaining(task_remaining, hold_remaining, bound_protected, data.qty)
        await capacity(session, destination, unit.sku_code, data.qty)
        unit.qty -= data.qty
        unit.reserved -= data.qty
        picked = self._clone(session, unit, data.qty, reserved=data.qty,
            location_id=destination.location_id, container=hold.container)
        # reserved is not a dimension, set it after construction.
        hold.picked += data.qty
        await session.flush()
        allocation = PickedAllocation(hold_id=hold.id, unit_id=picked.id, qty=data.qty,
            shipped=Decimal(0), packed=False, packing_evidence="", task_id=task.id,
            binding_revision_id=task.binding_revision_id)
        session.add(allocation)
        task.qty = new_task_qty
        if task.qty == 0:
            task.status = "done"
        self._deltas.extend([(unit, -data.qty), (picked, data.qty)])
        await session.flush()
        await finish_pick_tasks(session, hold)
        return {"pick_id": allocation.id, "task_id": task.id,
            "binding_revision_id": task.binding_revision_id, "unit": unit_output(picked), "lot": lot["lot_no"]}, None

    async def _unpick(self, session, warehouse, data, actor, controls):
        from modules.wms.physical_delivery import restore_task_quantity

        picked = await session.get(PickedAllocation, data.pick_id)
        if picked is None:
            reject("Подбор не найден", 404)
        hold = await session.get(PhysicalHold, picked.hold_id)
        if hold.warehouse_id != warehouse.warehouse_id or picked.shipped > 0:
            reject("После физической отгрузки требуется возврат")
        if hold.order_ref.startswith("assembly:"):
            reject("Комплект сборки нельзя отменить generic-операцией подбора")
        if picked.canceled:
            reject("Подбор уже отменён")
        task = None
        if picked.task_id is not None:
            task = await session.scalar(select(PhysicalTask).where(
                PhysicalTask.id == picked.task_id,
                PhysicalTask.hold_id == hold.id,
                PhysicalTask.warehouse_id == warehouse.warehouse_id,
            ).with_for_update().execution_options(populate_existing=True))
            if task is None or task.binding_revision_id != picked.binding_revision_id:
                reject("История задания подбора повреждена", 409)
            if picked.binding_revision_id is not None:
                from modules.wms.physical_delivery import (
                    binding_snapshot, bound_task_remaining, validate_binding_row,
                )

                binding = await session.get(DeliveryBindingRevision, picked.binding_revision_id)
                operation = await session.get(PhysicalOperation, binding.operation_pk) if binding is not None else None
                if binding is None or operation is None:
                    reject("Исходная привязка отменяемого подбора не найдена", 409)
                snapshot = validate_binding_row(binding, operation)
                target_row = next((row for row in snapshot["allocations"] if row["task_id"] == task.id), None)
                if target_row is None:
                    reject("Подбор не принадлежит исходной привязке", 409)
                await bound_task_remaining(session, task, snapshot)
        unit = await self._unit(session, warehouse, picked.unit_id)
        origin = await self._unit(session, warehouse, hold.unit_id)
        if (unit.assembly_output_id is not None or origin.assembly_output_id is not None) and (
            unit.assembly_output_id != origin.assembly_output_id or picked.qty != 1 or unit.qty != 1 or origin.qty != 0
        ):
            reject("Отмена подбора должна переносить одну и ту же целую идентичность")
        destination = await location(session, warehouse.warehouse_id, origin.location_id)
        await capacity(session, destination, origin.sku_code, picked.qty)
        qty = picked.qty
        origin.qty += qty
        origin.reserved += qty
        unit.qty -= qty
        unit.reserved -= qty
        hold.picked -= qty
        # Keep the immutable pick identity; a separate canceled flag avoids deleting history.
        picked.packed = False
        picked.packing_evidence = f"canceled:{data.operation_id}:{data.evidence}"[:1000]
        picked.canceled = True
        if task is not None:
            task.status, task.assignee = "open", ""
            if picked.binding_revision_id is None:
                task.qty = restore_task_quantity(task.qty or Decimal("0"), qty)
        else:
            legacy_tasks = (await session.scalars(select(PhysicalTask).where(
                PhysicalTask.hold_id == hold.id, PhysicalTask.kind == "pick",
                PhysicalTask.binding_revision_id.is_(None),
            ).order_by(PhysicalTask.id).with_for_update().execution_options(populate_existing=True))).all()
            if len(legacy_tasks) > 1:
                reject("Нельзя однозначно восстановить задание отменяемого подбора", 409)
            if legacy_tasks:
                task = legacy_tasks[0]
                task.status, task.assignee = "open", ""
        self._deltas.extend([(unit, -qty), (origin, qty)])
        await finish_pick_tasks(session, hold)
        return {"returned_to_location": origin.location_id, "qty": decimal_text(qty)}, None


    async def _pack_v2(self, session, warehouse, data, actor, controls):
        from modules.wms.physical_delivery import bound_task_remaining, validate_task_pick_totals

        if data.source.startswith("assembly:"):
            reject("Исполнение сборки не упаковывается generic-операцией продаж")

        binding = None
        binding_snapshot_value = None
        if data.binding_revision_id is not None:
            from modules.wms.physical_delivery import (
                binding_snapshot, validate_binding_row, validate_pick_place_partition,
            )

            binding = await session.get(DeliveryBindingRevision, str(data.binding_revision_id))
            operation = await session.get(PhysicalOperation, binding.operation_pk) if binding is not None else None
            if (binding is None or operation is None or binding.organization_id != warehouse.organization_id
                    or binding.warehouse_id != warehouse.warehouse_id):
                reject("Привязка упаковки не относится к выбранному складу", 409)
            binding_snapshot_value = validate_binding_row(binding, operation)
            if (binding_snapshot_value.get("order_ref") != data.source
                    or binding_snapshot_value.get("container") != data.container):
                reject("Упаковка должна сохранить заказ и контейнер привязки", 409)

        clone_type = aliased(StockUnit)
        original_type = aliased(StockUnit)
        row_query = (
            select(PickedAllocation, PhysicalHold, clone_type, original_type)
            .join(PhysicalHold, PickedAllocation.hold_id == PhysicalHold.id)
            .outerjoin(clone_type, PickedAllocation.unit_id == clone_type.id)
            .outerjoin(original_type, PhysicalHold.unit_id == original_type.id)
            .where(
                PhysicalHold.warehouse_id == warehouse.warehouse_id,
                PhysicalHold.order_ref == data.source,
                PhysicalHold.container == data.container,
                PickedAllocation.canceled.is_(False),
                PickedAllocation.shipped < PickedAllocation.qty,
            )
            .order_by(PickedAllocation.id)
            .execution_options(populate_existing=True)
        )
        if binding is not None:
            row_query = row_query.where(PickedAllocation.binding_revision_id == binding.id)
        rows = (await session.execute(row_query)).all()
        if not rows:
            reject("В контейнере нет подобранного товара")

        row_pick_ids = {pick.id for pick, _, _, _ in rows}
        bound_ids = {pick.binding_revision_id for pick, _, _, _ in rows}
        if binding is None:
            if any(value is not None for value in bound_ids):
                reject("Упаковка связанного подбора требует его идентификатор привязки", 409)
        else:
            if bound_ids != {binding.id}:
                reject("Упаковка не может смешивать связанные и обычные подборы", 409)
            all_binding_picks = (await session.scalars(select(PickedAllocation).where(
                PickedAllocation.binding_revision_id == binding.id,
                PickedAllocation.canceled.is_(False),
            ).order_by(PickedAllocation.id).with_for_update().execution_options(populate_existing=True))).all()
            if ({pick.id for pick in all_binding_picks} != row_pick_ids
                    or any(pick.shipped != 0 for pick in all_binding_picks)):
                reject("Одна упаковка должна завершить весь активный набор подбора привязки", 409)
            for allocation in binding_snapshot_value["allocations"]:
                task = await session.get(PhysicalTask, allocation["task_id"])
                if task is None:
                    reject("Задание исходной привязки доставки не найдено", 409)
                await bound_task_remaining(session, task, binding_snapshot_value)
            if data.shipping_packages is None:
                reject("Для связанной доставки нужны измеренные места упаковки", 409)
            validate_pick_place_partition(all_binding_picks, data.shipping_packages)
            actual_by_task = defaultdict(lambda: Decimal("0"))
            for pick in all_binding_picks:
                if pick.task_id is None:
                    reject("Связанный подбор не содержит идентификатор задания", 409)
                actual_by_task[pick.task_id] += pick.qty
            expected_by_task = {row["task_id"]: Decimal(row["target_qty"])
                for row in binding_snapshot_value["allocations"]}
            pending = await session.scalar(select(PhysicalTask.id).where(
                PhysicalTask.binding_revision_id == binding.id,
                PhysicalTask.status.in_(["open", "claimed"]),
            ).limit(1))
            validate_task_pick_totals(expected_by_task, actual_by_task)
            if pending is not None:
                reject("Нельзя упаковать частичную цель привязки", 409)
        line_by_id = {line.pick_id: line for line in data.lines}
        if set(line_by_id) != row_pick_ids:
            reject("Строки доказательства должны точно совпадать со всем остатком подбора")
        if any(pick.packed for pick, _, _, _ in rows):
            reject("Контейнер уже упакован; повторите исходную команду по её идентификатору")
        if data.container_scan != f"BOX-{data.container}":
            reject("Не совпадает скан контейнера заказа")

        destination = await location(session, warehouse.warehouse_id, data.destination_id)
        if destination.kind != "dispatch":
            reject("Упакованный заказ направляется в зону готовой отгрузки")

        shipping_quantities = defaultdict(list)
        for package in data.shipping_packages or ():
            for line in package.lines:
                shipping_quantities[line.pick_id].append(line.qty)

        incoming = defaultdict(lambda: Decimal(0))
        qualified = []
        for pick, hold, clone, original in rows:
            line = line_by_id[pick.id]
            if (hold.warehouse_id != warehouse.warehouse_id or hold.order_ref != data.source
                    or hold.container != data.container):
                reject("Подбор не относится к этому заказу и контейнеру")
            if clone is None or original is None:
                reject("Подобранная единица или исходная партия не найдена")
            if (clone.warehouse_id != warehouse.warehouse_id
                    or original.warehouse_id != warehouse.warehouse_id):
                reject("Подбор относится к другому складу")

            remaining = pick.qty - pick.shipped
            if line.qty != remaining or clone.qty != remaining or clone.reserved != remaining:
                reject("Количество доказательства и остаток подобранной единицы не совпадают")
            if clone.container != hold.container:
                reject("Контейнер подобранной единицы изменился")
            if (clone.sku_code != original.sku_code or clone.batch_id != original.batch_id
                    or clone.packaging_id != original.packaging_id
                    or clone.characteristic != original.characteristic
                    or clone.purpose != original.purpose):
                reject("Исходная партия и подобранная единица имеют разные измерения")
            if line.batch_scan != f"LOT-{original.batch_id}-{original.id.upper()}":
                reject("Не совпадает скан исходной партии")

            await location(session, warehouse.warehouse_id, original.location_id)
            clone_location = await location(session, warehouse.warehouse_id, clone.location_id)
            if clone_location.kind != "packing":
                reject("Подобранная единица находится вне зоны упаковки")

            reason, clone_lot = await eligible(
                session, self.reference, warehouse, clone, hold.min_shelf_days,
            )
            if reason:
                reject(f"Упаковка заблокирована: {reason}")
            original_lot = await effective_lot(session, self.reference, warehouse.organization_id, original)
            await package_check(
                session, warehouse, clone.sku_code, remaining,
                clone.packaging_id, clone.package_state,
            )
            await policy(session, warehouse.warehouse_id, clone.sku_code, line.qty)
            quantities = shipping_quantities.get(pick.id, ())
            if (
                data.shipping_packages is not None
                and clone.assembly_output_id is not None
                and len(quantities) != 1
            ):
                reject("Единица сборки должна целиком находиться в одном отправляемом месте", 422)
            for quantity in quantities:
                await policy(session, warehouse.warehouse_id, clone.sku_code, quantity)
                await package_check(
                    session, warehouse, clone.sku_code, quantity,
                    clone.packaging_id, clone.package_state,
                )
            sku = await session.scalar(select(Sku).where(Sku.code == clone.sku_code))
            if sku is None:
                reject("Товар подбора не подтверждён")
            if not sku.is_active:
                reject("Товар подбора больше не активен")
            from modules.wms.item_identity import resolve_item_scan

            resolved = await resolve_item_scan(
                session, warehouse.organization_id, line.item_scan, warehouse_id=warehouse.warehouse_id,
            )
            if resolved["sku_id"] != sku.id or resolved["packaging_id"] != clone.packaging_id:
                reject("Не совпадает скан товара или упаковки")
            gs1 = resolved["gs1"]
            if gs1:
                if gs1["lot_no"] and gs1["lot_no"] != original_lot["lot_no"]:
                    reject("GS1 обозначает другую исходную партию")
                for scanned, field in ((gs1["manufacture"], "mfg_date"), (gs1["expiry"], "expiry_date")):
                    if scanned and (
                        scanned["value"] != original_lot.get(field)
                        or (field == "mfg_date"
                            and scanned["precision"] != original_lot.get("manufacture_precision"))
                    ):
                        reject("Дата GS1 не совпадает с подтверждёнными данными исходной партии")
            if clone_lot["lot_no"] != original_lot["lot_no"]:
                reject("Партия clone не совпадает с исходной партией")

            incoming[clone.sku_code] += clone.qty
            qualified.append((pick, clone))

        for sku_code, qty in incoming.items():
            await capacity(session, destination, sku_code, qty)

        for pick, clone in qualified:
            self._deltas.append((clone, -clone.qty, dimensions(clone)))
            clone.location_id = destination.location_id
            self._deltas.append((clone, clone.qty, dimensions(clone)))
            pick.packed, pick.packing_evidence = True, data.evidence

        result = {"container": data.container, "packed": True, "protocol": data.protocol}
        packages = data.shipping_package_proof()
        if packages is not None:
            result["shipping_packages_v1"] = packages
        if data.binding_revision_id is not None:
            result["binding_revision_id"] = str(data.binding_revision_id)
        return result, None

    async def _delivery_bind(self, session, warehouse, data, actor, controls, *, delivery_context, request_hash):
        from modules.wms.physical_delivery import (
            binding_key, binding_snapshot, seal_binding_snapshot, split_task_target,
        )

        if warehouse.onec_ref is not None or warehouse.mode != "erp":
            reject("Привязка доставки доступна только автономному ERP-складу", 409)
        if delivery_context is None:
            reject("Для привязки доставки нужна свежая проверка исходных систем", 409)
        if await session.scalar(select(DeliveryBindingRevision.id).where(
                DeliveryBindingRevision.organization_id == warehouse.organization_id,
                DeliveryBindingRevision.execution_id == str(data.execution_id),
                DeliveryBindingRevision.warehouse_id == warehouse.warehouse_id,
                DeliveryBindingRevision.revision == 1).with_for_update()):
            reject("Для этого исполнения и склада привязка уже создана", 409)

        directory_name = await session.scalar(select(WarehouseDirectory.name).where(
            WarehouseDirectory.id == warehouse.warehouse_id,
        ))
        if not directory_name:
            reject("Не найден явно выбранный склад", 409)
        source_allocations = {row.get("source"): row for row in delivery_context["allocations"]
            if type(row) is dict and isinstance(row.get("source"), str)}
        latest_remaining = {row.source: Decimal(row.qty)
            for row in delivery_context["latest_versions"]}
        already_bound = defaultdict(lambda: Decimal("0"))
        prior_rows = (await session.scalars(select(DeliveryBindingRevision).where(
            DeliveryBindingRevision.organization_id == warehouse.organization_id,
            DeliveryBindingRevision.execution_id == str(data.execution_id),
        ).order_by(DeliveryBindingRevision.warehouse_id, DeliveryBindingRevision.revision))).all()
        for prior in prior_rows:
            prior_operation = await session.get(PhysicalOperation, prior.operation_pk)
            if prior_operation is None:
                reject("Привязка другого склада не имеет исходной квитанции", 409)
            from modules.wms.physical_delivery import validate_binding_row

            prior_snapshot = validate_binding_row(prior, prior_operation)
            if (prior_snapshot.get("invoice") != {
                    "document_id": data.document_id,
                    "version": data.expected_version,
                    "content_sha256": data.expected_content_sha256,
                    "reservation_digest": data.expected_reservation_digest,
                    "reservation_snapshot_digest": delivery_context["reservation_snapshot_digest"],
                }
                    or prior_snapshot.get("producer_source") != {
                        "kind": delivery_context["source"]["kind"],
                        "key": delivery_context["source"]["key"],
                        "revision": delivery_context["source"]["revision"],
                        "intent_digest": data.expected_intent_digest,
                        "state_sha256": data.expected_source_state_sha256,
                    }):
                reject("Исполнение уже связано с другой версией счёта или источника", 409)
            for row in prior_snapshot["allocations"]:
                already_bound[row["source_key"]] += Decimal(row["target_qty"])

        target_rows = []
        tasks = []
        source_totals = defaultdict(lambda: Decimal("0"))
        common_order_ref = None
        common_container = None
        cutoff = datetime.fromisoformat(delivery_context["intent"]["priority_cutoff_at"])
        for selection in data.allocations:
            task = await session.scalar(select(PhysicalTask).where(
                PhysicalTask.id == str(selection.task_id),
                PhysicalTask.warehouse_id == warehouse.warehouse_id,
            ).with_for_update().execution_options(populate_existing=True))
            if (task is None or task.kind != "pick" or task.status != "open" or task.assignee
                    or task.binding_revision_id is not None or task.hold_id is None):
                reject("Привязать можно только открытое непомеченное задание подбора", 409)
            hold = await session.scalar(select(PhysicalHold).where(
                PhysicalHold.id == task.hold_id,
                PhysicalHold.warehouse_id == warehouse.warehouse_id,
            ).with_for_update().execution_options(populate_existing=True))
            if (hold is None or hold.unit_id != task.unit_id or hold.source != task.source
                    or hold.order_ref != f"invoice:{data.document_id}"
                    or hold.picked != 0 or hold.shipped != 0 or hold.consumed != 0 or hold.released != 0):
                reject("Задание больше не совпадает с нетронутой строкой резерва счёта", 409)
            unit = await session.get(StockUnit, hold.unit_id)
            if unit is None:
                reject("Не найдена исходная единица строки резерва", 409)
            allocation = source_allocations.get(hold.source)
            if (allocation is None or allocation.get("warehouse") != directory_name
                    or allocation.get("sku_code") != unit.sku_code):
                reject("Строка задания отсутствует в точном резерве выбранного склада", 409)
            active_siblings = (await session.scalars(select(PhysicalTask).where(
                PhysicalTask.hold_id == hold.id,
                PhysicalTask.kind == "pick",
                PhysicalTask.status.in_(["open", "claimed"]),
            ).with_for_update().execution_options(populate_existing=True))).all()
            if len(active_siblings) != 1 or active_siblings[0].id != task.id:
                reject("Строка резерва уже распределена между другими заданиями", 409)
            hold_remaining = hold.qty - hold.picked - hold.released
            if task.qty is None or task.qty != hold_remaining or selection.qty > task.qty:
                reject("Цель привязки превышает остаток выбранного задания", 409)
            if await session.scalar(select(PickedAllocation.id).where(
                    PickedAllocation.hold_id == hold.id,
            ).limit(1)):
                reject("Нельзя привязать строку резерва после начала подбора", 409)
            if common_order_ref is None:
                common_order_ref, common_container = hold.order_ref, hold.container
            elif hold.order_ref != common_order_ref or hold.container != common_container:
                reject("Одна привязка должна относиться к одному заказу и одному контейнеру", 409)
            target = selection.qty
            source_totals[hold.source] += target
            target_rows.append({
                "task_id": task.id,
                "hold_id": hold.id,
                "unit_id": hold.unit_id,
                "source_key": hold.source,
                "target_qty": decimal_text(target),
                "remaining_at_binding_qty": decimal_text(task.qty),
            })
            tasks.append((task, hold, target))

        for source_key, target_total in source_totals.items():
            allocation = source_allocations[source_key]
            try:
                reserved_total = Decimal(str(allocation["qty"]))
            except (KeyError, TypeError, ValueError, InvalidOperation):
                reject("Количество строки подтверждённого резерва повреждено", 409)
            current_total = latest_remaining.get(source_key)
            if (current_total is None or already_bound[source_key] + target_total > reserved_total
                    or already_bound[source_key] + target_total > current_total):
                reject("Привязка превысит подтверждённое количество строки резерва", 409)

        for task, hold, requested_target in tasks:
            prior_due_at = task.due_at
            target, residual = split_task_target(task.qty, requested_target)
            task.qty = target
            task.due_at = cutoff
            if residual > 0:
                session.add(PhysicalTask(
                    id=str(uuid4()), warehouse_id=warehouse.warehouse_id, kind="pick", status="open",
                    source=task.source, unit_id=task.unit_id, hold_id=task.hold_id, qty=residual,
                    due_at=prior_due_at, assignee="", evidence=task.evidence,
                ))

        execution_source = delivery_context["source"]
        snapshot = {
            "schema_version": 1,
            "binding_id": str(data.operation_id),
            "binding_key": binding_key(data.execution_id, warehouse.warehouse_id, warehouse.organization_id),
            "revision": 1,
            "organization_id": warehouse.organization_id,
            "warehouse_id": warehouse.warehouse_id,
            "execution_id": str(data.execution_id),
            "invoice": {
                "document_id": data.document_id,
                "version": data.expected_version,
                "content_sha256": data.expected_content_sha256,
                "reservation_digest": data.expected_reservation_digest,
                "reservation_snapshot_digest": delivery_context["reservation_snapshot_digest"],
            },
            "producer_source": {
                "kind": execution_source["kind"],
                "key": execution_source["key"],
                "revision": execution_source["revision"],
                "intent_digest": data.expected_intent_digest,
                "state_sha256": data.expected_source_state_sha256,
            },
            "priority": delivery_context["intent"],
            "fulfillment_before_digest": delivery_context["fulfillment_before_digest"],
            "order_ref": common_order_ref,
            "container": common_container,
            "allocations": target_rows,
            "canonical_request": data.model_dump(mode="json"),
            "operation_request_hash": request_hash,
            "evidence": data.evidence,
        }
        sealed, sealed_digest, public_snapshot = seal_binding_snapshot(snapshot)
        return ({
            "binding_revision_id": str(data.operation_id),
            "binding_snapshot": public_snapshot,
        }, None, {
            "binding_key": snapshot["binding_key"],
            "execution_id": str(data.execution_id),
            "snapshot": sealed,
            "digest": sealed_digest,
            "task_ids": [task.id for task, _, _ in tasks],
        })

    async def _ship(self, session, warehouse, data, actor, controls):
        if data.source.startswith("assembly:"):
            reject("Комплект сборки не является отгрузкой клиенту")
        bound_picks = (await session.scalars(select(PickedAllocation).where(
            PickedAllocation.id.in_([line.pick_id for line in data.lines]),
        ).order_by(PickedAllocation.id).with_for_update().execution_options(populate_existing=True))).all()
        binding_ids = {pick.binding_revision_id for pick in bound_picks if pick.binding_revision_id is not None}
        if binding_ids:
            if len(binding_ids) != 1 or len(bound_picks) != len(data.lines) \
                    or any(pick.binding_revision_id is None for pick in bound_picks):
                reject("Отгрузка не может смешивать связанные и обычные подборы")
            from modules.wms.physical_delivery import validate_binding_row, validate_bound_shipment

            binding = await session.get(DeliveryBindingRevision, next(iter(binding_ids)))
            operation = await session.get(PhysicalOperation, binding.operation_pk) if binding is not None else None
            if (binding is None or operation is None or binding.organization_id != warehouse.organization_id
                    or binding.warehouse_id != warehouse.warehouse_id):
                reject("Исходная привязка отгружаемых подборов повреждена", 409)
            validate_binding_row(binding, operation)
            await validate_bound_shipment(session, binding, data)
        if data.correction_of:
            prior = await session.scalar(select(ExchangeJob).where(ExchangeJob.organization_id == warehouse.organization_id,
                ExchangeJob.document_ref == data.correction_of, ExchangeJob.status == "done"))
            if warehouse.onec_ref is None:
                reject("Исправления отгрузок native-склада пока не поддерживаются", 409)
            if prior is None or prior.payload.get("kind") != "ship":
                reject("Исправление требует подтверждённую исходную отгрузку 1С")
        if len({row.pick_id for row in data.lines}) != len(data.lines):
            reject("Строка отгрузки повторяется", 422)
        outgoing, dependencies, qualified = [], set(), []
        for line in data.lines:
            picked = await session.get(PickedAllocation, line.pick_id)
            hold = await session.get(PhysicalHold, picked.hold_id) if picked else None
            if hold is None or hold.warehouse_id != warehouse.warehouse_id or hold.order_ref != data.source:
                reject("Строка отгрузки не относится к этому заказу")
            if picked.canceled or not picked.packed or line.qty > picked.qty - picked.shipped:
                reject("Отгружаемое количество не упаковано или уже отгружено")
            unit = await self._unit(session, warehouse, picked.unit_id)
            if unit.assembly_output_id is not None and line.qty != 1:
                reject("Отгрузка идентичности требует целую единицу", 422)
            destination = await location(session, warehouse.warehouse_id, unit.location_id)
            if destination.kind != "dispatch":
                reject("Заказ ещё не находится в зоне отгрузки")
            reason, lot = await eligible(session, self.reference, warehouse, unit, hold.min_shelf_days)
            if reason:
                reject(f"Отгрузка заблокирована: {reason}")
            await policy(session, warehouse.warehouse_id, unit.sku_code, line.qty)
            await package_check(session, warehouse, unit.sku_code, line.qty, unit.packaging_id, unit.package_state)
            qualified.append((line, picked, hold, unit, lot))
        containers = {hold.container for _, _, hold, _, _ in qualified}
        if data.container_scans != {container: f"BOX-{container}" for container in containers}:
            reject("Перед отгрузкой отсканируйте каждый контейнер этого заказа; чужой или пропущенный контейнер запрещён")
        if data.shipment_mode == "full":
            holds = (await session.scalars(select(PhysicalHold).where(
                PhysicalHold.warehouse_id == warehouse.warehouse_id, PhysicalHold.order_ref == data.source,
                PhysicalHold.qty > PhysicalHold.shipped + PhysicalHold.released))).all()
            if any(hold.qty > hold.picked + hold.released for hold in holds):
                reject("Заказ подобран не полностью. Завершите подбор или явно выберите частичную отгрузку с причиной")
            remaining = (await session.execute(select(PickedAllocation).join(
                PhysicalHold, PickedAllocation.hold_id == PhysicalHold.id).where(
                    PhysicalHold.warehouse_id == warehouse.warehouse_id, PhysicalHold.order_ref == data.source,
                    PickedAllocation.canceled.is_(False), PickedAllocation.qty > PickedAllocation.shipped))).scalars().all()
            if {line.pick_id: line.qty for line in data.lines} != {pick.id: pick.qty - pick.shipped for pick in remaining}:
                reject("Количество полной отгрузки должно точно совпадать со всем остатком заказа. Для части укажите причину")
        from modules.wms.physical_invoice_bridge import create_invoice_act
        invoice_act = await create_invoice_act(session, self.services, self.user, warehouse, data, actor, qualified)
        for line, picked, hold, unit, lot in qualified:
            unit.qty -= line.qty
            unit.reserved -= line.qty
            hold.shipped += line.qty
            picked.shipped += line.qty
            self._deltas.append((unit, -line.qty))
            dependencies.update(unit.external_operations or [])
            outgoing.append({**dimensions(unit), "qty": decimal_text(line.qty), "lot": lot["lot_no"],
                "hold_id": hold.id, "pick_id": picked.id})
        proof = {"container_scans": data.container_scans, "shipment_mode": data.shipment_mode, "partial_reason": data.partial_reason}
        return {"shipped": outgoing, "source": data.source, "invoice_act": invoice_act, **proof}, {"source_ref": data.source,
            "invoice_act": invoice_act,
            **proof,
            "dependencies": sorted(dependencies),
            "accompanying_documents": data.accompanying_documents, "correction_of": data.correction_of, "lines": outgoing}

    async def _task_claim(self, session, warehouse, data, actor, controls, *, prechecked_delivery_context=None):
        task = await session.scalar(select(PhysicalTask).where(
            PhysicalTask.id == str(data.task_id),
            PhysicalTask.warehouse_id == warehouse.warehouse_id,
        ).with_for_update().execution_options(populate_existing=True))
        if task is None or task.warehouse_id != warehouse.warehouse_id:
            reject("Задание не найдено", 404)
        if task.status != "open" and not (task.status == "claimed" and task.assignee == actor):
            reject("Задание уже взято другим сотрудником или завершено")
        if task.binding_revision_id is not None:
            from modules.wms.physical_delivery import binding_snapshot, validate_binding_row

            checked_binding = (prechecked_delivery_context or {}).get("binding")
            if checked_binding is None or checked_binding.id != task.binding_revision_id:
                reject("Привязка задания изменилась; повторите запрос со свежей проверкой", 409)

            binding = await session.get(DeliveryBindingRevision, task.binding_revision_id)
            operation = await session.get(PhysicalOperation, binding.operation_pk) if binding is not None else None
            if binding is None or operation is None:
                reject("Задание привязки доставки повреждено", 409)
            snapshot = validate_binding_row(binding, operation)
            if not any(row["task_id"] == task.id for row in snapshot["allocations"]):
                reject("Задание не входит в исходную привязку", 409)
            from modules.wms.physical_delivery import bound_task_remaining

            target_remaining = await bound_task_remaining(session, task, snapshot)
            if task.qty is None or task.qty != target_remaining or target_remaining <= 0:
                reject("Задание не совпадает с неизменяемым остатком привязки", 409)
        task.status, task.assignee = "claimed", actor
        return {"task_id": task.id, "assignee": actor,
            "binding_revision_id": task.binding_revision_id}, None

    async def _task_complete(self, session, warehouse, data, actor, controls):
        task = await session.get(PhysicalTask, str(data.task_id))
        if task is None or task.warehouse_id != warehouse.warehouse_id:
            reject("Задание не найдено", 404)
        if task.status != "claimed" or task.assignee != actor:
            reject("Сначала возьмите задание в работу")
        if task.kind not in {"replenish", "putaway"}:
            reject("Завершите подбор, обслуживание или пересчёт соответствующей операцией")
        from modules.wms.physical_schemas import Move
        move = Move(operation_id=data.operation_id, warehouse_id=data.warehouse_id,
            kind="move", unit_id=task.unit_id, qty=task.qty, destination_id=task.destination_id, evidence=data.evidence)
        self._causal_delegated_move = move.model_dump(mode="json")
        task.status, task.evidence = "done", data.evidence
        result, exchange = await self._move(session, warehouse, move, actor, controls)
        return {**result, "task_id": task.id}, exchange

    async def _replenish(self, session, warehouse, data, actor, controls):
        return {"tasks": await plan_replenishment(session, self.reference, warehouse, data.sku_code, data.demand)}, None

    async def _count_start(self, session, warehouse, data, actor, controls):
        if len(set(data.location_ids)) != len(data.location_ids):
            reject("Ячейка в пересчёте повторяется", 422)
        places = (await session.scalars(select(PhysicalLocation).where(
            PhysicalLocation.warehouse_id == warehouse.warehouse_id))).all()
        if data.count_kind == "opening":
            if warehouse.mode != "preparing" or warehouse.opening_count_id:
                reject("Начальный пересчёт уже согласован или склад переключён")
            if set(data.location_ids) != {p.location_id for p in places}:
                reject("Начальный пересчёт должен охватывать все адреса склада")
            if await session.scalar(select(StockUnit.id).where(StockUnit.warehouse_id == warehouse.warehouse_id, StockUnit.qty > 0)):
                reject("Начальный остаток нельзя повторно создавать поверх существующего")
        elif warehouse.mode != "erp":
            reject("Циклический пересчёт доступен после переключения")
        for key in sorted(data.location_ids):
            await location(session, warehouse.warehouse_id, key)
        units = (await session.scalars(select(StockUnit).where(StockUnit.location_id.in_(data.location_ids), StockUnit.qty > 0))).all()
        count = PhysicalCount(id=str(uuid4()), warehouse_id=warehouse.warehouse_id,
            kind=data.count_kind, status="open", locations=sorted(data.location_ids), completed_locations=[],
            expected={u.id: {"qty": decimal_text(u.qty), "dimensions": dimensions(u)} for u in units},
            actor=actor, evidence=data.evidence)
        session.add(count)
        await session.flush()
        for key in data.location_ids:
            session.add(CountLocationLock(location_id=key, count_id=count.id))
        for unit in units:
            session.add(PhysicalCountLine(count_id=count.id, identity=unit.id, unit_id=unit.id,
                location_id=unit.location_id, dimensions=dimensions(unit), qty=None))
        await session.flush()
        return {"count_id": count.id, "location_ids": count.locations, "status": count.status}, None

    async def _count(self, session, warehouse, count_id):
        count = await session.get(PhysicalCount, str(count_id))
        if count is None or count.warehouse_id != warehouse.warehouse_id:
            reject("Пересчёт не найден", 404)
        if count.status != "open":
            reject("Пересчёт уже завершён")
        return count

    async def _count_record(self, session, warehouse, data, actor, controls):
        count = await self._count(session, warehouse, data.count_id)
        if data.location_id not in count.locations or data.location_id in count.completed_locations:
            reject("Адрес не включён в пересчёт или уже подтверждён")
        await location(session, warehouse.warehouse_id, data.location_id, unlocked=False)
        await policy(session, warehouse.warehouse_id, data.sku_code, data.qty)
        await batch(session, self.reference, warehouse.organization_id, data.batch_id, data.sku_code)
        if data.unit_id is not None:
            counted = await session.get(StockUnit, str(data.unit_id))
            if counted is not None and counted.assembly_output_id is not None and data.qty != counted.qty:
                reject("Пересчёт не изменяет и не восстанавливает идентичность изделия")
        elif await session.scalar(select(AssemblyOutputIdentity.id).where(
                AssemblyOutputIdentity.organization_id == warehouse.organization_id,
                AssemblyOutputIdentity.sku_code == data.sku_code,
                AssemblyOutputIdentity.batch_id == data.batch_id).limit(1)):
            reject("Найденное изделие требует точную существующую идентичность, не агрегат партии")
        await package_check(session, warehouse, data.sku_code, data.qty or Decimal(0), data.packaging_id, data.package_state)
        dims = data.model_dump(mode="json", exclude={"kind", "operation_id", "warehouse_id", "count_id", "unit_id", "qty"})
        dims["warehouse_id"] = warehouse.warehouse_id
        if data.arrived_at:
            arrived = data.arrived_at
            if not arrived.tzinfo or arrived > datetime.now(ZoneInfo("Europe/Minsk")) or not data.arrival_source:
                reject("Дата поступления требует часового пояса, источника и не может быть будущей")
            dims["arrived_at"] = timestamp_text(arrived)
        elif data.arrival_source:
            reject("Источник поступления требует даты")
        elif data.unit_id and str(data.unit_id) in count.expected:
            original = count.expected[str(data.unit_id)]["dimensions"]
            dims["arrived_at"], dims["arrival_source"] = original["arrived_at"], original["arrival_source"]
        else:
            # This is a count observation, not proof of a historical receipt date.
            dims["arrival_source"] = "opening_count_no_receipt_date"
            dims["arrived_at"] = timestamp_text(count.created_at)
        identity = str(data.unit_id) if data.unit_id else digest(dims)
        if data.unit_id:
            snapshot = count.expected.get(str(data.unit_id))
            if snapshot is None or snapshot["dimensions"] != dims:
                reject("Размерности пересчитываемого остатка отличаются от зафиксированных")
        elif count.kind != "opening":
            # New found stock is always quarantined; QC cannot be invented by a count.
            if data.quality != "quarantine" or data.last_checked or data.next_service:
                reject("Найденный товар сначала направляется на проверку")
        if data.last_checked and data.last_checked > today():
            reject("Будущая дата проверки недопустима")
        if data.next_service and (not data.last_checked or data.next_service <= data.last_checked or not data.inspection_ref):
            reject("Срок обслуживания требует подтверждённой проверки")
        if data.next_service and (not data.service_result or data.service_result.result != "passed"):
            reject("Начальное обслуживание требует результата и фактических измерений")
        line = await session.scalar(select(PhysicalCountLine).where(PhysicalCountLine.count_id == count.id,
            PhysicalCountLine.identity == identity))
        if line is None:
            line = PhysicalCountLine(count_id=count.id, identity=identity, unit_id=str(data.unit_id) if data.unit_id else None,
                location_id=data.location_id, dimensions=dims)
            session.add(line)
        line.qty = data.qty
        await session.flush()
        return {"count_id": count.id, "line_id": line.id, "counted_qty": None if line.qty is None else decimal_text(line.qty)}, None

    async def _count_location_complete(self, session, warehouse, data, actor, controls):
        count = await self._count(session, warehouse, data.count_id)
        if data.location_id not in count.locations:
            reject("Ячейка не включена в пересчёт")
        lines = (await session.scalars(select(PhysicalCountLine).where(PhysicalCountLine.count_id == count.id,
            PhysicalCountLine.location_id == data.location_id))).all()
        if any(line.qty is None for line in lines):
            reject("Непосчитанное количество остаётся неизвестным; заполните все строки")
        if not lines and not data.empty_confirmed:
            reject("Для пустого адреса требуется явное подтверждение")
        if data.empty_confirmed and any(line.qty > 0 for line in lines):
            reject("Адрес содержит положительное посчитанное количество")
        count.completed_locations = sorted(set(count.completed_locations) | {data.location_id})
        return {"count_id": count.id, "completed_locations": count.completed_locations}, None

    async def _count_cancel(self, session, warehouse, data, actor, controls):
        count = await self._count(session, warehouse, data.count_id)
        count.status, count.evidence = "canceled", data.evidence
        await session.execute(delete(CountLocationLock).where(CountLocationLock.count_id == count.id))
        return {"count_id": count.id, "status": count.status}, None

    async def _count_approve(self, session, warehouse, data, actor, controls):
        from modules.wms.physical_capacity import prelock_count_carriers

        count = await self._count(session, warehouse, data.count_id)
        if set(count.completed_locations) != set(count.locations):
            reject("Пересчитаны не все адреса документа")
        lines = (await session.scalars(select(PhysicalCountLine).where(PhysicalCountLine.count_id == count.id))).all()
        if any(line.qty is None for line in lines):
            reject("Пересчитаны не все строки; неизвестное количество не равно нулю")
        if set(count.expected) - {line.unit_id for line in lines if line.unit_id}:
            reject("Каждый ранее учтённый остаток нужно пересчитать, включая явное нулевое количество")
        reviewed = await review_count(session, count)
        if data.review_digest != reviewed["review_digest"]:
            reject("Перед утверждением просмотрите расхождения; результат пересчёта изменился или не был проверен")

        units, changed_carriers = await prelock_count_carriers(
            session, warehouse.warehouse_id, lines,
        )
        prepared = []
        for line in lines:
            await policy(session, warehouse.warehouse_id, line.dimensions["sku_code"], line.qty)
            await package_check(session, warehouse, line.dimensions["sku_code"], line.qty,
                line.dimensions["packaging_id"], line.dimensions["package_state"])
            if line.unit_id:
                unit = units[line.unit_id]
                snapshot = count.expected[line.unit_id]
                if dimensions(unit) != snapshot["dimensions"] or unit.qty != Decimal(snapshot["qty"]):
                    reject("Остаток изменился во время пересчёта")
                if line.qty < unit.reserved:
                    reject("Расхождение затрагивает действующий резерв; сначала разрешите его отдельно")
                if unit.assembly_output_id is not None and line.qty != unit.qty:
                    reject("Пересчёт не изменяет и не восстанавливает идентичность изделия")
                values = None
                delta = line.qty - unit.qty
            else:
                if await session.scalar(select(AssemblyOutputIdentity.id).where(
                        AssemblyOutputIdentity.organization_id == warehouse.organization_id,
                        AssemblyOutputIdentity.sku_code == line.dimensions["sku_code"],
                        AssemblyOutputIdentity.batch_id == line.dimensions["batch_id"]).limit(1)):
                    reject("Пересчёт не создаёт анонимный остаток партии с идентичностями изделий")
                values = dict(line.dimensions)
                for key in ("last_checked", "next_service"):
                    values[key] = date.fromisoformat(values[key]) if values[key] else None
                values["arrived_at"] = datetime.fromisoformat(values["arrived_at"])
                unit = None
                delta = line.qty
            prepared.append((line, unit, values, delta))

        for carrier in changed_carriers.values():
            carrier.content_revision += 1
            carrier.confirmed_content_revision = None

        adjustments = []
        count_basis = []
        for line, unit, values, delta in prepared:
            created_unit = unit is None
            if created_unit:
                unit = StockUnit(id=str(uuid4()), stock_state="on_hand", dispatch_operation_id=None,
                    qty=line.qty, reserved=Decimal(0), **values)
                session.add(unit)
            else:
                unit.qty = line.qty
            count_basis.append({"count_id": count.id, "count_kind": count.kind,
                "count_line_id": line.id, "line_identity": line.identity, "unit_id": unit.id,
                "created_unit": created_unit, "observed_qty": decimal_text(line.qty),
                "observed_dimensions": dimensions(unit), "qty_delta": decimal_text(delta),
                "review_digest": reviewed["review_digest"]})
            if delta:
                if count.kind != "opening":
                    unit.external_operations = sorted(set(unit.external_operations or []) | {str(data.operation_id)})
                self._deltas.append((unit, delta))
                adjustments.append({**dimensions(unit), "qty_delta": decimal_text(delta)})
        self._causal_count_basis = count_basis
        count.status, count.approved_by, count.approved_at = "approved", actor, datetime.now(ZoneInfo("Europe/Minsk"))
        count.evidence = data.evidence
        if count.kind == "opening":
            warehouse.opening_count_id = count.id
        await session.execute(delete(CountLocationLock).where(CountLocationLock.count_id == count.id))
        await session.flush()
        exchange = {"count_id": count.id, "evidence": data.evidence, "lines": adjustments} if adjustments and count.kind != "opening" else None
        return {"count_id": count.id, "status": count.status, "opening": count.kind == "opening",
            "adjustments": adjustments}, exchange

    async def _cutover(self, session, warehouse, data, actor, controls):
        if warehouse.mode != "preparing" or warehouse.opening_count_id != str(data.opening_count_id):
            reject("Переключение требует согласованного начального пересчёта")
        if not data.source_matches_working_base or not data.open_orders_reviewed:
            if warehouse.onec_ref is None:
                reject("Не подтверждены ERP-снимок начальных остатков и перенос открытых заказов")
            reject("Не подтверждены рабочая база 1С и перенос открытых заказов")
        count = await session.get(PhysicalCount, warehouse.opening_count_id)
        places = (await session.scalars(select(PhysicalLocation).where(PhysicalLocation.warehouse_id == warehouse.warehouse_id))).all()
        if count.status != "approved" or set(count.locations) != {row.location_id for row in places}:
            reject("После пересчёта изменился состав адресов")
        required = {"receiving", "storage", "pick", "packing", "dispatch", "returns", "quarantine"}
        if not required.issubset({row.kind for row in places}):
            reject("Подготовьте раздельные зоны полного складского цикла")
        if await session.scalar(select(CountLocationLock.location_id).join(PhysicalLocation).where(
            PhysicalLocation.warehouse_id == warehouse.warehouse_id)):
            reject("На складе остаётся незавершённый пересчёт")
        directory = await session.get(WarehouseDirectory, warehouse.warehouse_id)
        legacy = (await session.scalars(select(ReservationVersion).where(
            ReservationVersion.organization_id == warehouse.organization_id,
            ReservationVersion.warehouse == directory.name).order_by(ReservationVersion.version))).all()
        latest = {row.source: row for row in legacy}
        old_active = {key: row for key, row in latest.items() if row.qty > 0}
        if set(data.transferred_reserves) != set(old_active):
            reject("Список перенесённых резервов не совпадает с действующим учётом ERP")
        holds = (await session.scalars(select(PhysicalHold).where(PhysicalHold.warehouse_id == warehouse.warehouse_id))).all()
        for key, old in old_active.items():
            actual = sum((h.qty - h.shipped - h.released for h in holds if h.source == key), Decimal(0))
            if actual != old.qty:
                reject("Не все активные резервы распределены по партиям и адресам")
        for unit in (await session.scalars(select(StockUnit).where(StockUnit.warehouse_id == warehouse.warehouse_id,
            StockUnit.qty > 0))).all():
            await policy(session, warehouse.warehouse_id, unit.sku_code, unit.qty)
            await batch(session, self.reference, warehouse.organization_id, unit.batch_id, unit.sku_code)
        warehouse.mode, warehouse.cutover_at = "erp", datetime.now(ZoneInfo("Europe/Minsk"))
        warehouse.cutover_evidence = {**data.model_dump(mode="json"), "actor": actor}
        return {"source": "erp", "opening_count_id": count.id}, None


async def pending_move_qty(session, unit_id):
    tasks = (await session.scalars(select(PhysicalTask).where(PhysicalTask.unit_id == unit_id,
        PhysicalTask.kind.in_(["replenish", "putaway"]), PhysicalTask.status.in_(["open", "claimed"])))).all()
    return sum((task.qty for task in tasks), Decimal(0))


async def allocate(session, reference, warehouse, sku, qty, source, container, characteristic="", purpose="", shelf_days=0, due_at=None, order_ref=None, task_kind="pick"):
    """The same allocator is used by sales and TSD; it cannot choose a warehouse."""
    if task_kind not in {"pick", "assemblykit"}:
        raise ValueError("Unsupported physical reservation task kind")
    await policy(session, warehouse.warehouse_id, sku, qty)
    if warehouse.mode == "preparing" and not warehouse.opening_count_id:
        reject("Резерв требует согласованного начального остатка")
    previous = await session.scalar(select(PhysicalHold.id).join(StockUnit, PhysicalHold.unit_id == StockUnit.id).where(
        PhysicalHold.warehouse_id == warehouse.warehouse_id, PhysicalHold.source == source, StockUnit.sku_code == sku))
    if previous:
        reject("Строка заказа уже распределена; используйте идентификатор исходной операции")
    other = await session.scalar(select(PhysicalHold.id).where(PhysicalHold.warehouse_id == warehouse.warehouse_id,
        PhysicalHold.container == container, PhysicalHold.order_ref != (order_ref or source),
        PhysicalHold.qty > PhysicalHold.shipped + PhysicalHold.consumed + PhysicalHold.released))
    if other:
        reject("Контейнер уже назначен другому заказу")
    candidates = (await session.scalars(select(StockUnit).join(PhysicalLocation, StockUnit.location_id == PhysicalLocation.location_id)
        .where(StockUnit.warehouse_id == warehouse.warehouse_id, StockUnit.sku_code == sku,
            StockUnit.characteristic == characteristic, StockUnit.purpose == purpose,
            StockUnit.qty > StockUnit.reserved, PhysicalLocation.kind.in_(["storage", "pick"]))
        .order_by(StockUnit.id).with_for_update().execution_options(populate_existing=True))).all()
    usable = []
    for unit in candidates:
        if await session.get(CountLocationLock, unit.location_id):
            continue
        reason, lot = await eligible(session, reference, warehouse, unit, shelf_days)
        free = unit.qty - unit.reserved - await pending_move_qty(session, unit.id)
        if reason or free <= 0:
            continue
        usable.append((unit, lot, free))
    usable.sort(key=lambda item: (item[1].get("expiry_date") or "9999-12-31",
        item[0].arrived_at.isoformat(), item[0].id))
    if sum((item[2] for item in usable), Decimal(0)) < qty:
        reject("Недостаточно доступного количества с допустимой партией и сроками")
    remaining, holds = qty, []
    for unit, _, free in usable:
        take = min(remaining, free)
        if unit.assembly_output_id is not None and take != 1:
            continue
        if not take:
            break
        if unit.package_state == "sealed":
            pack = await session.get(Packaging, unit.packaging_id)
            take -= take % pack.factor
            if not take:
                continue
        unit.reserved += take
        hold = PhysicalHold(warehouse_id=warehouse.warehouse_id, source=source, order_ref=order_ref or source, unit_id=unit.id,
            qty=take, picked=Decimal(0), shipped=Decimal(0), released=Decimal(0),
            min_shelf_days=shelf_days, container=container)
        session.add(hold)
        await session.flush()
        session.add(PhysicalTask(id=str(uuid4()), warehouse_id=warehouse.warehouse_id, kind=task_kind,
            status="open", source=source, unit_id=unit.id, hold_id=hold.id, qty=take,
            due_at=due_at, assignee="", evidence=""))
        holds.append(hold)
        remaining -= take
    if remaining:
        reject("Для штучного подбора сначала вскройте нужную коробку")
    await session.flush()
    return holds


async def finish_pick_tasks(session, hold):
    from modules.wms.physical_delivery import bound_task_remaining

    tasks = (await session.scalars(select(PhysicalTask).where(
        PhysicalTask.hold_id == hold.id,
        PhysicalTask.kind == "pick",
        PhysicalTask.status.in_(["open", "claimed", "done"]),
    ).order_by(PhysicalTask.id).with_for_update().execution_options(populate_existing=True))).all()
    remaining = hold.qty - hold.picked - hold.released
    bound = [task for task in tasks if task.binding_revision_id is not None]
    unbound = [task for task in tasks if task.binding_revision_id is None
        and task.status in {"open", "claimed"}]
    bound_remaining = Decimal("0")
    for task in bound:
        target_remaining = await bound_task_remaining(session, task)
        if task.status == "done":
            if target_remaining != 0 or task.qty not in (Decimal("0"), 0):
                reject("Завершённое связанное задание не совпадает с его целью", 409)
            continue
        task.qty = target_remaining
        if target_remaining == 0:
            task.status = "done"
        else:
            bound_remaining += target_remaining
    if bound_remaining > remaining:
        reject("Обычный подбор не может занять количество выделенной доставки", 409)
    if len(unbound) > 1:
        reject("Нельзя распределить остаток резерва между несколькими обычными заданиями", 409)
    if unbound:
        unbound[0].qty = remaining - bound_remaining
        if unbound[0].qty == 0:
            unbound[0].status = "done"


async def plan_replenishment(session, reference, warehouse, sku=None, demand=None):
    demand = demand or {}
    owned = (await session.scalars(select(StockUnit).where(StockUnit.warehouse_id == warehouse.warehouse_id))).all()
    present_ids = {unit.id for unit in _on_hand_units(owned)}
    query = select(PickRule).where(PickRule.warehouse_id == warehouse.warehouse_id)
    if sku:
        query = query.where(PickRule.sku_code == sku)
    rules = (await session.scalars(query.order_by(PickRule.id))).all()
    output = []
    for rule in rules:
        if await session.get(CountLocationLock, rule.location_id):
            continue
        destination = await location(session, warehouse.warehouse_id, rule.location_id)
        units = (await session.scalars(select(StockUnit).where(StockUnit.location_id == rule.location_id,
            StockUnit.sku_code == rule.sku_code, StockUnit.qty > 0))).all()
        free = Decimal(0)
        occupied = sum((u.qty for u in units), Decimal(0))
        for unit in units:
            reason, _ = await eligible(session, reference, warehouse, unit)
            if not reason:
                free += unit.qty - unit.reserved
        pending = (await session.scalars(select(PhysicalTask).where(PhysicalTask.destination_id == rule.location_id,
            PhysicalTask.kind == "replenish", PhysicalTask.status.in_(["open", "claimed"])))).all()
        pending = [task for task in pending if task.unit_id in present_ids]
        incoming = sum((task.qty for task in pending), Decimal(0))
        if free + incoming >= rule.min_qty and free + incoming >= demand.get(rule.sku_code, Decimal(0)):
            continue
        if destination.capacity_qty is None or destination.capacity_sku != rule.sku_code:
            reject("Для пополнения подтвердите вместимость места отбора")
        needed = max(Decimal(0), min(rule.max_qty, destination.capacity_qty) - occupied - incoming)
        candidates = (await session.scalars(select(StockUnit).join(PhysicalLocation, StockUnit.location_id == PhysicalLocation.location_id)
            .where(StockUnit.warehouse_id == warehouse.warehouse_id, StockUnit.sku_code == rule.sku_code,
                PhysicalLocation.kind == "storage", StockUnit.qty > StockUnit.reserved))).all()
        usable = []
        for unit in candidates:
            if await session.get(CountLocationLock, unit.location_id):
                continue
            reason, lot = await eligible(session, reference, warehouse, unit)
            available = unit.qty - unit.reserved - await pending_move_qty(session, unit.id)
            if not reason and available > 0:
                usable.append((unit, lot, available))
        usable.sort(key=lambda item: (item[1].get("expiry_date") or "9999-12-31", item[0].arrived_at.isoformat(), item[0].id))
        for unit, _, available in usable:
            qty = min(needed, available)
            if unit.package_state == "sealed":
                pack = await session.get(Packaging, unit.packaging_id)
                qty -= qty % pack.factor
            if qty <= 0:
                continue
            task = PhysicalTask(id=str(uuid4()), warehouse_id=warehouse.warehouse_id, kind="replenish", status="open",
                source=f"minmax:{rule.id}", unit_id=unit.id, qty=qty, destination_id=rule.location_id, assignee="", evidence="")
            session.add(task)
            await session.flush()
            output.append({"id": task.id, "sku_code": unit.sku_code, "qty": decimal_text(qty),
                "destination_id": rule.location_id, "unit_id": unit.id})
            needed -= qty
            if needed <= 0:
                break
    return output


async def refresh_tasks(session, reference, warehouse):
    """Queue quality/service/putaway work without granting eligibility or moving stock."""
    units = (await session.scalars(select(StockUnit).where(StockUnit.warehouse_id == warehouse.warehouse_id))).all()
    units = _on_hand_units(units)
    tasks = (await session.scalars(select(PhysicalTask).where(PhysicalTask.warehouse_id == warehouse.warehouse_id,
        PhysicalTask.status.in_(["open", "claimed"]), PhysicalTask.unit_id.is_not(None)))).all()
    by_key = {(task.unit_id, task.kind): task for task in tasks if task.kind not in {"pick", "assemblykit"}}
    places = (await session.scalars(select(PhysicalLocation).where(PhysicalLocation.warehouse_id == warehouse.warehouse_id,
        PhysicalLocation.kind == "storage").order_by(PhysicalLocation.code))).all()
    for unit in units:
        free = unit.qty - unit.reserved
        for task in [t for t in tasks if t.unit_id == unit.id and t.kind not in {"pick", "assemblykit"}]:
            task.qty = min(task.qty, max(Decimal(0), free))
            if task.qty == 0:
                task.status = "done"
        if free <= 0 or await session.get(CountLocationLock, unit.location_id):
            continue
        place = await session.get(PhysicalLocation, unit.location_id)
        reason, _ = await eligible(session, reference, warehouse, unit)
        kind, destination = None, None
        if reason in {"maintenance_due", "maintenance_unknown"}:
            kind = "maintenance"
        elif unit.quality == "quarantine":
            kind = "quality"
        elif not reason and place.kind in {"receiving", "returns", "service", "quarantine"}:
            candidates = [p for p in places if p.capacity_sku in {None, unit.sku_code}]
            if candidates:
                kind, destination = "putaway", candidates[0].location_id
        if kind and (unit.id, kind) not in by_key:
            task = PhysicalTask(id=str(uuid4()), warehouse_id=warehouse.warehouse_id, kind=kind,
                status="open", source=f"unit:{unit.id}", unit_id=unit.id, qty=free,
                destination_id=destination, assignee="", evidence="")
            session.add(task)
    await session.flush()


async def tick_physical_tasks(session, services):
    gateway = getattr(services, "stock", None)
    if gateway is None or not hasattr(gateway, "batch_by_id"):
        return
    warehouses = (await session.scalars(select(WarehouseControl).where(WarehouseControl.mode == "erp")
        .order_by(WarehouseControl.organization_id, WarehouseControl.warehouse_id))).all()
    for row in warehouses:
        await services.accounting.lock_event_organization(session, row.organization_id)
        row = await control(session, row.warehouse_id, row.organization_id)
        await refresh_tasks(session, gateway, row)
        await plan_replenishment(session, gateway, row)
