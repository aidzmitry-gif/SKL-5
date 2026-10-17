"""Typed SQLAlchemy persistence declarations for directed placement.

This module defines persisted facts and native WorkPolicy integrity guards. The
snapshot JSON column carries only the server-frozen assignment record described by
the design: before and predicted post-owned-hold frames, current profile payloads
and slots, server route/topology binding, source/destination ancestry, exact unit
and reservation vectors, claim slots, and dependency IDs. This proposal does not
invent another nested snapshot format or authorization flag.
"""
from __future__ import annotations

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    Table,
    UniqueConstraint,
)

from core.db.base import Base
from modules.wms.physical_models import CAP_MAX, CAPACITY_WS_SQL, QTY, cap_evidence_columns

placement_work_policy_table = Table(
    "physical_placement_work_policy",
    Base.metadata,
    Column("id", String(36), primary_key=True),
    Column("organization_id", Integer, nullable=False),
    Column("warehouse_id", Integer, nullable=False),
    Column("scope_code", String(64), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("access_method", String(16), nullable=False),
    Column("valid_from", DateTime(timezone=True), nullable=False),
    Column("valid_until", DateTime(timezone=True), nullable=False),
    Column("policy", JSON(none_as_null=True), nullable=False),
    Column("server_binding", JSON(none_as_null=True), nullable=False),
    Column("canonical_payload", String, nullable=False),
    Column("digest", String(64), nullable=False),
    *cap_evidence_columns(),
    Column("approval_operation_id", String(36), nullable=False),
    UniqueConstraint("warehouse_id", "scope_code", "revision", name="cap_work_revision"),
    UniqueConstraint("organization_id", "warehouse_id", "id", name="cap_work_org_scope"),
    ForeignKeyConstraint(
        ["organization_id", "warehouse_id"],
        ["wms.physical_warehouse.organization_id", "wms.physical_warehouse.warehouse_id"],
        name="cap_work_warehouse",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["organization_id", "warehouse_id", "approval_operation_id"],
        [
            "wms.physical_operation.organization_id",
            "wms.physical_operation.warehouse_id",
            "wms.physical_operation.operation_id",
        ],
        name="cap_work_receipt",
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    ),
    CheckConstraint(
        "length(id) = 36 AND length(approval_operation_id) = 36",
        name="cap_work_ids",
    ),
    CheckConstraint(
        "revision BETWEEN 1 AND 2147483647",
        name="cap_work_revision_bounds",
    ),
    CheckConstraint(
        "access_method IN ('manual','equipment')",
        name="cap_work_method",
    ),
    CheckConstraint(
        "valid_until > valid_from",
        name="cap_work_validity",
    ),
    CheckConstraint(
        "length(digest) = 64",
        name="cap_work_digest",
    ),
    CheckConstraint(
        f"scope_code = trim(scope_code, {CAPACITY_WS_SQL}) "
        "AND length(scope_code) BETWEEN 1 AND 64",
        name="cap_work_scope_code",
    ),
    CheckConstraint(
        f"source_ref = trim(source_ref, {CAPACITY_WS_SQL}) "
        "AND length(source_ref) BETWEEN 1 AND 1000",
        name="cap_work_source_ref",
    ),
    CheckConstraint(
        f"source_revision = trim(source_revision, {CAPACITY_WS_SQL}) "
        "AND length(source_revision) BETWEEN 1 AND 200",
        name="cap_work_source_revision",
    ),
    CheckConstraint(
        f"evidence = trim(evidence, {CAPACITY_WS_SQL}) "
        "AND length(evidence) BETWEEN 1 AND 1000",
        name="cap_work_evidence",
    ),
    CheckConstraint(
        f"approved_by = trim(approved_by, {CAPACITY_WS_SQL}) "
        "AND length(approved_by) BETWEEN 1 AND 200",
        name="cap_work_approver",
    ),
    CheckConstraint(
        "length(canonical_payload) > 0",
        name="cap_work_canonical_payload",
    ),
    schema="wms",
)


placement_assignment_table = Table(
    "physical_placement_assignment",
    Base.metadata,
    Column("task_id", String(36), primary_key=True),
    Column("organization_id", Integer, nullable=False),
    Column("warehouse_id", Integer, nullable=False),
    Column("carrier_id", String(36), nullable=False),
    Column("active_carrier_id", String(36), nullable=True),
    Column("source_location_id", Integer, nullable=False),
    Column("destination_location_id", Integer, nullable=False),
    Column("source_profile_id", String(36), nullable=False),
    Column("destination_profile_id", String(36), nullable=False),
    Column("packaging_id", Integer, nullable=False),
    Column("packaging_profile_id", String(36), nullable=False),
    Column("work_policy_id", String(36), nullable=False),
    Column("source_content_revision", Integer, nullable=False),
    Column("source_content_digest", String(64), nullable=False),
    Column("gross_kg", QTY, nullable=False),
    Column("tare_kg", QTY, nullable=False),
    Column("orientation", String(3), nullable=False),
    Column("snapshot", JSON(none_as_null=True), nullable=False),
    Column("canonical_payload", String, nullable=False),
    Column("digest", String(64), nullable=False),
    Column("state", String(16), nullable=False),
    Column("assigned_by", String(200), nullable=False),
    Column("assigned_at", DateTime(timezone=True), nullable=False),
    Column("assignment_operation_id", String(36), nullable=False),
    Column("begin_operation_id", String(36), nullable=True),
    Column("settlement_operation_id", String(36), nullable=True),
    UniqueConstraint("active_carrier_id", name="cap_assignment_one_active_carrier"),
    UniqueConstraint(
        "warehouse_id",
        "destination_location_id",
        "destination_profile_id",
        "task_id",
        name="cap_assignment_claim_scope",
    ),
    ForeignKeyConstraint(
        ["organization_id", "warehouse_id"],
        ["wms.physical_warehouse.organization_id", "wms.physical_warehouse.warehouse_id"],
        name="cap_assignment_warehouse",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["warehouse_id", "task_id"],
        ["wms.physical_task.warehouse_id", "wms.physical_task.id"],
        name="cap_assignment_task",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["carrier_id"],
        ["wms.physical_carrier.id"],
        name="cap_assignment_carrier_history",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["warehouse_id", "active_carrier_id"],
        ["wms.physical_carrier.warehouse_id", "wms.physical_carrier.id"],
        name="cap_assignment_active_scope",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["warehouse_id", "source_location_id", "source_profile_id"],
        [
            "wms.physical_placement_profile.warehouse_id",
            "wms.physical_placement_profile.location_id",
            "wms.physical_placement_profile.id",
        ],
        name="cap_assignment_source_profile",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["warehouse_id", "destination_location_id", "destination_profile_id"],
        [
            "wms.physical_placement_profile.warehouse_id",
            "wms.physical_placement_profile.location_id",
            "wms.physical_placement_profile.id",
        ],
        name="cap_assignment_destination_profile",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["warehouse_id", "packaging_id", "packaging_profile_id"],
        [
            "wms.physical_placement_profile.warehouse_id",
            "wms.physical_placement_profile.packaging_id",
            "wms.physical_placement_profile.id",
        ],
        name="cap_assignment_packaging_profile",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["organization_id", "warehouse_id", "work_policy_id"],
        [
            "wms.physical_placement_work_policy.organization_id",
            "wms.physical_placement_work_policy.warehouse_id",
            "wms.physical_placement_work_policy.id",
        ],
        name="cap_assignment_work_policy",
        ondelete="RESTRICT",
    ),
    *[
        ForeignKeyConstraint(
            ["organization_id", "warehouse_id", field],
            [
                "wms.physical_operation.organization_id",
                "wms.physical_operation.warehouse_id",
                "wms.physical_operation.operation_id",
            ],
            name=name,
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        )
        for field, name in (
            ("assignment_operation_id", "cap_assignment_receipt"),
            ("begin_operation_id", "cap_assignment_begin_receipt"),
            ("settlement_operation_id", "cap_assignment_settlement_receipt"),
        )
    ],
    CheckConstraint(
        "source_location_id <> destination_location_id",
        name="cap_assignment_distinct_place",
    ),
    CheckConstraint(
        "source_content_revision BETWEEN 1 AND 2147483647",
        name="cap_assignment_content_revision",
    ),
    CheckConstraint(
        f"gross_kg >= tare_kg AND tare_kg >= 0 AND gross_kg <= {CAP_MAX} "
        f"AND tare_kg <= {CAP_MAX}",
        name="cap_assignment_mass",
    ),
    CheckConstraint(
        "orientation IN ('LWH','LHW','WLH','WHL','HLW','HWL')",
        name="cap_assignment_orientation",
    ),
    CheckConstraint(
        "state IN ('assigned','in_transit','blocked','needs_reconcile','completed','canceled')",
        name="cap_assignment_state",
    ),
    CheckConstraint(
        "(state IN ('assigned','in_transit','blocked','needs_reconcile')"
        " AND active_carrier_id IS NOT NULL AND active_carrier_id = carrier_id"
        " AND settlement_operation_id IS NULL) OR"
        " (state IN ('completed','canceled') AND active_carrier_id IS NULL"
        " AND settlement_operation_id IS NOT NULL)",
        name="cap_assignment_active",
    ),
    CheckConstraint(
        "state <> 'in_transit' OR begin_operation_id IS NOT NULL",
        name="cap_assignment_begin",
    ),
    CheckConstraint(
        "state <> 'completed' OR begin_operation_id IS NOT NULL",
        name="cap_assignment_completed",
    ),
    CheckConstraint(
        "state <> 'canceled' OR begin_operation_id IS NULL",
        name="cap_assignment_canceled_unstarted",
    ),
    CheckConstraint(
        "length(task_id) = 36 AND length(carrier_id) = 36 "
        "AND length(source_profile_id) = 36 AND length(destination_profile_id) = 36 "
        "AND length(packaging_profile_id) = 36 AND length(work_policy_id) = 36 "
        "AND length(assignment_operation_id) = 36 "
        "AND (begin_operation_id IS NULL OR length(begin_operation_id) = 36) "
        "AND (settlement_operation_id IS NULL OR length(settlement_operation_id) = 36)",
        name="cap_assignment_ids",
    ),
    CheckConstraint(
        "length(digest) = 64 AND length(source_content_digest) = 64",
        name="cap_assignment_digests",
    ),
    CheckConstraint(
        f"assigned_by = trim(assigned_by, {CAPACITY_WS_SQL}) "
        "AND length(assigned_by) BETWEEN 1 AND 200",
        name="cap_assignment_actor",
    ),
    CheckConstraint(
        "length(canonical_payload) > 0",
        name="cap_assignment_canonical_payload",
    ),
    schema="wms",
)


placement_claim_table = Table(
    "physical_placement_claim",
    Base.metadata,
    Column("task_id", String(36), primary_key=True),
    Column("ordinal", Integer, primary_key=True),
    Column("warehouse_id", Integer, nullable=False),
    Column("location_id", Integer, nullable=False),
    Column("profile_id", String(36), nullable=False),
    Column("slot_key", String(64), nullable=False),
    UniqueConstraint(
        "warehouse_id",
        "location_id",
        "profile_id",
        "slot_key",
        name="cap_claim_one_slot",
    ),
    ForeignKeyConstraint(
        ["warehouse_id", "location_id", "profile_id", "task_id"],
        [
            "wms.physical_placement_assignment.warehouse_id",
            "wms.physical_placement_assignment.destination_location_id",
            "wms.physical_placement_assignment.destination_profile_id",
            "wms.physical_placement_assignment.task_id",
        ],
        name="cap_claim_assignment",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["warehouse_id", "location_id", "profile_id", "slot_key"],
        [
            "wms.physical_placement_slot.warehouse_id",
            "wms.physical_placement_slot.location_id",
            "wms.physical_placement_slot.profile_id",
            "wms.physical_placement_slot.slot_key",
        ],
        name="cap_claim_slot",
        ondelete="RESTRICT",
    ),
    CheckConstraint("ordinal >= 0", name="cap_claim_ordinal"),
    CheckConstraint(
        f"profile_id = trim(profile_id, {CAPACITY_WS_SQL}) "
        "AND length(profile_id) = 36",
        name="cap_claim_profile_id",
    ),
    schema="wms",
)


class PlacementWorkPolicy(Base):
    __table__ = placement_work_policy_table


class PlacementAssignment(Base):
    __table__ = placement_assignment_table


class PlacementClaim(Base):
    __table__ = placement_claim_table


# WorkPolicy uses the existing capacity root/SAVEPOINT lifetime. Registration is
# here so either import order installs exactly the same mapper and Core guards.
_WORK_OLD = "wms_work_policy_original_v1"
_WORK_RECEIPT_OLD = "wms_work_receipt_original_v1"
_WORK_CONNECTION = "wms_work_policy_producer_session_v1"


def work_operation_indicator(kind, result):
    return kind == "capacity_work_approve" or (
        isinstance(result, dict) and (
            "_placement_work_policy_v1" in result or result.get("kind") == "capacity_work_approve"
        )
    )


def _work_json(values):
    from modules.wms.physical_models import _cap_json, _cap_timestamp_text
    body = dict(values)
    for key in ("valid_from", "valid_until", "approved_at", "created_at"):
        if key in body and body[key] is not None and not isinstance(body[key], str):
            body[key] = _cap_timestamp_text(body[key])
    return _cap_json(body)


def _work_passive(target):
    from sqlalchemy import inspect
    state = inspect(target)
    return {column.name: state.dict[column.name] for column in target.__table__.columns
            if column.name in state.dict}


def _work_obligations(session):
    from modules.wms.physical_models import _cap_fail, _cap_tracking
    state = _cap_tracking(session, create=False)
    result = {}
    if state is None:
        return result
    for entries in state.get("work_levels", {}).values():
        for key, item in entries.items():
            if key not in result:
                result[key] = {**item, "authorities": list(item["authorities"])}
            else:
                prior = result[key]
                if any(prior[name] != item[name] for name in ("row", "receipt", "insert")):
                    _cap_fail("work_duplicate_obligation_conflict")
                if prior["persistent_pk"] is not None and item["persistent_pk"] is not None and prior["persistent_pk"] != item["persistent_pk"]:
                    _cap_fail("work_receipt_identity_conflict")
                if prior["persistent_pk"] is None:
                    prior["persistent_pk"] = item["persistent_pk"]
                prior["authorities"].extend(item["authorities"])
    return result


def cap_freeze_work_policy(session, row, receipt, *, authority, insert, persistent_pk=None):
    import json
    import weakref
    from modules.wms.physical_models import _cap_fail, _cap_tracking
    from modules.wms.placement_service import validate_work_envelope
    validate_work_envelope(receipt, row)
    if not callable(authority) or type(insert) is not bool:
        _cap_fail("work_verified_authority_callback_required")
    state = _cap_tracking(session, create=True)
    level = session.get_nested_transaction() or session.get_transaction()
    key = (row["organization_id"], row["warehouse_id"], row["approval_operation_id"])
    expected_row, expected_receipt = _work_json(row), _work_json(receipt)
    inherited = _work_obligations(session).get(key)
    if inherited is not None and (inherited["row"] != expected_row or inherited["receipt"] != expected_receipt or inherited["insert"] != insert):
        _cap_fail("work_producer_obligation_conflict")
    if inherited is not None:
        if persistent_pk is not None and inherited["persistent_pk"] is not None and persistent_pk != inherited["persistent_pk"]:
            _cap_fail("work_receipt_identity_conflict")
        persistent_pk = persistent_pk if persistent_pk is not None else inherited["persistent_pk"]
    entries = state.setdefault("work_levels", {}).setdefault(level, {})
    old = entries.get(key)
    callbacks = [] if old is None else list(old["authorities"])
    callbacks.append(authority)
    entries[key] = {"row": expected_row, "receipt": expected_receipt, "insert": insert,
                    "persistent_pk": persistent_pk, "authorities": callbacks}
    session.info.pop("wms_work_final_authority_v1", None)
    connection = session.connection(bind_arguments={"mapper": PlacementWorkPolicy})
    connection.info[_WORK_CONNECTION] = (weakref.ref(session), state["root"])
    # Copies above, never references to caller-owned JSON.
    return json.loads(expected_row), json.loads(expected_receipt)


def _work_assert_loaded(session):
    import json
    from sqlalchemy import inspect
    from modules.wms.physical_models import PhysicalOperation, _cap_fail
    obligations = _work_obligations(session)
    for target in tuple(session.new) + tuple(session.identity_map.values()):
        if not isinstance(target, (PlacementWorkPolicy, PhysicalOperation)):
            continue
        state, values = inspect(target), _work_passive(target)
        policy = isinstance(target, PlacementWorkPolicy)
        original = state.info.get(_WORK_OLD if policy else _WORK_RECEIPT_OLD)
        if original is not None:
            before = json.loads(original)
            if any(_work_json({key: value}) != _work_json({key: before.get(key)}) for key, value in values.items()):
                _cap_fail("work_persistent_json_or_identity_mutated")
        indicator = policy or work_operation_indicator(values.get("kind"), values.get("result"))
        operation_id = values.get("approval_operation_id") if policy else values.get("operation_id")
        item = obligations.get((values.get("organization_id"), values.get("warehouse_id"), operation_id))
        if target in session.new and indicator and (item is None or not item["insert"]):
            _cap_fail("work_insert_without_verified_producer")
        if item is not None:
            expected = json.loads(item["row"] if policy else item["receipt"])
            if not policy:
                expected = {key: expected[key] for key in ("organization_id", "warehouse_id", "operation_id", "kind", "request_hash", "actor", "result")}
            if any(key not in values or _work_json({key: values[key]}) != _work_json({key: value}) for key, value in expected.items()):
                _cap_fail("work_loaded_expected_snapshot_mismatch")
            if not policy and item["persistent_pk"] is not None and (
                values.get("id") != item["persistent_pk"] or (state.identity is not None and state.identity != (item["persistent_pk"],))
            ):
                _cap_fail("work_receipt_identity_mutated")


def _work_before_insert(mapper, connection, target):
    from sqlalchemy import inspect
    from modules.wms.physical_models import PhysicalOperation, _cap_fail, _cap_require_read_committed
    if isinstance(target, PhysicalOperation) and not work_operation_indicator(target.kind, target.result):
        return
    session = inspect(target).session
    if session is None:
        _cap_fail("work_producer_session_missing")
    _cap_require_read_committed(connection)
    _work_assert_loaded(session)


def _work_immutable(mapper, connection, target):
    from modules.wms.physical_models import _cap_fail
    _cap_fail("work_policy_immutable")


def _work_loaded(target, context, attrs=None):
    from sqlalchemy import inspect
    state = inspect(target)
    if _WORK_OLD not in state.info:
        state.info[_WORK_OLD] = _work_json(_work_passive(target))


def _work_receipt_loaded(target, context, attrs=None):
    from sqlalchemy import inspect
    state = inspect(target)
    if work_operation_indicator(state.dict.get("kind"), state.dict.get("result")) and _WORK_RECEIPT_OLD not in state.info:
        state.info[_WORK_RECEIPT_OLD] = _work_json(_work_passive(target))


def _work_after_insert(mapper, connection, target):
    from sqlalchemy import inspect
    from modules.wms.physical_models import PhysicalOperation, _cap_fail, _cap_tracking
    state, values = inspect(target), _work_passive(target)
    if isinstance(target, PlacementWorkPolicy):
        state.info.setdefault(_WORK_OLD, _work_json(values))
        return
    if not isinstance(target, PhysicalOperation) or not work_operation_indicator(values.get("kind"), values.get("result")):
        return
    tracking = _cap_tracking(state.session, create=False)
    key = (values["organization_id"], values["warehouse_id"], values["operation_id"])
    for entries in tracking.get("work_levels", {}).values():
        item = entries.get(key)
        if item is not None:
            if item["persistent_pk"] is not None and item["persistent_pk"] != values["id"]:
                _cap_fail("work_receipt_identity_conflict")
            item["persistent_pk"] = values["id"]
    state.info.setdefault(_WORK_RECEIPT_OLD, _work_json(values))


def cap_validate_work_root(session):
    import json
    from sqlalchemy import select
    from modules.wms.physical_models import PhysicalOperation, _cap_fail, _cap_require_read_committed
    from modules.wms.placement_service import validate_work_receipt, validate_current_work_policy
    _work_assert_loaded(session)
    obligations = _work_obligations(session)
    if not obligations:
        return
    connection = session.connection(bind_arguments={"mapper": PlacementWorkPolicy})
    _cap_require_read_committed(connection)
    for (org, wh, operation_id), item in obligations.items():
        table = PhysicalOperation.__table__
        receipt = connection.execute(select(table).where(table.c.organization_id == org,
            table.c.warehouse_id == wh, table.c.operation_id == operation_id)).mappings().one_or_none()
        if receipt is None:
            _cap_fail("work_root_receipt_missing")
        if item["persistent_pk"] is None or receipt["id"] != item["persistent_pk"]:
            _cap_fail("work_root_receipt_identity_changed")
        expected = json.loads(item["receipt"])
        if any(_work_json({key: receipt[key]}) != _work_json({key: expected[key]}) for key in expected):
            _cap_fail("work_root_receipt_mutated_or_resealed")
        values = validate_work_receipt(connection, receipt)
        if _work_json(values) != item["row"]:
            _cap_fail("work_root_policy_mutated_or_resealed")
        if item["insert"]:
            validate_current_work_policy(connection, values, receipt["result"]["_placement_work_policy_v1"]["equipment_receipt"])


def _work_core_bind_type(bound):
    """Only exact native scalar bind types without dialect variants are trusted."""
    from sqlalchemy.sql.sqltypes import (
        BigInteger, Boolean, Date, DateTime, Float, Integer, JSON, LargeBinary,
        NullType, Numeric, String, Unicode, Uuid,
    )
    from modules.wms.physical_models import _cap_fail
    if type(bound.type) not in (Integer, BigInteger, String, Unicode, JSON,
            DateTime, Float, Boolean, Numeric, Date, Uuid, LargeBinary, NullType) or bound.type._variant_mapping:
        _cap_fail("work_core_bind_type_forbidden")


def _work_core_values(entries, parameters):
    """Resolve native scalar binds; SQL expressions never classify as nonwork."""
    from sqlalchemy.sql.elements import BindParameter, ClauseElement, _truncated_label
    from modules.wms.physical_models import _cap_fail
    if any(not isinstance(key, str) for key in parameters):
        _cap_fail("work_core_parameter_key_unknown")
    values = {}
    for key, value in entries:
        name = key.key if hasattr(key, "key") else str(key)
        if isinstance(value, BindParameter):
            _work_core_bind_type(value)
            if value.callable is not None:
                _cap_fail("work_core_callable_bind_forbidden")
            # Native CRUD compiler renames unique anonymous binds to the
            # column name. Explicit named binds keep their own parameter key.
            anonymous = value.unique and isinstance(value.key, _truncated_label)
            if anonymous and value.key != name and value.key in parameters and name in parameters:
                _cap_fail("work_core_anonymous_bind_dual_key")
            if not anonymous and value.key != name and name in parameters:
                _cap_fail("work_core_named_bind_column_shadow")
            if value.key in parameters:
                value = parameters[value.key]
            elif anonymous and name in parameters:
                value = parameters[name]
            elif value.required:
                _cap_fail("work_core_unresolved_bind_parameter")
            else:
                value = value.value
        if isinstance(value, ClauseElement):
            _cap_fail("work_core_sql_expression_forbidden")
        values[name] = value
    for key, value in parameters.items():
        if isinstance(value, ClauseElement):
            _cap_fail("work_core_sql_expression_forbidden")
        if key not in values:
            values[key] = value
    return values


def _work_core_targets(clause, batches, *, native_table=None):
    """Closed native WHERE/binding preflight; no SQL runs during validation."""
    from datetime import date, datetime
    from decimal import Decimal
    from uuid import UUID
    from sqlalchemy.sql import operators, visitors
    from sqlalchemy.sql.elements import (
        BinaryExpression, BindParameter, BooleanClauseList, False_, Grouping,
        Null, True_, UnaryExpression,
    )
    from sqlalchemy.sql.dml import Update
    from modules.wms.physical_models import PhysicalLedger, PhysicalOperation, _cap_fail
    table = PhysicalOperation.__table__ if native_table is None else native_table
    if table is not PhysicalOperation.__table__ and table is not PhysicalLedger.__table__ and table is not PlacementAssignment.__table__:
        _cap_fail("work_core_where_table_unknown")
    if clause.table._deannotate() is not table:
        _cap_fail("work_core_where_table_unknown")
    binds = {}
    predicates = tuple(clause._where_criteria)
    comparisons = (operators.eq, operators.ne, operators.lt, operators.le,
                   operators.gt, operators.ge, operators.is_, operators.is_not)
    for criterion in predicates:
        for node in visitors.iterate(criterion):
            if type(node) is BindParameter:
                _work_core_bind_type(node)
                if node.callable is not None:
                    _cap_fail("work_core_where_callable_bind_forbidden")
                if node.expanding or node.literal_execute:
                    _cap_fail("work_core_where_dynamic_bind_forbidden")
                if node.key in binds and binds[node.key] is not node:
                    _cap_fail("work_core_where_binding_ambiguous")
                binds[node.key] = node
                continue
            if getattr(node, "table", None) is table and node._deannotate() is table.c.get(node.key):
                continue
            if type(node) in (True_, False_, Null, Grouping):
                continue
            if type(node) is BooleanClauseList and node.operator in (operators.and_, operators.or_):
                continue
            if type(node) is BinaryExpression and node.operator in comparisons and node.modifiers == {}:
                continue
            if type(node) is UnaryExpression and node.operator is operators.inv and node.modifier is None:
                continue
            _cap_fail("work_core_where_expression_forbidden")
    entries = tuple((clause._values or {}).items()) if isinstance(clause, Update) else ()
    assignment_keys = set()
    for key, value in entries:
        name = key.key if hasattr(key, "key") else str(key)
        if name not in table.c or (not isinstance(key, str) and
                (getattr(key, "table", None) is not table or key._deannotate() is not table.c[name])):
            _cap_fail("work_core_where_set_column_unknown")
        assignment_keys.add(name)
        if isinstance(value, BindParameter):
            assignment_keys.add(value.key)
    # UPDATE's actual-column execute parameters are additional SET values.
    if isinstance(clause, Update):
        assignment_keys.update(table.c.keys())
    if assignment_keys.intersection(binds):
        _cap_fail("work_core_where_set_binding_overlap")
    resolved = []
    scalar_types = (str, int, float, bool, bytes, Decimal, date, datetime, UUID)
    for parameters in batches:
        if any(not isinstance(key, str) for key in parameters):
            _cap_fail("work_core_parameter_key_unknown")
        if set(parameters) - set(binds) - assignment_keys:
            _cap_fail("work_core_where_parameter_unknown")
        target = {}
        for key, bound in binds.items():
            if key in parameters:
                value = parameters[key]
            elif bound.required:
                _cap_fail("work_core_where_unresolved_bind_parameter")
            else:
                value = bound.value
            if value is not None and type(value) not in scalar_types:
                _cap_fail("work_core_where_nonscalar_bind_forbidden")
            target[key] = value
        resolved.append(target)
    return predicates, resolved, entries


def _work_core_before_execute(connection, clause, multiparams, params, execution_options):
    """Independent Core DML gate; mapper events are not its authority source."""
    from sqlalchemy import select
    from sqlalchemy.sql.dml import Delete, Insert, Update
    from sqlalchemy.sql.selectable import Alias
    from modules.wms.physical_models import PhysicalOperation, _cap_fail
    if not isinstance(clause, (Insert, Update, Delete)):
        return
    table = clause.table
    if isinstance(table, Alias) and table.original in (placement_work_policy_table, PhysicalOperation.__table__):
        _cap_fail("work_core_where_table_unknown")
    policy = table.name == placement_work_policy_table.name and table.schema == placement_work_policy_table.schema
    operation = table.name == PhysicalOperation.__table__.name and table.schema == PhysicalOperation.__table__.schema
    if not policy and not operation:
        return
    canonical_table = placement_work_policy_table if policy else PhysicalOperation.__table__
    if table._deannotate() is not canonical_table:
        _cap_fail("work_core_where_table_unknown")
    # These compound DML forms can carry hidden rows or UPDATE assignments.
    # They cannot use the plain-row producer route or legacy nonwork exception.
    if isinstance(clause, Insert):
        if clause._multi_values:
            _cap_fail("work_core_multi_values_forbidden")
        if clause._prefixes:
            _cap_fail("work_core_insert_prefix_forbidden")
        if getattr(clause, "_post_values_clause", None) is not None:
            _cap_fail("work_core_upsert_forbidden")
    if isinstance(clause, Update) and clause._ordered_values:
        _cap_fail("work_core_ordered_update_forbidden")
    if isinstance(clause, (Update, Delete)):
        if policy:
            _cap_fail("work_policy_core_immutable")
        batches = list(multiparams) if multiparams else [params]
        # Normalize the whole batch before any target SELECT/native DML.
        predicates, targets, entries = _work_core_targets(clause, batches)
        changes = [_work_core_values(entries, {key: value for key, value in supplied_params.items()
            if key not in target}) for supplied_params, target in zip(batches, targets)] if isinstance(clause, Update) else []
        rows = []
        for target in targets:
            rows.extend(connection.execute(select(PhysicalOperation.__table__).where(*predicates), target).mappings().all())
        if any(work_operation_indicator(row["kind"], row["result"]) for row in rows):
            _cap_fail("work_receipt_core_immutable")
        # A generic receipt cannot be converted into private metadata.
        for changed in changes:
            if work_operation_indicator(changed.get("kind"), changed.get("result")):
                _cap_fail("work_receipt_core_type_mutation")
            if "kind" in changed and type(changed["kind"]) is not str:
                _cap_fail("work_receipt_core_kind_expression_forbidden")
            if "result" in changed and type(changed["result"]) is not dict:
                _cap_fail("work_receipt_core_result_expression_forbidden")
        return
    batches = list(multiparams) if multiparams else [params]
    if operation and getattr(clause, "select", None) is not None:
        _cap_fail("work_receipt_insert_select_forbidden")
    if policy and getattr(clause, "select", None) is not None:
        _cap_fail("work_policy_insert_select_forbidden")
    token = connection.info.get(_WORK_CONNECTION)
    session = None if token is None else token[0]()
    obligations = {} if session is None or token[1] is not session.get_transaction() else _work_obligations(session)
    for parameters in batches:
        values = _work_core_values((clause._values or {}).items(), parameters)
        if not policy and (type(values.get("kind")) is not str or type(values.get("result")) is not dict):
            _cap_fail("work_core_receipt_classification_unknown")
        if not policy and not work_operation_indicator(values.get("kind"), values.get("result")):
            continue
        key = (values.get("organization_id"), values.get("warehouse_id"),
               values.get("approval_operation_id") if policy else values.get("operation_id"))
        item = obligations.get(key)
        if item is None or not item["insert"]:
            _cap_fail("work_core_insert_without_verified_producer")
        import json
        expected = json.loads(item["row"] if policy else item["receipt"])
        if policy:
            if _work_json(values) != item["row"]:
                _cap_fail("work_core_policy_insert_mismatch")
        elif any(key not in values or _work_json({key: values[key]}) != _work_json({key: value}) for key, value in expected.items()):
            _cap_fail("work_core_receipt_insert_mismatch")


def _work_driver_guard(connection, cursor, statement, parameters, context, executemany):
    # Text/driver DML cannot carry a typed producer obligation. The PG trigger
    # remains a mandatory separate migration for other database connections.
    import re
    from modules.wms.physical_models import _cap_fail
    if context.compiled is None or not hasattr(context.compiled.statement, "table"):
        if re.search(r"\b(?:insert\s+into|update|delete\s+from)\s+(?:[\w\".]+\.)?\"?(?:physical_placement_work_policy|physical_operation)\b", statement, re.I):
            _cap_fail("work_untyped_sql_write_forbidden")


def cap_work_before_commit(session):
    import inspect
    from modules.wms.physical_models import _cap_fail
    _work_assert_loaded(session)
    obligations = _work_obligations(session)
    if not obligations:
        return
    if session._flushing:
        _cap_fail("work_commit_during_flush")
    session.flush()
    if session.get_nested_transaction() is not None:
        return
    for item in obligations.values():
        for authority in item["authorities"]:
            if inspect.iscoroutinefunction(authority):
                if session.info.get("wms_work_final_authority_v1") != session.get_transaction():
                    _cap_fail("work_async_final_authority_required")
            else:
                value = authority()
                if inspect.isawaitable(value):
                    value.close()
                    _cap_fail("work_async_final_authority_required")
    cap_validate_work_root(session)
    if session.new or session.dirty or session.deleted:
        _cap_fail("work_changes_after_final_guard")


def cap_work_transaction_end(session, transaction, state, committed):
    from modules.wms.physical_models import _cap_fail
    entries = state.setdefault("work_levels", {}).pop(transaction, {})
    if committed and entries:
        if transaction.parent is None:
            _cap_fail("work_nested_parent_missing")
        parent = state["work_levels"].setdefault(transaction.parent, {})
        for key, item in entries.items():
            if key not in parent:
                parent[key] = {**item, "authorities": list(item["authorities"])}
                continue
            outer = parent[key]
            if any(outer[name] != item[name] for name in ("row", "receipt", "insert")):
                _cap_fail("work_nested_obligation_conflict")
            if outer["persistent_pk"] is not None and item["persistent_pk"] is not None and outer["persistent_pk"] != item["persistent_pk"]:
                _cap_fail("work_nested_receipt_identity_conflict")
            if outer["persistent_pk"] is None:
                outer["persistent_pk"] = item["persistent_pk"]
            outer["authorities"].extend(item["authorities"])


async def cap_finish_work_root(async_session):
    import inspect
    from modules.wms.physical_models import _cap_fail
    session = async_session.sync_session
    if not _work_obligations(session):
        return
    await async_session.flush()
    with async_session.no_autoflush:
        for item in _work_obligations(session).values():
            for authority in item["authorities"]:
                result = authority()
                if inspect.isawaitable(result):
                    await result
    await async_session.run_sync(cap_validate_work_root)
    with async_session.no_autoflush:
        for item in _work_obligations(session).values():
            for authority in item["authorities"]:
                result = authority()
                if inspect.isawaitable(result):
                    await result
    _work_assert_loaded(session)
    if session.new or session.dirty or session.deleted:
        _cap_fail("work_changes_after_final_authority")
    session.info["wms_work_final_authority_v1"] = session.get_transaction()


from sqlalchemy import event as _work_event  # noqa: E402
from sqlalchemy.engine import Engine as _WorkEngine  # noqa: E402
from modules.wms.physical_models import PhysicalOperation as _WorkOperation  # noqa: E402

_work_event.listen(PlacementWorkPolicy, "before_insert", _work_before_insert)
_work_event.listen(PlacementWorkPolicy, "after_insert", _work_after_insert)
_work_event.listen(PlacementWorkPolicy, "before_update", _work_immutable)
_work_event.listen(PlacementWorkPolicy, "before_delete", _work_immutable)
_work_event.listen(PlacementWorkPolicy, "load", _work_loaded)
_work_event.listen(PlacementWorkPolicy, "refresh", _work_loaded)
_work_event.listen(_WorkOperation, "load", _work_receipt_loaded)
_work_event.listen(_WorkOperation, "refresh", _work_receipt_loaded)
_work_event.listen(_WorkOperation, "after_insert", _work_after_insert)
_work_event.listen(_WorkOperation, "before_insert", _work_before_insert)
_work_event.listen(_WorkEngine, "before_execute", _work_core_before_execute)
_work_event.listen(_WorkEngine, "before_cursor_execute", _work_driver_guard)


_PLACEMENT_CONNECTION = "wms_placement_native_producer_session_v1"
_PLACEMENT_OLD = "wms_placement_original_header_v1"


def placement_operation_indicator(kind, result):
    kinds = {"capacity_placement_assign", "capacity_placement_begin", "capacity_placement_complete", "capacity_placement_cancel"}
    markers = {"_placement_assign_v1", "_placement_begin_v1", "_placement_complete_v1", "_placement_cancel_v1"}
    # Private key presence is authoritative even when sibling kinds are null,
    # lists or mappings; do not hash an untrusted read-boundary value.
    if isinstance(result, dict) and markers.intersection(result):
        return True
    return (isinstance(kind, str) and kind in kinds) or (
        isinstance(result, dict) and isinstance(result.get("kind"), str) and result["kind"] in kinds)


def _placement_obligations(session):
    from modules.wms.physical_models import _cap_tracking
    state = _cap_tracking(session, create=False)
    if state is None:
        return {}
    result = {}
    for entries in state.get("placement_levels", {}).values():
        result.update(entries)
    return result


def cap_freeze_placement(session, receipt, *, authority, insert, rows=(), deleted=()):
    import json
    import weakref
    from sqlalchemy import inspect
    from modules.wms.physical_models import _cap_fail, _cap_tracking
    from modules.wms.physical_capacity import _observation_row
    from modules.wms.placement_service import validate_placement_envelope
    validate_placement_envelope(receipt)
    if not callable(authority) or type(insert) is not bool:
        _cap_fail("placement_verified_authority_callback_required")
    state = _cap_tracking(session, create=True)
    level = session.get_nested_transaction() or session.get_transaction()
    key = (receipt["organization_id"], receipt["warehouse_id"], receipt["operation_id"])
    expected = _work_json({name: value for name, value in receipt.items() if name != "id"})
    inherited = _placement_obligations(session).get(key)
    if inherited is not None and inherited["receipt"] != expected:
        _cap_fail("placement_producer_obligation_conflict")
    values = {}
    for target in rows:
        table = target.__table__
        pk = tuple(getattr(target, column.name) for column in table.primary_key)
        values[(table.name, pk)] = {"table": table, "pk": pk, "row": _work_json(_observation_row(target))}
    entries = state.setdefault("placement_levels", {}).setdefault(level, {})
    # A replay in this TX cannot discard an earlier producer's final-state
    # obligation, including when a committed child level is inherited.
    old = inherited or {}
    entries[key] = {"receipt": expected, "insert": insert or old.get("insert", False),
                    "persistent_pk": old.get("persistent_pk") or receipt.get("id"),
                    "authorities": [*old.get("authorities", ()), authority],
                    "rows": {**old.get("rows", {}), **values},
                    "deleted": [*old.get("deleted", ()), *json.loads(_work_json({"deleted": list(deleted)}))["deleted"]]}
    session.info.pop("wms_placement_final_authority_v1", None)
    connection = session.connection(bind_arguments={"mapper": PlacementAssignment})
    connection.info[_PLACEMENT_CONNECTION] = (weakref.ref(session), state["root"])
    # Keep the original immutable159 body on load/flush without allowing refresh
    # to replace it. Mutable lifecycle columns are checked by root obligations.
    for target in rows:
        if isinstance(target, PlacementAssignment):
            inspected = inspect(target)
            inspected.info.setdefault(_PLACEMENT_OLD, _placement_header_json(target))


def _placement_immutable_keys():
    return {column.name for column in PlacementAssignment.__table__.columns} - {
        "state", "active_carrier_id", "begin_operation_id", "settlement_operation_id"}


def _placement_header_json(target):
    from sqlalchemy import Numeric
    from modules.wms.physical_models import _cap_fixed, _cap_fail, _cap_timestamp_text
    passive = _work_passive(target)
    if not _placement_immutable_keys().issubset(passive):
        _cap_fail("placement_assignment_passive_header_incomplete")
    values = {name: passive[name] for name in _placement_immutable_keys()}
    if not isinstance(values['assigned_at'], str):
        values['assigned_at'] = _cap_timestamp_text(values['assigned_at'])
    for column in PlacementAssignment.__table__.columns:
        if column.name in values and isinstance(column.type, Numeric) and values[column.name] is not None:
            values[column.name] = _cap_fixed(values[column.name])
    return _work_json(values)


def _placement_loaded(target, context, attrs=None):
    from sqlalchemy import inspect
    values = _work_passive(target)
    # Empty model fixtures remain DB constraint tests, never accepted159 facts.
    if values.get("snapshot"):
        inspect(target).info.setdefault(_PLACEMENT_OLD, _placement_header_json(target))


def _placement_assert_loaded(session):
    from sqlalchemy import inspect
    from modules.wms.physical_models import _cap_fail
    for target in tuple(session.identity_map.values()) + tuple(session.new):
        if isinstance(target, PlacementAssignment):
            original = inspect(target).info.get(_PLACEMENT_OLD)
            if original is not None and _placement_header_json(target) != original:
                _cap_fail("placement_assignment_immutable_snapshot")


def _placement_before_insert(mapper, connection, target):
    from sqlalchemy import inspect
    from modules.wms.physical_models import _cap_fail
    if not placement_operation_indicator(target.kind, target.result):
        return
    session = inspect(target).session
    item = None if session is None else _placement_obligations(session).get((target.organization_id, target.warehouse_id, target.operation_id))
    if item is None or not item["insert"]:
        _cap_fail("placement_receipt_insert_without_verified_producer")
    values = {column.name: getattr(target, column.name) for column in target.__table__.columns
              if column.name not in {"id", "created_at"}}
    if _work_json(values) != item["receipt"]:
        _cap_fail("placement_receipt_insert_mismatch")


def _placement_after_insert(mapper, connection, target):
    from sqlalchemy import inspect
    from modules.wms.physical_models import _cap_tracking
    if not placement_operation_indicator(target.kind, target.result):
        return
    session = inspect(target).session
    state = _cap_tracking(session, create=False)
    for entries in state.get("placement_levels", {}).values():
        item = entries.get((target.organization_id, target.warehouse_id, target.operation_id))
        if item is not None:
            item["persistent_pk"] = target.id


def _placement_core_before_execute(connection, clause, multiparams, params, execution_options):
    """Use the established exact native DML/bind guards before any new probe."""
    import json
    from sqlalchemy import select
    from sqlalchemy.sql.dml import Delete, Insert, Update
    from sqlalchemy.sql.selectable import Alias
    from modules.wms.physical_models import PhysicalLedger, PhysicalOperation, _cap_fail
    if isinstance(clause, (Insert, Update, Delete)) and isinstance(clause.table, Alias) and clause.table.original in (
            PhysicalLedger.__table__, PhysicalOperation.__table__, PlacementAssignment.__table__):
        _cap_fail("work_core_where_table_unknown")
    if isinstance(clause, (Insert, Update, Delete)) and (clause.table.name, clause.table.schema) == (
            PlacementAssignment.__table__.name, PlacementAssignment.__table__.schema):
        _placement_assignment_core(connection, clause, list(multiparams) if multiparams else [params])
        return
    if isinstance(clause, (Insert, Update, Delete)) and (clause.table.name, clause.table.schema) == (
            PhysicalLedger.__table__.name, PhysicalLedger.__table__.schema):
        if clause.table._deannotate() is not PhysicalLedger.__table__:
            _cap_fail("work_core_where_table_unknown")
        batches = list(multiparams) if multiparams else [params]
        if isinstance(clause, Insert):
            if clause._multi_values or clause._prefixes or getattr(clause, "_post_values_clause", None) is not None or getattr(clause, "select", None) is not None:
                _cap_fail("placement_ledger_compound_insert_forbidden")
            values = [_work_core_values((clause._values or {}).items(), supplied) for supplied in batches]
            # Validate every bind/value before the first parent/protected SQL.
            if any(type(value.get("operation_pk")) is not int for value in values):
                _cap_fail("placement_ledger_parent_binding_unknown")
            _placement_check_ledger_inserts(connection, values)
        else:
            if isinstance(clause, Update) and clause._ordered_values:
                _cap_fail("work_core_ordered_update_forbidden")
            predicates, targets, entries = _work_core_targets(clause, batches, native_table=PhysicalLedger.__table__)
            changes = [_work_core_values(entries, {key: value for key, value in supplied.items() if key not in target})
                for supplied, target in zip(batches, targets)] if isinstance(clause, Update) else [{} for _ in batches]
            # Normalize ALL effective incoming parents before the first probe.
            if any("operation_pk" in changed and type(changed["operation_pk"]) is not int for changed in changes):
                _cap_fail("placement_ledger_parent_binding_unknown")
            rows = []
            for target, changed in zip(targets, changes):
                rows.extend((dict(row), changed) for row in connection.execute(
                    select(PhysicalLedger.__table__).where(*predicates), target).mappings())
            for row, changed in rows:
                parents = {row["operation_pk"], changed.get("operation_pk", row["operation_pk"])}
                for parent in parents:
                    op = connection.execute(select(PhysicalOperation.__table__).where(PhysicalOperation.__table__.c.id == parent)).mappings().one_or_none()
                    if op is not None and (placement_operation_indicator(op["kind"], op["result"]) or work_operation_indicator(op["kind"], op["result"])):
                        _cap_fail("placement_ledger_core_immutable")
        return
    if not isinstance(clause, (Insert, Update, Delete)) or (clause.table.name, clause.table.schema) != (
            PhysicalOperation.__table__.name, PhysicalOperation.__table__.schema):
        return
    if clause.table._deannotate() is not PhysicalOperation.__table__:
        _cap_fail("work_core_where_table_unknown")
    batches = list(multiparams) if multiparams else [params]
    if isinstance(clause, (Update, Delete)):
        predicates, targets, entries = _work_core_targets(clause, batches)
        changes = [_work_core_values(entries, {key: value for key, value in supplied.items() if key not in target})
                   for supplied, target in zip(batches, targets)] if isinstance(clause, Update) else []
        if any(placement_operation_indicator(value.get("kind"), value.get("result")) for value in changes):
            _cap_fail("placement_receipt_core_type_mutation")
        rows = []
        for target in targets:
            rows.extend(connection.execute(select(clause.table).where(*predicates), target).mappings().all())
        if any(placement_operation_indicator(row["kind"], row["result"]) for row in rows):
            _cap_fail("placement_receipt_core_immutable")
        return
    normalized = [_work_core_values((clause._values or {}).items(), supplied) for supplied in batches]
    token = connection.info.get(_PLACEMENT_CONNECTION)
    session = None if token is None else token[0]()
    obligations = {} if session is None or token[1] is not session.get_transaction() else _placement_obligations(session)
    for values in normalized:
        if not placement_operation_indicator(values.get("kind"), values.get("result")):
            continue
        item = obligations.get((values.get("organization_id"), values.get("warehouse_id"), values.get("operation_id")))
        if item is None or not item["insert"]:
            _cap_fail("placement_core_insert_without_verified_producer")
        expected = json.loads(item["receipt"])
        if any(name not in values or _work_json({name: values[name]}) != _work_json({name: value}) for name, value in expected.items()):
            _cap_fail("placement_core_receipt_insert_mismatch")


def _placement_assignment_core(connection, clause, batches):
    """Native159 rows: immutable body and producer-only mutable transitions."""
    from sqlalchemy import select
    from sqlalchemy.sql.dml import Delete, Insert, Update
    from modules.wms.physical_models import _cap_fail
    from modules.wms.physical_capacity import _measurement_db_row
    table = PlacementAssignment.__table__
    if clause.table._deannotate() is not table:
        _cap_fail("work_core_where_table_unknown")
    token = connection.info.get(_PLACEMENT_CONNECTION)
    session = None if token is None else token[0]()
    obligations = {} if session is None or token[1] is not session.get_transaction() else _placement_obligations(session)
    expected_rows = {}
    for item in obligations.values():
        expected_rows.update(item['rows'])
    if isinstance(clause, Insert):
        if clause._multi_values or clause._prefixes or getattr(clause, '_post_values_clause', None) is not None or getattr(clause, 'select', None) is not None:
            _cap_fail('placement_assignment_compound_insert_forbidden')
        normalized = [_work_core_values((clause._values or {}).items(), supplied) for supplied in batches]
        if any(type(values.get('snapshot')) is not dict for values in normalized):
            _cap_fail('placement_assignment_core_snapshot_unknown')
        for values in normalized:
            if not values['snapshot']:
                # Existing schema-only constraint fixtures remain persistable;
                # decode_active_assignments rejects them as unproven159 facts.
                continue
            expected = expected_rows.get((table.name, (values.get('task_id'),)))
            if expected is None or _work_json(_measurement_db_row(PlacementAssignment, values)) != expected['row']:
                _cap_fail('placement_assignment_insert_without_verified_producer')
        return
    if isinstance(clause, Update) and clause._ordered_values:
        _cap_fail('work_core_ordered_update_forbidden')
    predicates, targets, entries = _work_core_targets(clause, batches, native_table=table)
    changes = [_work_core_values(entries, {key: value for key, value in supplied.items() if key not in target})
        for supplied, target in zip(batches, targets)] if isinstance(clause, Update) else [{} for _ in batches]
    rows = []
    for target, changed in zip(targets, changes):
        rows.extend((dict(row), changed) for row in connection.execute(select(table).where(*predicates), target).mappings())
    for row, changed in rows:
        effective = {**row, **changed}
        if type(effective.get('snapshot')) is not dict:
            _cap_fail('placement_assignment_core_snapshot_unknown')
        if not row['snapshot']:
            if effective['snapshot']:
                expected = expected_rows.get((table.name, (effective['task_id'],)))
                if expected is None or _work_json(_measurement_db_row(PlacementAssignment, effective)) != expected['row']:
                    _cap_fail('placement_assignment_insert_without_verified_producer')
            continue
        if isinstance(clause, Delete) or _placement_immutable_keys().intersection(changed):
            _cap_fail('placement_assignment_core_immutable')
        expected = expected_rows.get((table.name, (row['task_id'],)))
        if expected is None or _work_json(_measurement_db_row(PlacementAssignment, {**row, **changed})) != expected['row']:
            _cap_fail('placement_assignment_transition_without_verified_producer')


def _placement_check_ledger_inserts(connection, values):
    import json
    from collections import Counter
    from decimal import Decimal
    from sqlalchemy import select
    from modules.wms.physical_models import PhysicalLedger, PhysicalOperation, _cap_fail
    from modules.wms.physical_stock import canonical
    from modules.wms.placement_service import _native_dimensions
    token = connection.info.get(_PLACEMENT_CONNECTION)
    session = None if token is None else token[0]()
    obligations = {} if session is None or token[1] is not session.get_transaction() else _placement_obligations(session)
    groups = {}
    for value in values:
        groups.setdefault(value["operation_pk"], []).append(value)
    for operation_pk, entries in groups.items():
        table = PhysicalOperation.__table__
        op = connection.execute(select(table).where(table.c.id == operation_pk)).mappings().one_or_none()
        if op is not None and work_operation_indicator(op["kind"], op["result"]):
            # A WorkApprove is a verified whole zero-ledger event, never a
            # quantity-producing operation (249). Legacy quantity rows remain.
            _cap_fail("work_receipt_ledger_forbidden")
        if op is None or not placement_operation_indicator(op["kind"], op["result"]):
            continue
        item = obligations.get((op["organization_id"], op["warehouse_id"], op["operation_id"]))
        if item is None or not item["insert"] or op["kind"] != "capacity_placement_complete":
            _cap_fail("placement_ledger_insert_without_verified_producer")
        proof = json.loads(item["receipt"])["result"]["_placement_complete_v1"]
        expected = []
        for source, moved in zip(proof["source_before"]["members"], proof["destination_after"]["members"]):
            quantity = Decimal(source["qty"])
            if quantity > 0:
                expected.extend((canonical({"unit_id": source["id"], "qty_delta": format(amount, ".6f"),
                    "dimensions": {**_native_dimensions(unit), "carrier_id": proof["source_before"]["carrier"]["id"]}})
                    for amount, unit in ((-quantity, source), (quantity, moved))))
        ledger = PhysicalLedger.__table__
        persisted = connection.execute(select(ledger).where(ledger.c.operation_pk == operation_pk)).mappings().all()
        supplied = [*persisted, *entries]
        encoded = [canonical({"unit_id": value["unit_id"], "qty_delta": format(Decimal(value["qty_delta"]), ".6f"),
                              "dimensions": value["dimensions"]}) for value in supplied]
        if Counter(encoded) - Counter(expected):
            _cap_fail("placement_ledger_insert_packet_mismatch")


def cap_validate_placement_root(session):
    import json
    from sqlalchemy import select
    from modules.wms.physical_models import CarrierPosition, PhysicalOperation, _cap_fail, _cap_require_read_committed
    from modules.wms.physical_capacity import _measurement_db_row
    from modules.wms.placement_service import validate_placement_receipt, placement_measurement_current_view, _measurement_binding
    obligations = _placement_obligations(session)
    if not obligations:
        return
    _placement_assert_loaded(session)
    connection = session.connection(bind_arguments={"mapper": PlacementAssignment})
    _cap_require_read_committed(connection)
    current_rows, deletions, measurements = {}, {}, {}
    for key, item in obligations.items():
        table = PhysicalOperation.__table__
        receipt = connection.execute(select(table).where(table.c.organization_id == key[0], table.c.warehouse_id == key[1],
            table.c.operation_id == key[2])).mappings().one_or_none()
        if receipt is None or receipt["id"] != item["persistent_pk"]:
            _cap_fail("placement_root_receipt_identity")
        expected = json.loads(item["receipt"])
        if any(_work_json({name: receipt[name]}) != _work_json({name: value}) for name, value in expected.items()):
            _cap_fail("placement_root_receipt_mutated_or_resealed")
        proof = validate_placement_receipt(connection, dict(receipt))
        if proof["kind"] == "capacity_placement_assign":
            work = proof["assignments"][0]["snapshot"]["bindings"]["work_policy"]
        else:
            header = connection.execute(select(PlacementAssignment.__table__).where(
                PlacementAssignment.__table__.c.task_id == proof["task_id"])).mappings().one()
            work = header["snapshot"]["bindings"]["work_policy"]
        measured = _measurement_binding(work)
        if item["insert"]:
            # Fresh producer rows must match the current final folded state.
            measurements[(measured["organization_id"], measured["warehouse_id"], measured["operation_id"])] = measured
        else:
            # Replay validates only the historical prefix before its ORIGINAL
            # persisted PK; a later real movement is not a stale replay.
            placement_measurement_current_view(connection, measured, before_pk=receipt["id"], current=False)
        current_rows.update(item["rows"])
        for deletion in item["deleted"]:
            deletions[(deletion["table"], tuple(deletion["key"]))] = deletion
    for (table_name, pk), item in current_rows.items():
        if (table_name, pk) in deletions:
            continue
        table = item["table"]
        row = connection.execute(select(table).where(*(column == value for column, value in zip(table.primary_key, pk)))).mappings().one_or_none()
        if row is None:
            _cap_fail("placement_root_native_row_missing")
        # Use the existing native type/timestamp formatter, including Numeric
        # fixed precision and full JSON columns. No default=str coercion.
        from modules.wms.physical_models import StockUnit
        if table is StockUnit.__table__:
            model = StockUnit
        else:
            class Columns:
                __table__ = table
            model = Columns
        if _work_json(_measurement_db_row(model, row)) != item["row"]:
            _cap_fail("placement_root_native_row_mismatch")
    tables = {PlacementClaim.__table__.name: PlacementClaim.__table__, CarrierPosition.__table__.name: CarrierPosition.__table__}
    for table_name, pk in deletions:
        table = tables[table_name]
        if connection.execute(select(table).where(*(column == value for column, value in zip(table.primary_key, pk)))).first() is not None:
            _cap_fail("placement_root_native_delete_missing")
    for measured in measurements.values():
        placement_measurement_current_view(connection, measured)


def cap_placement_before_commit(session):
    from modules.wms.physical_models import _cap_fail
    _placement_assert_loaded(session)
    if not _placement_obligations(session):
        return
    if session._flushing:
        _cap_fail("placement_commit_during_flush")
    session.flush()
    if session.get_nested_transaction() is not None:
        return
    if session.info.get("wms_placement_final_authority_v1") is not session.get_transaction():
        _cap_fail("placement_async_final_authority_required")
    cap_validate_placement_root(session)
    if session.new or session.dirty or session.deleted:
        _cap_fail("placement_changes_after_final_guard")


def cap_placement_transaction_end(session, transaction, state, committed):
    from modules.wms.physical_models import _cap_fail
    entries = state.setdefault("placement_levels", {}).pop(transaction, {})
    if committed and entries:
        if transaction.parent is None:
            _cap_fail("placement_nested_parent_missing")
        parent = state["placement_levels"].setdefault(transaction.parent, {})
        for key, item in entries.items():
            if key in parent and parent[key]["receipt"] != item["receipt"]:
                _cap_fail("placement_nested_obligation_conflict")
            parent[key] = item


async def cap_finish_placement_root(async_session):
    from modules.wms.physical_models import _cap_fail
    session = async_session.sync_session
    if not _placement_obligations(session):
        return
    await async_session.flush()
    for item in _placement_obligations(session).values():
        for authority in item["authorities"]:
            await authority()
    await async_session.run_sync(cap_validate_placement_root)
    for item in _placement_obligations(session).values():
        for authority in item["authorities"]:
            await authority()
    _placement_assert_loaded(session)
    if session.new or session.dirty or session.deleted:
        _cap_fail("placement_changes_after_final_authority")
    session.info["wms_placement_final_authority_v1"] = session.get_transaction()


_work_event.listen(PlacementAssignment, "load", _placement_loaded)
_work_event.listen(PlacementAssignment, "refresh", _placement_loaded)
_work_event.listen(_WorkOperation, "before_insert", _placement_before_insert)
_work_event.listen(_WorkOperation, "after_insert", _placement_after_insert)
_work_event.listen(_WorkEngine, "before_execute", _placement_core_before_execute)

def _native_historical_owner(session, transaction, connection):
    import weakref
    if not session.info.get('wms_native_historical_reader_v1'):
        connection.info['wms_native_historical_owner_v1'] = weakref.ref(session)

from sqlalchemy.orm import Session as _HistoricalSession  # noqa: E402
_work_event.listen(_HistoricalSession, 'after_begin', _native_historical_owner)
