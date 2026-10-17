"""Durable process anchors and exact physical assembly evidence consumers."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select

from core.domain.models import User
from core.services.auth import (
    CurrentUser,
    EffectiveIdentityLookupError,
    has_permission,
    resolve_effective_oidc_user,
)
from modules.wms.assembly_workplace import read_assembly_blind_count, read_assembly_warehouse
from modules.wms.physical_models import AssemblyExecution, AssemblyProcessRun
from modules.wms.physical_schemas import (
    AssemblyRunConfirm,
    AssemblyRunH11Projection,
    AssemblyRunPrepare,
    AssemblyRunManufacturingPolicyResolved,
    AssemblyRunSerialPolicyResolved,
    AssemblyRunExpiryPolicyResolved,
)
from modules.wms.physical_stock import PhysicalStock

MANAGER_ROLES = frozenset({"admin", "director"})
FORECAST_PERMISSION = "production.assembly.forecast"
PROCESS_SCHEMA_VERSION = 1


def _canonical(value):
    try:
        return json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise HTTPException(409, "Assembly process history contains invalid JSON") from exc


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _positive_int(value):
    return type(value) is int and value > 0


def _valid_uuid(value):
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except (TypeError, ValueError):
        return False


def _nonempty(value, maximum=200):
    return isinstance(value, str) and 0 < len(value.strip()) <= maximum


def _conflict(detail="Assembly process-run state conflicts with the request"):
    raise HTTPException(409, detail)


def _source_dto(value, organization_id, order_id):
    if (not isinstance(value, dict)
            or set(value) != {"organization_id", "order_id", "status", "refs", "composition", "current_stage", "issues"}
            or type(value["organization_id"]) is not int or value["organization_id"] != organization_id
            or type(value["order_id"]) is not int or value["order_id"] != order_id
            or not isinstance(value["status"], str)
            or value["status"] not in {
                "validated", "missing_specification", "unapproved", "stale_approval",
                "source_changed", "invalid_source",
            }
            or not isinstance(value["refs"], dict)
            or set(value["refs"]) != {"specification", "nomination", "approval", "bom"}
            or not isinstance(value["issues"], list)
            or len(value["issues"]) > 20
            or any(not isinstance(issue, str) or len(issue) > 128 for issue in value["issues"])
            or (value["current_stage"] is not None
                and (not isinstance(value["current_stage"], str) or len(value["current_stage"]) > 32))):
        _conflict("Production source gateway returned an invalid DTO")
    refs = value["refs"]
    for name in ("specification", "nomination", "approval", "bom"):
        ref = refs[name]
        if ref is None:
            continue
        expected_keys = {"id", "revision", "digest"} if name == "specification" else (
            {"id", "version"} if name == "bom" else {"id", "revision"}
        )
        if (not isinstance(ref, dict) or set(ref) != expected_keys
                or not _positive_int(ref.get("id"))):
            _conflict("Production source reference is malformed")
        if name in {"specification", "nomination", "approval"} and not _positive_int(ref.get("revision")):
            _conflict("Production source revision reference is malformed")
        if name == "specification" and (
            not isinstance(ref.get("digest"), str)
            or re.fullmatch(r"[0-9a-f]{64}", ref["digest"]) is None
        ):
            _conflict("Production specification digest reference is malformed")
        if name == "bom" and (
            not isinstance(ref.get("version"), str) or not 0 < len(ref["version"]) <= 64
        ):
            _conflict("Production BOM reference is malformed")
    if value["status"] != "validated":
        if value["composition"] is not None:
            _conflict("Unvalidated production source must not contain a composition digest")
        return value
    composition = value["composition"]
    if (not isinstance(composition, dict)
            or set(composition) != {"schema_version", "digest", "snapshot"}
            or type(composition["schema_version"]) is not int or composition["schema_version"] != 1
            or not isinstance(composition["digest"], str)
            or len(composition["digest"]) != 64
            or not isinstance(composition["snapshot"], dict)
            or _digest(composition["snapshot"]) != composition["digest"]):
        _conflict("Production source composition digest is invalid")
    snapshot = composition["snapshot"]
    if (set(snapshot) != {
            "organization_id", "order", "legacy_bom", "legacy_bom_items", "exact_lines",
            "output", "instructions_version", "first_unit_qc",
        }
            or type(snapshot["organization_id"]) is not int
            or snapshot["organization_id"] != organization_id
            or not isinstance(snapshot["order"], dict)
            or set(snapshot["order"]) != {"order_id", "number", "product", "quantity", "created_at"}
            or not _positive_int(snapshot["order"]["order_id"])
            or snapshot["order"]["order_id"] != order_id
            or type(snapshot["order"]["quantity"]) is not int or snapshot["order"]["quantity"] <= 0
            or not isinstance(snapshot["order"]["number"], str)
            or len(snapshot["order"]["number"]) > 64
            or not isinstance(snapshot["order"]["product"], str)
            or not 0 < len(snapshot["order"]["product"]) <= 255
            or (snapshot["order"]["created_at"] is not None
                and (not isinstance(snapshot["order"]["created_at"], str)
                     or len(snapshot["order"]["created_at"]) > 64))
            or not isinstance(snapshot["legacy_bom"], dict)
            or set(snapshot["legacy_bom"]) != {"id", "product", "version", "status"}
            or not _positive_int(snapshot["legacy_bom"]["id"])
            or not all(isinstance(snapshot["legacy_bom"][key], str)
                       for key in ("product", "version", "status"))
            or not 0 < len(snapshot["legacy_bom"]["product"]) <= 255
            or not 0 < len(snapshot["legacy_bom"]["version"]) <= 16
            or snapshot["legacy_bom"]["status"] != "approved"
            or not isinstance(snapshot["legacy_bom_items"], list)
            or not 1 <= len(snapshot["legacy_bom_items"]) <= 500
            or not isinstance(snapshot["exact_lines"], list)
            or not 1 <= len(snapshot["exact_lines"]) <= 500
            or not isinstance(snapshot["output"], dict)
            or set(snapshot["output"]) != {"sku_code", "base_unit", "precision", "serialized"}
            or not isinstance(snapshot["output"]["sku_code"], str)
            or not 0 < len(snapshot["output"]["sku_code"]) <= 64
            or not isinstance(snapshot["output"]["base_unit"], str)
            or not 0 < len(snapshot["output"]["base_unit"]) <= 16
            or type(snapshot["output"]["precision"]) is not int
            or not 0 <= snapshot["output"]["precision"] <= 6
            or type(snapshot["output"]["serialized"]) is not bool
            or not isinstance(snapshot["instructions_version"], str)
            or not 0 < len(snapshot["instructions_version"]) <= 64
            or not isinstance(snapshot["first_unit_qc"], str)
            or not 10 <= len(snapshot["first_unit_qc"]) <= 1000):
        _conflict("Production source composition is outside the allowlisted projection")
    item_ids = []
    for item in snapshot["legacy_bom_items"]:
        if (not isinstance(item, dict)
                or set(item) != {"id", "component", "norm_qty_source", "unit"}
                or not _positive_int(item["id"])
                or not all(isinstance(item[key], str) for key in ("component", "norm_qty_source", "unit"))):
            _conflict("Production BOM source projection is malformed")
        if (len(item["component"]) > 255 or len(item["norm_qty_source"]) > 64
                or len(item["unit"]) > 32):
            _conflict("Production BOM source projection exceeds its bounds")
        item_ids.append(item["id"])
    if item_ids != sorted(item_ids) or len(item_ids) != len(set(item_ids)):
        _conflict("Production BOM source lines are not unique and ordered")
    line_ids = []
    for line in snapshot["exact_lines"]:
        if (not isinstance(line, dict)
                or set(line) != {
                    "legacy_bom_item_id", "sku_code", "base_unit", "precision", "quantity_per_output",
                }
                or not _positive_int(line["legacy_bom_item_id"])
                or not isinstance(line["sku_code"], str)
                or not 0 < len(line["sku_code"]) <= 64
                or not isinstance(line["base_unit"], str)
                or not 0 < len(line["base_unit"]) <= 16
                or type(line["precision"]) is not int or not 0 <= line["precision"] <= 6
                or not isinstance(line["quantity_per_output"], str)
                or re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", line["quantity_per_output"]) is None):
            _conflict("Production exact assembly line is malformed")
        try:
            line_quantity = Decimal(line["quantity_per_output"])
        except InvalidOperation as exc:
            raise HTTPException(409, "Production exact assembly line quantity is invalid") from exc
        try:
            quantized = line_quantity.quantize(Decimal(1).scaleb(-line["precision"]))
        except InvalidOperation as exc:
            raise HTTPException(409, "Production exact assembly line precision is invalid") from exc
        if (not line_quantity.is_finite() or line_quantity <= 0 or line_quantity != quantized
                or len(quantized.as_tuple().digits) > 20):
            _conflict("Production exact assembly line precision is invalid")
        line_ids.append(line["legacy_bom_item_id"])
    if line_ids != sorted(line_ids) or line_ids != item_ids:
        _conflict("Production exact lines do not cover the approved BOM exactly")
    return value





def _refs_equal(left, right):
    return _canonical(left) == _canonical(right)


def _receipt_for(row):
    result = row.result
    from modules.wms.physical_schemas import AssemblyRunSourceRefs

    created_at = row.created_at
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    if (not isinstance(result, dict)
            or set(result) != {
                "schema_version", "organization_id", "order_id", "request_id", "run_id",
                "revision_id", "revision", "kind", "actor_user_id", "actor_sub", "created_at",
                "source_refs", "composition_schema_version", "composition_digest",
                "prepared_revision_id", "prepared_digest", "metadata_outcome",
            }
            or type(result["schema_version"]) is not int or result["schema_version"] != 1
            or type(result["organization_id"]) is not int
            or result["organization_id"] != row.organization_id
            or type(result["order_id"]) is not int
            or result["order_id"] != row.order_id
            or result["request_id"] != row.request_id
            or result["run_id"] != row.run_id
            or result["revision_id"] != row.id
            or type(result["revision"]) is not int or result["revision"] != row.revision
            or result["kind"] != row.kind
            or result["actor_user_id"] != row.actor_user_id
            or result["actor_sub"] != row.actor_sub
            or result["created_at"] != created_at.astimezone(timezone.utc).isoformat()
            or type(result["composition_schema_version"]) is not int
            or result["composition_schema_version"] != 1
            or result["metadata_outcome"] != row.kind):
        _conflict("Assembly process receipt integrity failed")
    if (not isinstance(result["source_refs"], dict)
            or not isinstance(result["composition_digest"], str)
            or re.fullmatch(r"[0-9a-f]{64}", result["composition_digest"]) is None):
        _conflict("Assembly process receipt source integrity failed")
    try:
        refs = AssemblyRunSourceRefs.model_validate(result["source_refs"]).model_dump(mode="json")
    except Exception as exc:
        raise HTTPException(409, "Assembly process receipt references are malformed") from exc
    if refs != result["source_refs"]:
        _conflict("Assembly process receipt references are not canonical")
    if row.kind == "prepared":
        if (row.prepared_revision_id is not None
                or result["prepared_revision_id"] != row.id
                or result["prepared_digest"] != row.content_digest):
            _conflict("Prepared receipt integrity failed")
    elif (row.kind != "confirmed"
            or not _valid_uuid(row.prepared_revision_id)
            or result["prepared_revision_id"] != row.prepared_revision_id
                or not isinstance(result["prepared_digest"], str)
                or re.fullmatch(r"[0-9a-f]{64}", result["prepared_digest"]) is None):
        _conflict("Confirmed receipt integrity failed")
    return result


def _validate_row(row):
    if (not _valid_uuid(row.id) or not _positive_int(row.organization_id)
            or not _positive_int(row.order_id) or not _positive_int(row.revision)
            or not _positive_int(row.actor_user_id)
            or not _valid_uuid(row.run_id) or not _nonempty(row.actor_sub)
            or row.kind not in {"prepared", "confirmed"}
            or type(row.command_schema_version) is not int
            or row.command_schema_version != PROCESS_SCHEMA_VERSION
            or not isinstance(row.request_id, str)
            or not isinstance(row.command, dict)
            or not isinstance(row.content, dict)
            or not isinstance(row.command_hash, str) or len(row.command_hash) != 64
            or not isinstance(row.content_digest, str) or len(row.content_digest) != 64
            or row.created_at is None):
        _conflict("Assembly process revision is malformed")
    try:
        canonical_request = str(UUID(row.request_id))
    except (TypeError, ValueError) as exc:
        raise HTTPException(409, "Assembly process request UUID is malformed") from exc
    if canonical_request != row.request_id or row.command.get("request_id") != row.request_id:
        _conflict("Assembly process request identity is inconsistent")
    if _digest(row.command) != row.command_hash or _digest(row.content) != row.content_digest:
        _conflict("Assembly process command or content digest is inconsistent")
    receipt = _receipt_for(row)
    if row.kind == "prepared":
        if (row.command.get("kind") != "prepare"
                or row.content.get("schema_version") != 1
                or row.content.get("source_refs") != receipt["source_refs"]
                or row.content.get("composition_digest") != receipt["composition_digest"]
                or row.command.get("expected_source_refs") != receipt["source_refs"]
                or row.command.get("expected_composition_schema_version") != 1
                or row.command.get("expected_composition_digest") != receipt["composition_digest"]
                or row.command.get("designated_first_output_key") != row.content.get("designated_first_output_key")
                or row.command.get("expected_revision") != row.revision - 1):
            _conflict("Prepared process content is inconsistent")
        _validate_prepared_content(row)
    else:
        if (row.command.get("kind") != "confirm"
                or set(row.content) != {"prepared_revision_id", "prepared_digest", "evidence"}
                or row.content.get("prepared_revision_id") != row.prepared_revision_id
                or row.content.get("prepared_digest") != receipt["prepared_digest"]
                or row.command.get("expected_revision") != row.revision - 1
                or row.command.get("prepared_revision_id") != row.prepared_revision_id
                or row.command.get("expected_prepared_digest") != receipt["prepared_digest"]
                or row.command.get("expected_source_refs") != receipt["source_refs"]
                or row.command.get("expected_composition_schema_version") != 1
                or row.command.get("expected_composition_digest") != receipt["composition_digest"]
                or row.command.get("evidence") != row.content.get("evidence")):
            _conflict("Confirmed process content is inconsistent")
        raw = dict(row.command)
        raw.pop("kind", None)
        try:
            command = AssemblyRunConfirm.model_validate(raw)
        except Exception as exc:
            raise HTTPException(409, "Confirmed process command failed strict validation") from exc
        if command.model_dump(mode="json") != raw:
            _conflict("Confirmed process command is not canonical")
    return receipt


def _validate_chain(rows, organization_id, order_id):
    if not rows:
        return None, {}
    by_id = {}
    run_id = None
    previous = None
    for index, row in enumerate(rows, start=1):
        _validate_row(row)
        if (row.organization_id != organization_id or row.order_id != order_id
                or row.revision != index or row.id in by_id):
            _conflict("Assembly process revision scope or sequence is inconsistent")
        if run_id is None:
            run_id = row.run_id
            if row.revision != 1 or row.previous_revision_id is not None or row.kind != "prepared":
                _conflict("Assembly process chain has no valid first prepared revision")
        elif row.run_id != run_id or row.previous_revision_id != previous.id:
            _conflict("Assembly process chain predecessor is inconsistent")
        if row.kind == "prepared":
            if row.prepared_revision_id is not None:
                _conflict("Prepared revision unexpectedly references another revision")
            prior_prepared = _head_prepared(previous, by_id)
            if (prior_prepared is not None
                    and row.content["designated_first_output_key"] != prior_prepared.content["designated_first_output_key"]
                    and _qualification_digest(row.content) == _qualification_digest(prior_prepared.content)):
                _conflict("Historical first-key change lacks a changed immutable qualification")
        else:
            prepared = by_id.get(row.prepared_revision_id)
            if (prepared is None or prepared is not previous or prepared.kind != "prepared"
                    or prepared.content_digest != row.result.get("prepared_digest")
                    or row.content.get("prepared_revision_id") != prepared.id
                    or row.result["source_refs"] != prepared.content["source_refs"]
                    or row.result["composition_schema_version"] != prepared.content["composition_schema_version"]
                    or row.result["composition_digest"] != prepared.content["composition_digest"]
                    or row.actor_user_id != prepared.content["responsibility"]["qc_responsible_user_id"]
                    or row.actor_sub != prepared.content["responsibility"]["qc_responsible_sub"]):
                _conflict("Confirmed revision does not reference its immutable prepared content")
        by_id[row.id] = row
        previous = row
    return previous, by_id


async def _fresh_actor(session, core, organization_id, user, *, expected_actor=None):
    accounting = getattr(core.services, "accounting", None)
    if accounting is None:
        raise HTTPException(503, "Organization service is unavailable")
    # Token identity is a value captured before the first membership wait;
    # external caller carriers may be mutable and must not renew their claims.
    seed = (user.username, tuple(user.roles), user.keycloak_user_id, user.local_status,
        user.deal_visibility, user.crm_restricted, user.issued_at, user.local_user_id)

    def principal():
        return CurrentUser(seed[0], list(seed[1]), keycloak_user_id=seed[2], local_status=None,
            deal_visibility=seed[4], crm_restricted=seed[5], issued_at=seed[6], local_user_id=seed[7])

    subject = seed[2]
    if not _nonempty(subject) or subject != subject.strip():
        raise HTTPException(403, "Managed OIDC identity required for assembly process metadata")
    # The organization lock precedes every User lock, including receipt recovery.
    if await accounting.source_member(session, organization_id, principal()) != subject:
        raise HTTPException(403, "Organization membership does not match the managed subject")
    try:
        fresh = await resolve_effective_oidc_user(principal(), session)
    except EffectiveIdentityLookupError as exc:
        raise HTTPException(503, "Current identity cannot be verified") from exc
    managed = await session.scalar(select(User).where(User.keycloak_user_id == subject)
        .execution_options(populate_existing=True))
    if (managed is None or managed.status != "active" or not _positive_int(managed.id)
            or not managed.role or managed.role not in getattr(fresh, "roles", ())
            or getattr(fresh, "keycloak_user_id", None) != subject
            or getattr(fresh, "local_status", None) != "active"
            or (expected_actor is not None and expected_actor != (managed.id, subject))):
        raise HTTPException(403, "Active original managed identity with its current role is required")
    managed_uid = managed.id
    if await accounting.source_member(session, organization_id, fresh) != subject:
        raise HTTPException(403, "Organization membership changed during authorization")
    # AuditLog revocation or role mutation can occur during the LAST authority
    # wait. No successful result is published from the earlier resolved token.
    try:
        fresh = await resolve_effective_oidc_user(principal(), session)
    except EffectiveIdentityLookupError as exc:
        raise HTTPException(503, "Current identity cannot be verified") from exc
    if (fresh.local_user_id != managed_uid or fresh.keycloak_user_id != subject
            or fresh.issued_at != seed[6] or fresh.local_status != "active"
            or managed.role not in fresh.roles
            or (expected_actor is not None and expected_actor != (fresh.local_user_id, subject))):
        raise HTTPException(403, "Original managed identity was revoked during authorization")
    return fresh, managed, subject


async def _lock_process_users(session, *user_ids):
    # One sorted query avoids actor/nominee inversion across organizations.
    await session.scalars(select(User).where(User.id.in_(sorted(set(user_ids))))
        .order_by(User.id).with_for_update().execution_options(populate_existing=True))


def _sop_content(sop):
    value = sop.model_dump(mode="json")
    return {**value, "server_digest": _digest({
        key: value[key] for key in ("version", "full_content", "approval_reference")
    })}


def _qualification_digest(content):
    # Evidence/reason/UUID changes alone cannot renew first-unit qualification.
    return _digest({
        "composition_schema_version": content["composition_schema_version"],
        "composition_digest": content["composition_digest"],
        "source_refs": content["source_refs"],
        "sop_digest": content["sop"]["server_digest"],
        "process": content["process"],
        "setup": {key: content["setup"][key] for key in ("version", "content")},
        "qc": content["qc"],
        "responsibility": {key: content["responsibility"][key]
            for key in ("qc_responsible_user_id", "qc_responsible_sub")},
        "independence": {key: content["independence"][key]
            for key in ("policy_version", "reference", "rule")},
    })


async def _require_unused_first_key(session, core, organization_id, order_id, key):
    # Caller holds the production order lock also used by physical assembly commands.
    executions = (await session.scalars(select(AssemblyExecution).where(
        AssemblyExecution.organization_id == organization_id,
        AssemblyExecution.order_id == order_id,
    ).order_by(AssemblyExecution.id).with_for_update()
        .execution_options(populate_existing=True))).all()
    for execution in executions:
        keys, used = execution.output_keys, execution.consumed_output_keys
        if (not _valid_uuid(execution.id) or not isinstance(keys, list) or not keys
                or not isinstance(used, list)
                or any(not _nonempty(item, 64) or item != item.strip() for item in keys + used)
                or len(set(keys)) != len(keys) or len(set(used)) != len(used)
                or not set(used).issubset(keys)):
            _conflict("Current assembly key history is malformed")
    try:
        _active, consumed, _settled = await PhysicalStock(
            core.services.stock, services=core.services,
        )._assembly_quota_state(session, organization_id, order_id)
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise HTTPException(409, "Current assembly consumption history is malformed") from exc
    if key in consumed:
        _conflict("An already consumed output key cannot become a new first unit")


def _require_manager(user, managed):
    if managed.role not in MANAGER_ROLES or managed.role not in getattr(user, "roles", ()):
        raise HTTPException(403, "Only a current administrator or director may prepare assembly metadata")


def _require_approver(core, user):
    if not has_permission(core, user, "wms.approve"):
        raise HTTPException(403, "Current WMS approval permission is required")


def _source_method(core):
    service = getattr(core.services, "production_output", None)
    method = getattr(service, "assembly_run_source", None)
    if not callable(method):
        raise HTTPException(503, "Production assembly source gateway is unavailable")
    return method


def _validate_assignment(assignment, organization_id, order_id, warehouse_id):
    if (not isinstance(assignment, dict)
            or set(assignment) != {"organization_id", "order_id", "revision", "state", "warehouse_id", "own_active"}
            or type(assignment["organization_id"]) is not int or assignment["organization_id"] != organization_id
            or type(assignment["order_id"]) is not int or assignment["order_id"] != order_id
            or type(assignment["revision"]) is not int or assignment["revision"] < 0
            or assignment["state"] not in {"unassigned", "assigned", "released"}
            or (assignment["warehouse_id"] is not None and not _positive_int(assignment["warehouse_id"]))
            or type(assignment["own_active"]) is not bool
            or (assignment["own_active"] and (assignment["state"] != "assigned"
                or assignment["warehouse_id"] != warehouse_id))):
        _conflict("Production assignment gateway returned an invalid DTO")
    return assignment


def _source_refs_model(source):
    from modules.wms.physical_schemas import AssemblyRunSourceRefs

    try:
        return AssemblyRunSourceRefs.model_validate(source["refs"])
    except Exception as exc:
        raise HTTPException(409, "Production assembly source references are malformed") from exc


def _validated_source(source, organization_id, order_id):
    source = _source_dto(source, organization_id, order_id)
    if source["status"] != "validated":
        raise HTTPException(409, "Current approved assembly source is not valid")
    refs = _source_refs_model(source).model_dump(mode="json")
    return source, refs


def _assert_expected_source(source, refs, expected_refs, schema_version, expected_digest):
    if (not _refs_equal(refs, expected_refs)
            or schema_version != source["composition"]["schema_version"]
            or expected_digest != source["composition"]["digest"]):
        _conflict("Approved assembly source changed; refresh process metadata")


def _get_rows_statement(organization_id, order_id, *, lock=False, through_revision=None):
    statement = select(AssemblyProcessRun).where(
        AssemblyProcessRun.organization_id == organization_id,
        AssemblyProcessRun.order_id == order_id,
    ).order_by(AssemblyProcessRun.revision.asc(), AssemblyProcessRun.id.asc())
    if through_revision is not None:
        statement = statement.where(AssemblyProcessRun.revision <= through_revision)
    if lock:
        statement = statement.with_for_update()
    return statement.execution_options(populate_existing=True)


async def _history(session, organization_id, order_id, *, lock=False, through_revision=None):
    rows = (await session.scalars(_get_rows_statement(
        organization_id, order_id, lock=lock, through_revision=through_revision,
    ))).all()
    head, by_id = _validate_chain(rows, organization_id, order_id)
    return rows, head, by_id


async def _find_request(session, organization_id, request_id):
    return await session.scalar(
        select(AssemblyProcessRun).where(
            AssemblyProcessRun.organization_id == organization_id,
            AssemblyProcessRun.request_id == str(request_id),
        ).with_for_update().execution_options(populate_existing=True)
    )


def _replay_or_conflict(row, organization_id, order_id, request_id, actor_user_id, actor_sub, command):
    _validate_row(row)
    if (row.organization_id != organization_id or row.order_id != order_id
            or row.request_id != str(request_id) or row.actor_user_id != actor_user_id
            or row.actor_sub != actor_sub or row.command != command):
        _conflict("Assembly process request key conflicts with its original revision")
    return row.result


def _response(row):
    return _receipt_for(row)


def _receipt(row, source_refs, composition_digest, prepared_revision_id, prepared_digest):
    created_at = row.created_at
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    created = created_at.astimezone(timezone.utc).isoformat()
    return {
        "schema_version": 1,
        "organization_id": row.organization_id,
        "order_id": row.order_id,
        "request_id": row.request_id,
        "run_id": row.run_id,
        "revision_id": row.id,
        "revision": row.revision,
        "kind": row.kind,
        "actor_user_id": row.actor_user_id,
        "actor_sub": row.actor_sub,
        "created_at": created,
        "source_refs": source_refs,
        "composition_schema_version": 1,
        "composition_digest": composition_digest,
        "prepared_revision_id": prepared_revision_id,
        "prepared_digest": prepared_digest,
        "metadata_outcome": row.kind,
    }


async def _check_responsibility(session, core, organization_id, responsibility):
    managed = await session.scalar(
        select(User).where(
            User.id == responsibility.qc_responsible_user_id,
            User.keycloak_user_id == responsibility.qc_responsible_sub,
            User.status == "active",
        ).execution_options(populate_existing=True)
    )
    if managed is None:
        raise HTTPException(422, "QC responsibility must name an active managed identity")
    candidate = CurrentUser(
        username=managed.username, roles=[managed.role] if managed.role else [],
        keycloak_user_id=managed.keycloak_user_id, local_status=managed.status,
    )
    if not has_permission(core, candidate, "wms.approve"):
        raise HTTPException(422, "QC responsible identity must have current WMS approval eligibility")
    accounting = getattr(core.services, "accounting", None)
    if accounting is None:
        raise HTTPException(503, "Organization service is unavailable")
    member = await accounting.source_member(session, organization_id, candidate)
    if member != managed.keycloak_user_id:
        raise HTTPException(422, "QC responsible identity is not a current organization member")
    return managed


async def prepare_process_run(session, core, organization_id, order_id, user, command: AssemblyRunPrepare):
    fresh, managed, actor_sub = await _fresh_actor(session, core, organization_id, user)
    _require_manager(fresh, managed)
    original_actor = (managed.id, actor_sub)
    normalized = {**command.model_dump(mode="json"), "kind": "prepare"}
    prior = await _find_request(session, organization_id, command.request_id)
    if prior is not None:
        await _lock_process_users(session, original_actor[0])
        history, _head, _by_id = await _history(
            session, organization_id, order_id, lock=True, through_revision=prior.revision,
        )
        if not any(row.id == prior.id for row in history):
            _conflict("Assembly preparation receipt is not part of its order history")
        fresh, managed, actor_sub = await _fresh_actor(
            session, core, organization_id, user, expected_actor=original_actor,
        )
        _require_manager(fresh, managed)
        return _replay_or_conflict(
            prior, organization_id, order_id, command.request_id, managed.id, actor_sub, normalized,
        )

    await _lock_process_users(session, original_actor[0], command.responsibility.qc_responsible_user_id)
    fresh, managed, actor_sub = await _fresh_actor(
        session, core, organization_id, user, expected_actor=original_actor,
    )
    _require_manager(fresh, managed)
    source, refs = _validated_source(
        await _source_method(core)(session, organization_id, order_id, lock=True),
        organization_id, order_id,
    )
    _assert_expected_source(source, refs, command.expected_source_refs.model_dump(mode="json"),
        command.expected_composition_schema_version, command.expected_composition_digest)
    _rows, head, by_id = await _history(session, organization_id, order_id, lock=True)
    current_revision = head.revision if head else 0
    if command.expected_revision != current_revision:
        _conflict("Assembly process revision changed; refresh before preparing")
    _validate_policy_state(command)
    await _check_responsibility(session, core, organization_id, command.responsibility)
    await _require_unused_first_key(session, core, organization_id, order_id, command.designated_first_output_key)
    fresh, managed, actor_sub = await _fresh_actor(
        session, core, organization_id, user, expected_actor=original_actor,
    )
    _require_manager(fresh, managed)
    content = {
        "schema_version": 1,
        "source_refs": refs,
        "composition_schema_version": source["composition"]["schema_version"],
        "composition_snapshot": source["composition"]["snapshot"],
        "composition_digest": source["composition"]["digest"],
        "designated_first_output_key": command.designated_first_output_key,
        "sop": _sop_content(command.sop),
        "process": command.process.model_dump(mode="json"),
        "setup": command.setup.model_dump(mode="json"),
        "qc": command.qc.model_dump(mode="json"),
        "responsibility": {**command.responsibility.model_dump(mode="json"),
            "appointed_by": {"user_id": managed.id, "sub": actor_sub}},
        "independence": command.independence.model_dump(mode="json"),
        "manufacturing_policy": command.manufacturing_policy.model_dump(mode="json"),
        "serial_policy": command.serial_policy.model_dump(mode="json"),
        "expiry_policy": command.expiry_policy.model_dump(mode="json"),
        "reason": command.reason,
        "evidence": command.evidence,
    }
    prior_prepared = _head_prepared(head, by_id)
    if (prior_prepared is not None
            and prior_prepared.content["designated_first_output_key"] != command.designated_first_output_key
            and _qualification_digest(prior_prepared.content) == _qualification_digest(content)):
        _conflict("A different first output key requires changed process qualification with reason and evidence")
    row = AssemblyProcessRun(
        id=str(uuid4()), run_id=head.run_id if head else str(uuid4()),
        organization_id=organization_id, order_id=order_id, revision=current_revision + 1,
        previous_revision_id=head.id if head else None, kind="prepared",
        request_id=str(command.request_id), command_schema_version=PROCESS_SCHEMA_VERSION,
        command=normalized, command_hash=_digest(normalized),
        content=content, content_digest=_digest(content), prepared_revision_id=None,
        actor_sub=actor_sub, actor_user_id=managed.id, created_at=datetime.now(timezone.utc), result=None,
    )
    row.result = _receipt(row, refs, source["composition"]["digest"], row.id, row.content_digest)
    session.add(row)
    await session.flush()
    return _response(row)


def _validate_policy_state(command):
    for field_name in ("manufacturing_policy", "serial_policy", "expiry_policy"):
        value = getattr(command, field_name)
        if value.status == "unknown" and not _nonempty(value.reason, 1000):
            raise HTTPException(422, "Unknown manufacturing policies require an explicit reason")
    if not command.qc.criteria:
        raise HTTPException(422, "Executable QC criteria are required before confirmation")
    for criterion in command.qc.criteria:
        if criterion.kind == "observation" and not _nonempty(criterion.expected_result, 1000):
            raise HTTPException(422, "Observation criteria require an explicit expected result")


async def confirm_process_run(session, core, organization_id, order_id, user, command: AssemblyRunConfirm):
    fresh, managed, actor_sub = await _fresh_actor(session, core, organization_id, user)
    _require_approver(core, fresh)
    original_actor = (managed.id, actor_sub)
    normalized = {**command.model_dump(mode="json"), "kind": "confirm"}
    prior = await _find_request(session, organization_id, command.request_id)
    await _lock_process_users(session, original_actor[0])
    if prior is not None:
        history, _head, _by_id = await _history(
            session, organization_id, order_id, lock=True, through_revision=prior.revision,
        )
        if not any(row.id == prior.id for row in history):
            _conflict("Assembly confirmation receipt is not part of its order history")
        fresh, managed, actor_sub = await _fresh_actor(
            session, core, organization_id, user, expected_actor=original_actor,
        )
        _require_approver(core, fresh)
        return _replay_or_conflict(
            prior, organization_id, order_id, command.request_id, managed.id, actor_sub, normalized,
        )

    source, refs = _validated_source(
        await _source_method(core)(session, organization_id, order_id, lock=True),
        organization_id, order_id,
    )
    _assert_expected_source(source, refs, command.expected_source_refs.model_dump(mode="json"),
        command.expected_composition_schema_version, command.expected_composition_digest)
    _rows, head, _by_id = await _history(session, organization_id, order_id, lock=True)
    fresh, managed, actor_sub = await _fresh_actor(
        session, core, organization_id, user, expected_actor=original_actor,
    )
    _require_approver(core, fresh)
    if (head is None or head.kind != "prepared" or command.expected_revision != head.revision
            or str(command.prepared_revision_id) != head.id
            or command.expected_prepared_digest != head.content_digest):
        _conflict("Only the exact current prepared revision can be confirmed")
    if (head.content["source_refs"] != refs
            or head.content["composition_digest"] != source["composition"]["digest"]
            or head.content["composition_schema_version"] != 1):
        _conflict("Prepared metadata no longer matches current approved composition")
    responsibility = head.content["responsibility"]
    if (responsibility["qc_responsible_user_id"] != managed.id
            or responsibility["qc_responsible_sub"] != actor_sub):
        raise HTTPException(403, "Only the exact nominated QC responsible identity may confirm")
    _validate_prepared_content(head)
    content = {"prepared_revision_id": head.id, "prepared_digest": head.content_digest,
        "evidence": command.evidence}
    row = AssemblyProcessRun(
        id=str(uuid4()), run_id=head.run_id, organization_id=organization_id,
        order_id=order_id, revision=head.revision + 1, previous_revision_id=head.id,
        kind="confirmed", request_id=str(command.request_id), command_schema_version=PROCESS_SCHEMA_VERSION,
        command=normalized, command_hash=_digest(normalized), content=content,
        content_digest=_digest(content), prepared_revision_id=head.id,
        actor_sub=actor_sub, actor_user_id=managed.id, created_at=datetime.now(timezone.utc), result=None,
    )
    row.result = _receipt(row, refs, source["composition"]["digest"], head.id, head.content_digest)
    session.add(row)
    await session.flush()
    return _response(row)


def _validate_prepared_content(row):
    content = row.content
    required = {
        "schema_version", "source_refs", "composition_schema_version", "composition_snapshot",
        "composition_digest", "designated_first_output_key", "sop", "process", "setup", "qc",
        "responsibility", "independence", "manufacturing_policy", "serial_policy", "expiry_policy",
        "reason", "evidence",
    }
    responsibility = content.get("responsibility") if isinstance(content, dict) else None
    if (not isinstance(content, dict) or set(content) != required
            or content["schema_version"] != 1 or content["composition_schema_version"] != 1
            or _digest(content["composition_snapshot"]) != content["composition_digest"]
            or not isinstance(content["process"], dict)
            or not isinstance(content["process"].get("steps"), list)
            or not content["process"]["steps"]
            or not isinstance(content["qc"], dict)
            or not isinstance(content["qc"].get("criteria"), list)
            or not content["qc"]["criteria"]
            or not isinstance(responsibility, dict)
            or set(responsibility) != {
                "qc_responsible_sub", "qc_responsible_user_id", "appointment_reason",
                "appointment_evidence", "appointed_by",
            }
            or not _positive_int(responsibility["qc_responsible_user_id"])
            or not _nonempty(responsibility["qc_responsible_sub"])
            or not isinstance(responsibility["appointed_by"], dict)
            or set(responsibility["appointed_by"]) != {"user_id", "sub"}
            or responsibility["appointed_by"].get("user_id") != row.actor_user_id
            or responsibility["appointed_by"].get("sub") != row.actor_sub):
        _conflict("Prepared process metadata is incomplete or corrupt")
    from modules.wms.physical_schemas import AssemblyRunPrepare

    raw = dict(row.command)
    if raw.pop("kind", None) != "prepare":
        _conflict("Prepared process command kind is invalid")
    try:
        command = AssemblyRunPrepare.model_validate(raw)
    except Exception as exc:
        raise HTTPException(409, "Prepared process content failed strict validation") from exc
    if command.model_dump(mode="json") != raw:
        _conflict("Prepared process command is not canonical")
    server_responsibility = {
        key: value for key, value in content["responsibility"].items()
        if key != "appointed_by"
    }
    if (not _refs_equal(command.expected_source_refs.model_dump(mode="json"), content["source_refs"])
            or command.expected_composition_schema_version != content["composition_schema_version"]
            or command.expected_composition_digest != content["composition_digest"]
            or command.designated_first_output_key != content["designated_first_output_key"]
            or _sop_content(command.sop) != content["sop"]
            or command.process.model_dump(mode="json") != content["process"]
            or command.setup.model_dump(mode="json") != content["setup"]
            or command.qc.model_dump(mode="json") != content["qc"]
            or command.responsibility.model_dump(mode="json") != server_responsibility
            or command.independence.model_dump(mode="json") != content["independence"]
            or command.manufacturing_policy.model_dump(mode="json") != content["manufacturing_policy"]
            or command.serial_policy.model_dump(mode="json") != content["serial_policy"]
            or command.expiry_policy.model_dump(mode="json") != content["expiry_policy"]
            or command.reason != content["reason"] or command.evidence != content["evidence"]):
        _conflict("Prepared content differs from its immutable command")
    _validate_policy_state(command)
    _source_dto({
        "organization_id": row.organization_id, "order_id": row.order_id,
        "status": "validated", "refs": content["source_refs"],
        "composition": {"schema_version": content["composition_schema_version"],
            "digest": content["composition_digest"], "snapshot": content["composition_snapshot"]},
        "current_stage": None, "issues": [],
    }, row.organization_id, row.order_id)
    return content


def _head_prepared(head, by_id):
    if head is None:
        return None
    if head.kind == "prepared":
        return head
    return by_id.get(head.prepared_revision_id)


@dataclass(frozen=True)
class ConfirmedAssemblyPolicyInputs:
    """Private frozen inputs; this is not physical-output admission or QC PASS."""
    organization_id: int
    order_id: int
    run_id: str
    confirmed_revision_id: str
    confirmed_revision: int
    confirmed_digest: str
    prepared_revision_id: str
    prepared_digest: str
    composition_digest: str
    manufacturing_policy: AssemblyRunManufacturingPolicyResolved
    serial_policy: AssemblyRunSerialPolicyResolved
    expiry_policy: AssemblyRunExpiryPolicyResolved
    canonical_payload: bytes
    digest: str


def confirmed_assembly_policy_inputs(rows, organization_id, order_id):
    """Validate immutable history and extract explicit rules for a future consumer.

    The caller must supply the current scoped history. Current actor/source,
    output birth, uniqueness, lineage and physical QC checks remain separate.
    """
    if not _positive_int(organization_id) or not _positive_int(order_id):
        _conflict("Resolved policy inputs require exact organization and order IDs")
    head, by_id = _validate_chain(rows, organization_id, order_id)
    if head is None or head.kind != "confirmed":
        _conflict("Resolved policy inputs require a confirmed current process head")
    prepared = _head_prepared(head, by_id)
    if prepared is None:
        _conflict("Resolved policy inputs require the immutable prepared revision")
    raw = dict(prepared.command)
    raw.pop("kind")
    command = AssemblyRunPrepare.model_validate(raw)
    policies = (command.manufacturing_policy, command.serial_policy, command.expiry_policy)
    expected = (AssemblyRunManufacturingPolicyResolved, AssemblyRunSerialPolicyResolved, AssemblyRunExpiryPolicyResolved)
    if any(not isinstance(policy, model) for policy, model in zip(policies, expected)):
        _conflict("Every manufacturing, serial and expiry policy must be explicitly resolved")
    anchors = {
        "organization_id": organization_id, "order_id": order_id, "run_id": head.run_id,
        "confirmed_revision_id": head.id, "confirmed_revision": head.revision,
        "confirmed_digest": head.content_digest, "prepared_revision_id": prepared.id,
        "prepared_digest": prepared.content_digest,
        "composition_digest": prepared.content["composition_digest"],
    }
    payload = {"schema_version": 1, **anchors,
        **{field_name: policy.model_dump(mode="json") for field_name, policy in zip(
            ("manufacturing_policy", "serial_policy", "expiry_policy"), policies,
        )}}
    canonical = _canonical(payload)
    return ConfirmedAssemblyPolicyInputs(
        **anchors, manufacturing_policy=policies[0], serial_policy=policies[1], expiry_policy=policies[2],
        canonical_payload=canonical, digest=hashlib.sha256(canonical).hexdigest(),
    )


def physical_assembly_proof(model, value):
    """Strict new evidence never downgrades to the historical no-proof shape."""
    try:
        parsed = model.model_validate(value)
        if parsed.model_dump(mode="json") != value:
            _conflict("Assembly proof is not canonical")
        return parsed
    except (ValidationError, ValueError, TypeError) as exc:
        raise HTTPException(409, "Assembly physical proof is malformed") from exc


def physical_assembly_executor(seed, current):
    from modules.wms.physical_schemas import AssemblyExecutorProof

    if seed is None:
        raise HTTPException(403, "Original managed assembly principal is required")
    return physical_assembly_proof(AssemblyExecutorProof, {
        "original": {"user_id": seed[7], "sub": seed[2], "issued_at": seed[6]},
        "current": {"user_id": current.local_user_id, "sub": current.keycloak_user_id,
            "issued_at": current.issued_at},
    }).model_dump(mode="json")


async def physical_assembly_process(session, organization_id, order_id):
    from modules.wms.physical_schemas import AssemblyProcessAnchor

    rows, head, by_id = await _history(session, organization_id, order_id, lock=True)
    inputs = confirmed_assembly_policy_inputs(rows, organization_id, order_id)
    prepared = _head_prepared(head, by_id)
    anchor = physical_assembly_proof(AssemblyProcessAnchor, {
        "organization_id": organization_id, "order_id": order_id, "run_id": inputs.run_id,
        "confirmed_revision_id": inputs.confirmed_revision_id,
        "confirmed_revision": inputs.confirmed_revision, "confirmed_digest": inputs.confirmed_digest,
        "prepared_revision_id": inputs.prepared_revision_id, "prepared_digest": inputs.prepared_digest,
        "composition_digest": inputs.composition_digest, "policy_inputs_digest": inputs.digest,
        "designated_first_output_key": prepared.content["designated_first_output_key"],
    }).model_dump(mode="json")
    return anchor, inputs, prepared


async def physical_assembly_binding(session, execution, current_anchor, *, historical=None):
    from modules.wms.physical_models import PhysicalOperation
    from modules.wms.physical_schemas import AssemblyBindProof
    from modules.wms.physical_stock import digest

    row = await session.scalar(select(PhysicalOperation).where(
        PhysicalOperation.organization_id == execution.organization_id,
        PhysicalOperation.operation_id == execution.bind_operation_id,
        PhysicalOperation.kind == "assembly_bind",
    ).execution_options(populate_existing=True))
    if row is None or "assembly_bind_proof_v1" not in row.result:
        _conflict("Historical unconfigured binding permits replay or unused-kit unwind only")
    if historical is not None:
        historical.operation(row)
    proof = physical_assembly_proof(AssemblyBindProof, row.result["assembly_bind_proof_v1"])
    history, head, by_id = await _history(session, execution.organization_id, execution.order_id,
        through_revision=proof.anchor.confirmed_revision)
    inputs = confirmed_assembly_policy_inputs(history, execution.organization_id, execution.order_id)
    prepared = _head_prepared(head, by_id)
    expected_anchor = {"organization_id": execution.organization_id, "order_id": execution.order_id,
        "run_id": inputs.run_id, "confirmed_revision_id": inputs.confirmed_revision_id,
        "confirmed_revision": inputs.confirmed_revision, "confirmed_digest": inputs.confirmed_digest,
        "prepared_revision_id": inputs.prepared_revision_id, "prepared_digest": inputs.prepared_digest,
        "composition_digest": inputs.composition_digest, "policy_inputs_digest": inputs.digest,
        "designated_first_output_key": prepared.content["designated_first_output_key"]}
    if (str(proof.execution_id) != execution.id or proof.anchor.model_dump(mode="json") != current_anchor
            or expected_anchor != current_anchor
            or row.warehouse_id != execution.warehouse_id
            or row.actor != proof.executor.original.sub
            or row.result.get("execution_id") != execution.id
            or current_anchor["organization_id"] != execution.organization_id
            or current_anchor["order_id"] != execution.order_id):
        _conflict("Binding is not anchored to the current confirmed process")
    return proof, digest(current_anchor)


def physical_assembly_qc_verdict(command, pinned_criteria):
    actual = {row.criterion_id: row for row in command.criteria}
    expected = {row["criterion_id"]: row for row in pinned_criteria}
    if len(expected) != len(pinned_criteria) or set(actual) != set(expected):
        raise HTTPException(422, "QC must report every pinned criterion exactly once")
    passed = True
    for key, spec in expected.items():
        result = actual[key]
        if result.kind != spec["kind"]:
            raise HTTPException(422, "QC criterion type differs from the pinned process")
        if result.kind == "observation":
            passed &= result.result == spec["expected_result"]
            continue
        if result.unit != spec["unit"]:
            raise HTTPException(422, "QC measurement unit differs from the pinned process")
        value = Decimal(result.value)
        if value.as_tuple().exponent < -spec["precision"]:
            raise HTTPException(422, "QC value exceeds declared precision")
        try:
            quantized = value.quantize(Decimal(1).scaleb(-spec["precision"]))
        except InvalidOperation as exc:
            raise HTTPException(422, "QC value exceeds declared precision") from exc
        if value != quantized or len(quantized.as_tuple().digits) > 20:
            raise HTTPException(422, "QC value exceeds declared precision")
        lower, upper = spec["lower_bound"], spec["upper_bound"]
        if lower is not None:
            passed &= value >= Decimal(lower) if spec["lower_inclusive"] else value > Decimal(lower)
        if upper is not None:
            passed &= value <= Decimal(upper) if spec["upper_inclusive"] else value < Decimal(upper)
    return "PASS" if passed else "FAIL"


async def physical_assembly_qc_records(session, identity_id, organization_id, birth_operation_id, *, historical=None):
    from modules.wms.physical_models import PhysicalOperation
    from modules.wms.physical_schemas import AssemblyQcProof
    from modules.wms.physical_stock import digest

    rows = (await session.scalars(select(PhysicalOperation).where(
        PhysicalOperation.organization_id == organization_id,
        PhysicalOperation.kind == "assembly_qc",
        PhysicalOperation.result["assembly_qc_v1"]["command"]["identity_id"].as_string() == identity_id,
        PhysicalOperation.result["assembly_qc_v1"]["command"]["qc_birth_operation_id"].as_string() == birth_operation_id,
        *([] if historical is None else [PhysicalOperation.id < historical.before_pk]),
    ).execution_options(populate_existing=True))).all()
    result = []
    for row in rows:
        proof = physical_assembly_proof(AssemblyQcProof, row.result.get("assembly_qc_v1"))
        command = proof.command.model_dump(mode="json")
        if (command["operation_id"] != row.operation_id or command["warehouse_id"] != row.warehouse_id
                or digest(command) != row.request_hash or row.actor != proof.executor.original.sub
                or (proof.verdict == "PASS") != (proof.released_unit_id is not None)):
            _conflict("Persisted QC is inconsistent with its exact command")
        result.append((row, proof))
    if len(result) > 1:
        _conflict("A physical birth has more than one terminal QC")
    return result


async def physical_assembly_first_gate(session, anchor, output_key):
    from modules.wms.physical_models import AssemblyOutputIdentity, PhysicalOperation
    from modules.wms.physical_stock import digest

    if output_key == anchor["designated_first_output_key"]:
        return
    identity = await session.scalar(select(AssemblyOutputIdentity).where(
        AssemblyOutputIdentity.organization_id == anchor["organization_id"],
        AssemblyOutputIdentity.order_id == anchor["order_id"],
        AssemblyOutputIdentity.output_key == anchor["designated_first_output_key"],
    ).execution_options(populate_existing=True))
    if identity is None or identity.anchor != anchor:
        _conflict("Designated first output must pass actual QC before continuing production")
    identity, _output, _original_birth = await physical_assembly_identity(session, identity.organization_id, identity.id)
    birth = await session.get(PhysicalOperation, identity.birth_operation_pk)
    records = await physical_assembly_qc_records(session, identity.id, identity.organization_id, birth.operation_id)
    if (len(records) != 1 or records[0][1].verdict != "PASS"
            or records[0][1].anchor_digest != digest(anchor)):
        _conflict("Designated first output must pass actual QC before continuing production")
    await physical_assembly_validate_qc(session, identity, records[0][0], records[0][1])


async def physical_assembly_validate_qc(session, identity, operation, proof, *, historical=None):
    """Persisted PASS is a command/criterion/ledger proof, not a boolean marker."""
    from modules.wms.physical_models import AssemblyProcessRun, PhysicalLedger, StockUnit
    from modules.wms.physical_stock import digest, physical_unit_frontier
    from modules.wms.item_identity import (
        _OriginUnproven, _origin_frontier_tokens, _origin_operation_snapshot,
        _origin_validate_operation, _prove_unit_boundary,
    )

    prepared = await session.get(AssemblyProcessRun, identity.anchor["prepared_revision_id"])
    if prepared is None:
        _conflict("Pinned QC process history is missing")
    _validate_prepared_content(prepared)
    criteria = prepared.content["qc"]["criteria"]
    nominee = prepared.content["responsibility"]
    original = proof.executor.original
    if (proof.anchor_digest != digest(identity.anchor)
            or str(proof.command.confirmed_revision_id) != identity.anchor["confirmed_revision_id"]
            or proof.criteria_digest != digest(criteria)
            or physical_assembly_qc_verdict(proof.command, criteria) != proof.verdict
            or (original.user_id, original.sub) != (nominee["qc_responsible_user_id"], nominee["qc_responsible_sub"])):
        _conflict("QC verdict is not bound to the immutable criteria")
    if prepared.content["independence"]["rule"] == "independent_required" and any(
        (original.user_id, original.sub) == (executor["original"]["user_id"], executor["original"]["sub"])
        for executor in (identity.output_executor, identity.consume_executor)
    ):
        _conflict("QC executor is not independent from actual production executors")
    ledgers = (await session.scalars(select(PhysicalLedger).where(PhysicalLedger.operation_pk == operation.id))).all()
    source_id = str(proof.command.unit_id)
    if historical is None:
        source = await session.get(StockUnit, source_id)
        source_identity = None if source is None else source.assembly_output_id
        prior_historical = None
    else:
        historical.operation(operation)
        prior_historical = historical.before(operation.id)
        source_identity = await prior_historical.identity(session, source_id)
    if source_identity != identity.id:
        _conflict("QC source has lost its assembly identity")
    if (str(proof.command.operation_id) != operation.operation_id or proof.command.warehouse_id != operation.warehouse_id
            or digest(proof.command.model_dump(mode="json")) != operation.request_hash
            or operation.actor != proof.executor.original.sub):
        _conflict("QC command differs from its immutable operation")
    try:
        if proof.verdict == "PASS":
            causal, _request, _result = _origin_validate_operation(_origin_operation_snapshot(operation), identity.organization_id)
            frontier = causal["prior_frontiers"][source_id]
        else:
            frontier, _records = await physical_unit_frontier(session, identity.organization_id, source_id, historical=prior_historical)
        boundary = await _prove_unit_boundary(session, identity.organization_id, source_id,
            _origin_frontier_tokens(frontier), allow_zero=proof.verdict == "FAIL", historical=prior_historical)
        if boundary.get("qc_birth_operation_id") != str(proof.command.qc_birth_operation_id):
            raise _OriginUnproven
        if proof.verdict == "PASS" and (boundary["qty"] != 1 or boundary["dimensions"]["quality"] != "quarantine"):
            raise _OriginUnproven
    except (_OriginUnproven, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(409, "QC source birth is unproved") from exc
    if proof.verdict == "FAIL":
        if ledgers:
            _conflict("Failed QC cannot create released stock")
        return None
    released_id = str(proof.released_unit_id)
    released_identity = (getattr(await session.get(StockUnit, released_id), "assembly_output_id", None)
        if historical is None else await historical.identity(session, released_id))
    pairs = {(row.unit_id, row.qty_delta) for row in ledgers}
    if (released_identity != identity.id or len(ledgers) != 2
            or pairs != {(source_id, Decimal(-1)), (released_id, Decimal(1))}
            or not any(row.unit_id == released_id and row.dimensions["quality"] == "released" for row in ledgers)
            or operation.result.get("unit", {}).get("id") != released_id
            or (historical is not None and operation.result["unit"].get("assembly_output_id") != identity.id)):
        _conflict("QC PASS has no exact released-clone ledger proof")
    return boundary if historical is not None else None


async def physical_assembly_genealogy(session, execution, output_key, anchor, *, trail=frozenset(), historical=None):
    from modules.wms.physical_models import AssemblyConsumption, AssemblyInputLink, PhysicalLedger, PhysicalOperation
    from modules.wms.physical_schemas import AssemblyConsumeProof
    from modules.wms.physical_stock import _origin_dimension_snapshot_valid, digest
    from modules.wms.item_identity import (
        _OriginUnproven, _origin_frontier_tokens, _origin_operation_snapshot, _origin_qty,
        _origin_validate_operation, _prove_unit_boundary,
    )
    from modules.wms.physical_schemas import Code

    rows = (await session.scalars(select(AssemblyConsumption).where(
        AssemblyConsumption.execution_id == execution.id, AssemblyConsumption.output_key == output_key,
    ).order_by(AssemblyConsumption.id).execution_options(populate_existing=True))).all()
    if not rows or len({row.operation_id for row in rows}) != 1:
        _conflict("Output requires one complete immutable consumption operation")
    consumption = await session.scalar(select(PhysicalOperation).where(
        PhysicalOperation.organization_id == execution.organization_id,
        PhysicalOperation.operation_id == rows[0].operation_id, PhysicalOperation.kind == "assembly_consume",
    ))
    if consumption is None:
        _conflict("Consumption receipt is missing")
    if historical is not None:
        historical.operation(consumption)
    proof = physical_assembly_proof(AssemblyConsumeProof, consumption.result.get("assembly_consume_proof_v1"))
    await physical_assembly_binding(session, execution, anchor, historical=historical)
    if (str(proof.execution_id) != execution.id or str(proof.bind_operation_id) != execution.bind_operation_id
            or proof.output_key != output_key or proof.anchor_digest != digest(anchor)
            or consumption.actor != proof.executor.original.sub):
        _conflict("Consumption is not bound to the configured execution")
    try:
        causal, request, _ = _origin_validate_operation(_origin_operation_snapshot(consumption), execution.organization_id)
        if request["execution_id"] != execution.id or request["output_key"] != output_key:
            _conflict("Consumption canonical request differs from its genealogy")
        genealogy, quantities, unit_dimensions = [], {}, {}
        for row in rows:
            link = await session.get(AssemblyInputLink, row.input_link_id)
            if (link is None or link.execution_id != execution.id or link.specification_line_id != row.specification_line_id
                    or row.qty <= 0 or (historical is None and row.qty > link.consumed_qty)):
                _conflict("Consumed input link is inconsistent")
            if historical is not None:
                # Replace the live mutable counter with complete immutable packets
                # available at the original output boundary, never today's total.
                traces = (await session.scalars(select(AssemblyConsumption).where(
                    AssemblyConsumption.input_link_id == link.id))).all()
                total = Decimal(0)
                verified = set()
                for trace in traces:
                    saved = await session.scalar(select(PhysicalOperation).where(
                        PhysicalOperation.organization_id == execution.organization_id,
                        PhysicalOperation.operation_id == trace.operation_id,
                        PhysicalOperation.kind == "assembly_consume",
                        PhysicalOperation.id < historical.before_pk))
                    if saved is None:
                        continue  # Later consumption cannot rewrite the old boundary.
                    historical.operation(saved)
                    if saved.id in verified:
                        continue
                    verified.add(saved.id)
                    typed = physical_assembly_proof(AssemblyConsumeProof, saved.result.get("assembly_consume_proof_v1"))
                    saved_causal, saved_request, _ = _origin_validate_operation(_origin_operation_snapshot(saved), execution.organization_id)
                    packet = (await session.scalars(select(AssemblyConsumption).where(
                        AssemblyConsumption.execution_id == execution.id,
                        AssemblyConsumption.operation_id == saved.operation_id))).all()
                    expected, packet_dimensions = {}, {}
                    for item in packet:
                        linked = await session.get(AssemblyInputLink, item.input_link_id)
                        if (linked is None or linked.execution_id != execution.id or item.qty <= 0
                                or item.output_key != typed.output_key or item.specification_line_id != linked.specification_line_id
                                or item.dimensions.get("qty") is None or _origin_qty(item.dimensions["qty"]) != item.qty):
                            _conflict("Historical consumption packet has an invalid input identity")
                        core = {key: value for key, value in item.dimensions.items() if key not in {"lot_no", "qty"}}
                        if not _origin_dimension_snapshot_valid(core) or core != linked.kit_dimensions:
                            _conflict("Historical consumption changed immutable input dimensions")
                        expected[linked.actual_kit_unit_id] = expected.get(linked.actual_kit_unit_id, Decimal(0)) + item.qty
                        packet_dimensions[linked.actual_kit_unit_id] = core
                    actual_packet = (await session.scalars(select(PhysicalLedger).where(PhysicalLedger.operation_pk == saved.id))).all()
                    actual_qty = {}
                    for item in actual_packet:
                        if item.qty_delta >= 0 or item.dimensions != packet_dimensions.get(item.unit_id):
                            _conflict("Historical consumption whole ledger differs from genealogy")
                        actual_qty[item.unit_id] = actual_qty.get(item.unit_id, Decimal(0)) - item.qty_delta
                    if (not packet or actual_qty != expected or typed.execution_id != UUID(execution.id)
                            or str(typed.bind_operation_id) != execution.bind_operation_id
                            or typed.anchor_digest != digest(anchor) or saved.actor != typed.executor.original.sub
                            or saved_request["execution_id"] != execution.id or saved_request["output_key"] != typed.output_key):
                        _conflict("Historical consumption whole packet is not anchored to its execution")
                    for item_id, amount in expected.items():
                        pre = await _prove_unit_boundary(session, execution.organization_id, item_id,
                            _origin_frontier_tokens(saved_causal["prior_frontiers"][item_id]), trail=trail,
                            historical=historical.before(saved.id))
                        if pre["dimensions"] != packet_dimensions[item_id] or pre["qty"] < amount:
                            _conflict("Historical consumption predecessor is unproved")
                    total += sum((item.qty for item in packet if item.input_link_id == link.id), Decimal(0))
                if row.qty > total or total > link.qty:
                    _conflict("Historical consumed quantity is not supported by immutable packets")
            snapshot = row.dimensions
            if not isinstance(snapshot, dict) or set(snapshot) != {
                "warehouse_id", "location_id", "sku_code", "batch_id", "characteristic", "purpose",
                "quality", "packaging_id", "package_state", "container", "last_checked", "next_service",
                "inspection_ref", "service_result", "arrived_at", "arrival_source", "lot_no", "qty",
            }:
                _conflict("Consumption must retain all eighteen immutable dimensions")
            core_dimensions = {key: value for key, value in snapshot.items() if key not in {"lot_no", "qty"}}
            lot_no = TypeAdapter(Code).validate_python(snapshot["lot_no"], strict=True)
            snapshot_qty = _origin_qty(snapshot["qty"])
            if (not lot_no.strip() or lot_no != lot_no.strip()
                    or not _origin_dimension_snapshot_valid(core_dimensions)
                    or snapshot_qty <= 0 or snapshot_qty != row.qty or core_dimensions != link.kit_dimensions):
                _conflict("Consumption dimensions differ from the actual kit")
            kit = await session.scalar(select(PhysicalOperation).where(
                PhysicalOperation.organization_id == execution.organization_id,
                PhysicalOperation.operation_id == link.kit_move_operation_id,
                PhysicalOperation.kind == "assembly_kit_move",
            ))
            if kit is None:
                _conflict("Actual kit move receipt is missing")
            if historical is not None:
                historical.operation(kit)
                if kit.id >= consumption.id:
                    _conflict("Historical kit must precede actual consumption")
            kit_causal, kit_request, kit_result = _origin_validate_operation(_origin_operation_snapshot(kit), execution.organization_id)
            if (kit_request["execution_id"] != execution.id or kit_result["input_link_id"] != link.id
                    or kit_result["unit"]["id"] != link.actual_kit_unit_id):
                _conflict("Kit receipt does not prove its input link")
            prekit = await _prove_unit_boundary(session, execution.organization_id, link.original_unit_id,
                _origin_frontier_tokens(kit_causal["prior_frontiers"][link.original_unit_id]), trail=trail,
                historical=None if historical is None else historical.before(kit.id))
            if prekit["dimensions"] != link.source_dimensions or prekit["qty"] < link.qty:
                _conflict("Kit source boundary is unproved")
            kit_ledgers = (await session.scalars(select(PhysicalLedger).where(PhysicalLedger.operation_pk == kit.id))).all()
            if (len(kit_ledgers) != 2 or {(item.unit_id, item.qty_delta) for item in kit_ledgers}
                    != {(link.original_unit_id, -link.qty), (link.actual_kit_unit_id, link.qty)}
                    or any(item.dimensions != (link.source_dimensions if item.unit_id == link.original_unit_id
                        else link.kit_dimensions) for item in kit_ledgers)):
                _conflict("Kit move ledger does not conserve the exact input")
            quantities[link.actual_kit_unit_id] = quantities.get(link.actual_kit_unit_id, Decimal(0)) + row.qty
            unit_dimensions[link.actual_kit_unit_id] = core_dimensions
            projected = {**snapshot, "qty": format(row.qty, ".6f")}
            genealogy.append({"id": row.id, "execution_id": row.execution_id, "input_link_id": row.input_link_id,
                "specification_line_id": row.specification_line_id, "output_key": row.output_key,
                "operation_id": row.operation_id, "qty": format(row.qty, ".6f"), "dimensions": projected})
        ledgers = (await session.scalars(select(PhysicalLedger).where(PhysicalLedger.operation_pk == consumption.id))).all()
        actual = {}
        for ledger in ledgers:
            if ledger.qty_delta >= 0:
                _conflict("Consumption has an unexpected positive ledger")
            actual[ledger.unit_id] = actual.get(ledger.unit_id, Decimal(0)) - ledger.qty_delta
            if ledger.dimensions != unit_dimensions.get(ledger.unit_id):
                _conflict("Consumption negative ledger changed its dimensions")
        if actual != quantities:
            _conflict("Consumption ledger does not match the complete genealogy")
        return genealogy, proof, consumption
    except (_OriginUnproven, KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise HTTPException(409, "Assembly input genealogy is unproved") from exc


async def physical_assembly_identity(session, organization_id, identity_id, *, trail=frozenset(), historical=None):
    from modules.wms.physical_models import AssemblyOutputIdentity, PhysicalOperation
    from modules.wms.physical_schemas import AssemblyExecutorProof, AssemblyOutputProof, AssemblyProcessAnchor, AssemblyEffectiveLot
    from modules.wms.physical_stock import digest

    identity = await session.scalar(select(AssemblyOutputIdentity).where(
        AssemblyOutputIdentity.id == identity_id, AssemblyOutputIdentity.organization_id == organization_id,
    ).execution_options(populate_existing=True))
    if identity is None:
        _conflict("Assembly output identity is missing")
    anchor = physical_assembly_proof(AssemblyProcessAnchor, identity.anchor)
    physical_assembly_proof(AssemblyEffectiveLot, identity.batch_snapshot)
    lot = physical_assembly_proof(AssemblyEffectiveLot, identity.effective_lot)
    executor = physical_assembly_proof(AssemblyExecutorProof, identity.output_executor)
    physical_assembly_proof(AssemblyExecutorProof, identity.consume_executor)
    if historical is None:
        birth = await session.get(PhysicalOperation, identity.birth_operation_pk)
    else:
        births = (await session.scalars(select(PhysicalOperation).where(
            PhysicalOperation.id == identity.birth_operation_pk,
            PhysicalOperation.id > 0,
            PhysicalOperation.id < historical.before_pk,
            PhysicalOperation.organization_id == organization_id,
            PhysicalOperation.warehouse_id == historical.warehouse_id,
            PhysicalOperation.kind == "assembly_output",
        ).execution_options(populate_existing=True))).all()
        if len(births) != 1:
            _conflict("Assembly birth operation is outside the historical boundary")
        birth = births[0]
    execution = await session.get(AssemblyExecution, identity.execution_id)
    if (birth is None or execution is None or birth.kind != "assembly_output"
            or birth.organization_id != organization_id or execution.organization_id != organization_id
            or identity.order_id != execution.order_id or anchor.order_id != identity.order_id
            or lot.batch_id != identity.batch_id or lot.sku_code != identity.sku_code
            or identity.batch_snapshot["batch_id"] != identity.batch_id
            or identity.batch_snapshot["sku_code"] != identity.sku_code):
        _conflict("Assembly identity scope is inconsistent")
    if historical is not None:
        historical.operation(birth)
    proof = physical_assembly_proof(AssemblyOutputProof, birth.result.get("assembly_output_v1"))
    if (str(proof.identity_id) != identity.id or str(proof.execution_id) != identity.execution_id
            or proof.output_key != identity.output_key or proof.anchor.model_dump(mode="json") != identity.anchor
            or proof.genealogy_digest != identity.genealogy_digest or proof.executor != executor
            or proof.effective_lot != lot or birth.actor != executor.original.sub
            or digest(identity.genealogy) != identity.genealogy_digest):
        _conflict("Assembly identity differs from its immutable output proof")
    genealogy, consume, operation = await physical_assembly_genealogy(session, execution, identity.output_key, identity.anchor, trail=trail, historical=None if historical is None else historical.before(birth.id))
    if (genealogy != identity.genealogy or str(proof.consume_operation_id) != operation.operation_id
            or consume.executor.model_dump(mode="json") != identity.consume_executor):
        _conflict("Assembly genealogy changed after output birth")
    rows, head, by_id = await _history(session, organization_id, identity.order_id,
        through_revision=anchor.confirmed_revision)
    inputs = confirmed_assembly_policy_inputs(rows, organization_id, identity.order_id)
    prepared = _head_prepared(head, by_id)
    expected_anchor = {
        "organization_id": organization_id, "order_id": identity.order_id, "run_id": inputs.run_id,
        "confirmed_revision_id": inputs.confirmed_revision_id, "confirmed_revision": inputs.confirmed_revision,
        "confirmed_digest": inputs.confirmed_digest, "prepared_revision_id": inputs.prepared_revision_id,
        "prepared_digest": inputs.prepared_digest, "composition_digest": inputs.composition_digest,
        "policy_inputs_digest": inputs.digest, "designated_first_output_key": prepared.content["designated_first_output_key"],
    }
    if expected_anchor != identity.anchor:
        _conflict("Output qualification anchor differs from its immutable confirmed history")
    from modules.wms.item_identity import _OriginUnproven, _origin_operation_snapshot, _origin_validate_operation
    try:
        _causal, request, _result = _origin_validate_operation(_origin_operation_snapshot(birth), organization_id)
    except _OriginUnproven as exc:
        raise HTTPException(409, "Output canonical command is unproved") from exc
    if (request["execution_id"] != identity.execution_id or request["output_key"] != identity.output_key
            or request["batch_id"] != identity.batch_id or request["serial_number"] != identity.serial_number
            or request["confirmed_revision_id"] != str(anchor.confirmed_revision_id)
            or request["policy_inputs_digest"] != anchor.policy_inputs_digest
            or identity.serial_scope != inputs.serial_policy.rules.uniqueness_scope
            or (inputs.serial_policy.rules.requirement == "required") != (identity.serial_number is not None)):
        _conflict("Output identity differs from its exact command and serial rules")
    expected_lot = dict(identity.batch_snapshot)
    operator_report = False
    for field, rules in (("mfg_date", inputs.manufacturing_policy.rules), ("expiry_date", inputs.expiry_policy.rules)):
        value = request[field]
        if rules.date_requirement == "not_applicable":
            if value is not None:
                _conflict("Not applicable output dates cannot replace the verified Batch snapshot")
        elif rules.date_source == "output_batch":
            if value is not None or expected_lot[field] is None:
                _conflict("Output Batch has no required immutable date")
        else:
            if (value is None or (expected_lot[field] is not None and value != expected_lot[field])
                    or (field == "mfg_date" and expected_lot[field] is not None
                        and expected_lot["manufacture_precision"] != "day")):
                _conflict("Operator date conflicts with immutable Batch evidence")
            expected_lot[field] = value
            if field == "mfg_date":
                expected_lot["manufacture_precision"] = "day"
            operator_report = True
    if operator_report:
        expected_lot["date_source"] = f"assembly_operator_report:{birth.operation_id}"
    if expected_lot != identity.effective_lot:
        _conflict("Output effective dates differ from the immutable policy and command")
    from modules.production.assembly_specifications import AssemblySpecification
    specification = await session.get(AssemblySpecification, execution.specification_id)
    if specification is None:
        _conflict("Output specification history is missing")
    totals = {}
    for entry in genealogy:
        key = entry["specification_line_id"]
        totals[key] = totals.get(key, Decimal(0)) + Decimal(entry["qty"])
    required = {str(line["legacy_bom_item_id"]): Decimal(line["quantity_per_output"])
        for line in specification.snapshot["exact_lines"]}
    if totals != required or identity.sku_code != specification.snapshot["output"]["sku_code"]:
        _conflict("Output genealogy no longer proves the exact specification norms")
    return identity, proof, birth


def physical_assembly_public_result(result):
    """Closed operational receipts, shared by final/replay/GET/event paths."""
    from modules.wms.physical_schemas import AssemblyOutputProof, AssemblyQcProof, AssemblyReturnProof

    kind = result.get("kind")
    unit = result.get("unit")
    if (not isinstance(unit, dict) or not _valid_uuid(result.get("operation_id"))
            or not _valid_uuid(unit.get("id")) or not _valid_uuid(unit.get("assembly_output_id"))
            or any(not _positive_int(unit.get(key)) or unit[key] > 2147483647
                for key in ("warehouse_id", "location_id", "batch_id"))
            or unit["warehouse_id"] != result.get("warehouse_id")
            or not isinstance(unit.get("sku_code"), str) or not 1 <= len(unit["sku_code"]) <= 64
            or any(not isinstance(unit.get(key), str) or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", unit[key]) is None
                for key in ("qty", "reserved"))
            or unit.get("quality") not in {"quarantine", "released"}):
        _conflict("Assembly receipt unit is malformed")
    if Decimal(unit["qty"]) != 1 or Decimal(unit["reserved"]) != 0:
        _conflict("Assembly receipt unit is malformed")
    if kind == "assembly_output":
        proof = physical_assembly_proof(AssemblyOutputProof, result.get("assembly_output_v1"))
        identity_id = str(proof.identity_id)
        public = {"protocol": 1, "identity_id": identity_id, "execution_id": str(proof.execution_id),
            "output_key": proof.output_key, "confirmed_revision_id": str(proof.anchor.confirmed_revision_id),
            "policy_inputs_digest": proof.anchor.policy_inputs_digest,
            "effective_lot": proof.effective_lot.model_dump(mode="json")}
        if unit["quality"] != "quarantine":
            _conflict("Output receipt must represent its quarantine birth")
    elif kind == "assembly_qc":
        proof = physical_assembly_proof(AssemblyQcProof, result.get("assembly_qc_v1"))
        command = proof.command
        identity_id = str(command.identity_id)
        if (str(command.operation_id) != result["operation_id"] or command.warehouse_id != result["warehouse_id"]
                or (proof.verdict == "PASS") != (proof.released_unit_id is not None)
                or unit["id"] != str(proof.released_unit_id or command.unit_id)
                or unit["quality"] != ("released" if proof.verdict == "PASS" else "quarantine")):
            _conflict("QC receipt does not match its terminal result")
        public = {"protocol": 1, "identity_id": identity_id, "source_unit_id": str(command.unit_id),
            "released_unit_id": str(proof.released_unit_id) if proof.released_unit_id else None,
            "verdict": proof.verdict, "qc_birth_operation_id": str(command.qc_birth_operation_id),
            "confirmed_revision_id": str(command.confirmed_revision_id), "criteria_digest": proof.criteria_digest}
    elif kind == "assembly_return":
        proof = physical_assembly_proof(AssemblyReturnProof, result.get("assembly_return_v1"))
        identity_id = str(proof.identity_id)
        if str(proof.returned_unit_id) != unit["id"] or unit["quality"] != "quarantine":
            _conflict("Return receipt does not match its quarantine birth")
        public = {"protocol": 1, "identity_id": identity_id, "ship_operation_id": str(proof.ship_operation_id),
            "pick_id": proof.pick_id, "shipped_unit_id": str(proof.shipped_unit_id),
            "qc_birth_operation_id": result["operation_id"]}
    else:
        _conflict("Unsupported assembly receipt kind")
    if unit.get("assembly_output_id") != identity_id:
        _conflict("Assembly receipt lost its identity")
    projected_unit = {key: unit[key] for key in ("id", "warehouse_id", "location_id", "sku_code", "batch_id", "quality")}
    projected_unit.update(qty="1", reserved="0", assembly_output_id=identity_id)
    return {"operation_id": result["operation_id"], "kind": kind, "warehouse_id": result["warehouse_id"],
        "unit": projected_unit, f"{kind}_v1": public}


def _h11_source_projection(source):
    composition = source["composition"]
    return {
        "status": source["status"],
        "refs": source["refs"],
        "composition": None if composition is None else {
            "schema_version": composition["schema_version"],
            "digest": composition["digest"],
        },
        "issues": source["issues"],
    }


def _h11_policy_projection(policy):
    if policy["status"] == "reference":
        return {key: policy[key] for key in ("status", "source", "revision")}
    if policy["status"] == "unknown":
        return {key: policy[key] for key in ("status", "reason")}
    if policy["status"] == "resolved":
        return {**{key: policy[key] for key in ("status", "policy_type", "source", "revision")},
            "rules": dict(policy["rules"])}
    _conflict("Manufacturing policy status is malformed")


def _h11_document_projection(content):
    qc = content["qc"]
    steps = content["process"]["steps"]
    return {
        "sop": {key: content["sop"][key] for key in (
            "version", "full_content", "approval_reference", "server_digest",
        )},
        "process": {
            "version": content["process"]["version"],
            "steps": [{key: step[key] for key in ("step_id", "content", "required_checks")}
                for step in steps],
        },
        "setup": {key: content["setup"][key] for key in ("version", "content")},
        "qc": {
            "policy_version": qc["policy_version"],
            "policy_document_or_authoritative_reference": qc["policy_document_or_authoritative_reference"],
            "criteria": qc["criteria"],
        },
        "independence": {key: content["independence"][key] for key in (
            "policy_version", "reference", "rule",
        )},
        "manufacturing_policy": _h11_policy_projection(content["manufacturing_policy"]),
        "serial_policy": _h11_policy_projection(content["serial_policy"]),
        "expiry_policy": _h11_policy_projection(content["expiry_policy"]),
    }


async def read_process_run_h11(session, core, organization_id, order_id, user, *, warehouse_id):
    if (not all(type(value) is int and 1 <= value <= 2147483647
            for value in (organization_id, order_id, warehouse_id))):
        raise HTTPException(422, "H11 requires positive 32-bit organization, order, and warehouse IDs")
    subject = getattr(user, "keycloak_user_id", None)
    issued_at = getattr(user, "issued_at", None)
    if (not _nonempty(subject) or subject != subject.strip()
            or (issued_at is not None and type(issued_at) is not int)):
        raise HTTPException(403, "Original managed identity and issue time are required for H11")

    # Resolve the original local UID before the organization/source lock sequence.
    actor_user_id = await session.scalar(select(User.id).where(
        User.keycloak_user_id == subject,
    ).execution_options(populate_existing=True))
    if not _positive_int(actor_user_id):
        raise HTTPException(403, "Original managed identity is unavailable for H11")
    original_actor = (actor_user_id, subject)
    fresh, _managed, actor_sub = await _fresh_actor(
        session, core, organization_id, user, expected_actor=original_actor,
    )
    if actor_sub != subject or getattr(fresh, "issued_at", None) != issued_at:
        raise HTTPException(403, "Original H11 identity changed before authorization")
    await _lock_process_users(session, actor_user_id)

    # H11 takes warehouse before source; physical assembly uses the reverse inner order, and the shared exclusive organization lock serializes both.
    await read_assembly_warehouse(session, organization_id, warehouse_id)
    blind = await read_assembly_blind_count(session, organization_id, warehouse_id)
    await read_assembly_warehouse(session, organization_id, warehouse_id)
    source = _source_dto(
        await _source_method(core)(session, organization_id, order_id, lock=True),
        organization_id, order_id,
    )
    _rows, head, by_id = await _history(session, organization_id, order_id, lock=True)
    prepared = _head_prepared(head, by_id)
    prepared_content = None if prepared is None else json.loads(_canonical(prepared.content))
    prepared_digest = None if prepared is None else prepared.content_digest
    prepared_nominee = None if prepared_content is None else (
        prepared_content["responsibility"]["qc_responsible_user_id"],
        prepared_content["responsibility"]["qc_responsible_sub"],
    )
    head_snapshot = None if head is None else {
        "run_id": head.run_id,
        "id": head.id,
        "revision": head.revision,
        "kind": head.kind,
        "content_digest": head.content_digest,
        "prepared_revision_id": head.id if head.kind == "prepared" else head.prepared_revision_id,
    }
    del _rows, by_id, head, prepared
    method = getattr(getattr(core.services, "production_output", None), "assembly_run_assignment", None)
    if not callable(method):
        raise HTTPException(503, "Production assignment gateway is unavailable")
    assignment = await method(session, organization_id, order_id, warehouse_id=warehouse_id,
        actor_user_id=actor_user_id, actor_sub=subject, lock=True)
    assignment = _validate_assignment(assignment, organization_id, order_id, warehouse_id)

    # All source, history, warehouse, and assignment waits have completed; re-read authority.
    fresh, managed, actor_sub = await _fresh_actor(
        session, core, organization_id, user, expected_actor=original_actor,
    )
    if actor_sub != subject or getattr(fresh, "issued_at", None) != issued_at:
        raise HTTPException(403, "Original H11 identity changed while reading process metadata")
    manager = managed.role in MANAGER_ROLES and managed.role in getattr(fresh, "roles", ())
    nominee = bool(prepared_nominee is not None
        and prepared_nominee == (actor_user_id, subject)
        and has_permission(core, fresh, "wms.approve"))
    builder = bool(assignment["own_active"] and managed.role == "production"
        and "production" in getattr(fresh, "roles", ())
        and has_permission(core, fresh, FORECAST_PERMISSION)
        and has_permission(core, fresh, "wms.read"))
    if manager:
        read_scope = "management"
    elif nominee:
        read_scope = "nominee"
    elif builder:
        read_scope = "builder"
    else:
        raise HTTPException(403, "Assembly H11 metadata requires manager, nominated QC, or assigned builder access")

    assignment_projection = {
        key: assignment[key] for key in ("revision", "state", "warehouse_id", "own_active")
    }
    if blind:
        status, source_projection, head_projection, document_projection = "hidden", None, None, None
    else:
        source_projection = _h11_source_projection(source)
        head_projection = None if head_snapshot is None else {
            **head_snapshot, "prepared_digest": prepared_digest,
        }
        document_projection = None
        if head_snapshot is None:
            status = "missing"
        elif head_snapshot["kind"] == "prepared":
            status = "unconfirmed"
        elif (prepared_content is not None and source["status"] == "validated"
                and source["composition"] is not None
                and source["refs"] == prepared_content["source_refs"]
                and source["composition"]["schema_version"] == prepared_content["composition_schema_version"]
                and source["composition"]["digest"] == prepared_content["composition_digest"]):
            status = "confirmed"
            document_projection = _h11_document_projection(prepared_content)
        else:
            status = "stale"

    result = {
        "schema_version": 1,
        "organization_id": organization_id,
        "warehouse_id": warehouse_id,
        "order_id": order_id,
        "actor": {"user_id": managed.id, "sub": actor_sub},
        "read_scope": read_scope,
        "read_at": datetime.now(timezone.utc),
        "assignment": assignment_projection,
        "status": status,
        "source": source_projection,
        "head": head_projection,
        "document": document_projection,
    }
    try:
        return AssemblyRunH11Projection.model_validate(result).model_dump(mode="json")
    except ValidationError as exc:
        raise HTTPException(409, "Assembly H11 projection failed strict validation") from exc


async def read_process_run(session, core, organization_id, order_id, user, *, warehouse_id=None, view=None):
    if view == "h11":
        return await read_process_run_h11(session, core, organization_id, order_id, user,
            warehouse_id=warehouse_id)
    fresh, managed, actor_sub = await _fresh_actor(session, core, organization_id, user)
    original_actor = (managed.id, actor_sub)
    await _lock_process_users(session, original_actor[0])
    source = _source_dto(
        await _source_method(core)(session, organization_id, order_id, lock=True),
        organization_id, order_id,
    )
    _rows, head, by_id = await _history(session, organization_id, order_id, lock=True)
    prepared = _head_prepared(head, by_id)
    assignment = None
    if warehouse_id is not None:
        await read_assembly_warehouse(session, organization_id, warehouse_id)
        method = getattr(getattr(core.services, "production_output", None), "assembly_run_assignment", None)
        if not callable(method):
            raise HTTPException(503, "Production assignment gateway is unavailable")
        assignment = await method(session, organization_id, order_id, warehouse_id=warehouse_id,
            actor_user_id=original_actor[0], actor_sub=original_actor[1], lock=True)
        assignment = _validate_assignment(assignment, organization_id, order_id, warehouse_id)
    # Source/assignment reads may wait; derive every authorization branch afterwards.
    fresh, managed, actor_sub = await _fresh_actor(
        session, core, organization_id, user, expected_actor=original_actor,
    )
    manager = managed.role in MANAGER_ROLES and managed.role in getattr(fresh, "roles", ())
    nominee = bool(prepared is not None
        and prepared.content["responsibility"]["qc_responsible_user_id"] == managed.id
        and prepared.content["responsibility"]["qc_responsible_sub"] == actor_sub
        and has_permission(core, fresh, "wms.approve"))
    builder = bool(assignment is not None and assignment["own_active"]
        and managed.role == "production" and "production" in getattr(fresh, "roles", ())
        and has_permission(core, fresh, FORECAST_PERMISSION) and has_permission(core, fresh, "wms.read"))
    if manager:
        read_scope = "management"
    elif nominee:
        read_scope = "nominee"
    elif builder:
        read_scope = "builder"
    else:
        raise HTTPException(403, "Assembly process metadata requires manager, nominated QC, or assigned builder access")
    run = None if head is None else {
        "run_id": head.run_id,
        "head": {"id": head.id, "revision": head.revision, "kind": head.kind,
            "content_digest": head.content_digest,
            "prepared_revision_id": head.prepared_revision_id if head.kind == "confirmed" else head.id},
        "content": None if prepared is None else {"prepared": prepared.content,
            "confirmation": head.content if head.kind == "confirmed" else None},
    }
    return {"schema_version": 1, "organization_id": organization_id, "order_id": order_id,
        "actor": {"user_id": managed.id, "sub": actor_sub}, "read_scope": read_scope,
        "assignment": assignment, "run": run, "source": source}


async def read_process_receipt(session, core, organization_id, order_id, request_id, user):
    fresh, managed, actor_sub = await _fresh_actor(session, core, organization_id, user)
    original_actor = (managed.id, actor_sub)
    await _lock_process_users(session, original_actor[0])
    row = await session.scalar(select(AssemblyProcessRun).where(
        AssemblyProcessRun.organization_id == organization_id,
        AssemblyProcessRun.order_id == order_id, AssemblyProcessRun.request_id == str(request_id),
    ).with_for_update().execution_options(populate_existing=True))
    if row is None or row.actor_user_id != original_actor[0] or row.actor_sub != original_actor[1]:
        raise HTTPException(404, "Assembly process receipt not found for this actor")
    rows, _head, _by_id = await _history(
        session, organization_id, order_id, lock=True, through_revision=row.revision,
    )
    found = next((entry for entry in rows if entry.id == row.id), None)
    if found is None:
        _conflict("Assembly process receipt is not part of its order history")
    fresh, managed, actor_sub = await _fresh_actor(
        session, core, organization_id, user, expected_actor=original_actor,
    )
    if row.kind == "prepared":
        _require_manager(fresh, managed)
    elif row.kind == "confirmed":
        _require_approver(core, fresh)
    else:
        _conflict("Assembly process receipt kind is invalid")
    return _receipt_for(found)
