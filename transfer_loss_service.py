"""Native transfer-discrepancy lifecycle over the existing physical ledger.

The route owns the transaction. This module never commits, registers services,
emits a report/return event, or creates exchange work.
"""
from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal

from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select

from modules.wms.transfer_loss_protocol import (
    CONFIRM_KIND,
    LOSS_KINDS,
    REPORT_KIND,
    RETURN_KIND,
    LossReceiptError,
    TransferLossCommand,
    TransferLossReceipt,
    loss_operation_indicator,
    loss_request_hash,
    public_loss_result,
    recorded_receipt,
)

_COMMAND_ADAPTER = TypeAdapter(TransferLossCommand)
_RECEIPT_ADAPTER = TypeAdapter(TransferLossReceipt)


def _conflict(message="Доказательство межскладской недостачи не подтверждено"):
    raise HTTPException(409, message)


def _origin_failure():
    from modules.wms.item_identity import _OriginUnproven

    raise _OriginUnproven


def _operation_snapshot(operation):
    from modules.wms.item_identity import _origin_operation_snapshot

    return deepcopy(_origin_operation_snapshot(operation))


def _actor_seed(stock, actor):
    seed = getattr(stock, "_caller_seed", None)
    if (
        not isinstance(seed, tuple)
        or len(seed) < 8
        or type(seed[7]) is not int
        or not 1 <= seed[7] <= 2147483647
        or not isinstance(seed[2], str)
        or not seed[2].strip()
        or actor != seed[2]
        or not isinstance(seed[1], tuple)
        or "wms_mobile" in seed[1]
    ):
        raise HTTPException(403, "Требуется управляемая нативная учётная запись")
    return seed[7], seed[2]


def _ids(command):
    from modules.wms.item_identity import _origin_uuid_text

    operation_id = str(command.operation_id)
    if not _origin_uuid_text(operation_id):
        _conflict("Некорректный идентификатор физической операции")
    return operation_id


def _typed_command(data):
    try:
        command = _COMMAND_ADAPTER.validate_python(data)
        wire = command.model_dump_json()
        command_json = json.loads(wire)
        command = _COMMAND_ADAPTER.validate_json(wire)
    except (ValidationError, TypeError, ValueError) as exc:
        raise HTTPException(422, "Недопустимая команда межскладской недостачи") from exc
    return command, command_json


def _mobile_report_context(stock, organization_id, warehouse_id):
    from modules.wms.mobile_access import (
        OriginalMobileReceivingSourceAuthority,
        OriginalMobileTransferAuthority,
        VerifiedProof,
    )

    authority = getattr(stock, "mobile_transfer_authority", None)
    seed = getattr(stock, "_caller_seed", None)
    if (
        not isinstance(authority, OriginalMobileTransferAuthority)
        or authority.action != REPORT_KIND
        or not isinstance(authority.identity, OriginalMobileReceivingSourceAuthority)
        or not isinstance(authority.proof, VerifiedProof)
        or not isinstance(seed, tuple) or len(seed) < 8
        or seed[1] != ("wms_mobile",)
        or type(seed[7]) is not int or seed[7] < 1
        or not isinstance(seed[2], str) or not seed[2]
        or type(seed[6]) is not int
    ):
        raise HTTPException(403, "Мобильный доступ не подтверждён")
    identity, proof = authority.identity, authority.proof
    if (
        type(organization_id) is not int or organization_id < 1
        or type(warehouse_id) is not int or warehouse_id < 1
        or identity.organization_id != organization_id
        or identity.warehouse_id != warehouse_id
        or identity.original_user_id != seed[7]
        or identity.original_sub != seed[2]
        or identity.bearer_issued_at != seed[6]
        or identity.roles != seed[1]
        or proof.original_user_id != identity.original_user_id
        or proof.original_sub != identity.original_sub
    ):
        raise HTTPException(403, "Мобильный доступ не подтверждён")
    return authority, identity, proof


async def _fresh_mobile_report_authority(
    stock, session, organization_id, warehouse_id, actor, *, receipt=False,
):
    authority, identity, _ = _mobile_report_context(
        stock, organization_id, warehouse_id,
    )
    if actor != identity.original_sub:
        raise HTTPException(403, "Мобильный доступ не подтверждён")
    await stock._fresh_transfer_authority(
        session, organization_id, actor, receipt=receipt,
    )
    return authority, identity


async def _warehouse_operations(session, organization_id, warehouse_ids):
    from modules.wms.physical_models import PhysicalOperation

    identifiers = tuple(sorted(set(warehouse_ids)))
    if not identifiers:
        _origin_failure()
    rows = (await session.scalars(
        select(PhysicalOperation)
        .where(
            PhysicalOperation.organization_id == organization_id,
            PhysicalOperation.warehouse_id.in_(identifiers),
        )
        .order_by(PhysicalOperation.id)
        .execution_options(populate_existing=True)
    )).all()
    return rows


def _loss_candidate(operation, organization_id):
    snapshot = _operation_snapshot(operation)
    causal, request, result = origin_validate_loss(snapshot, organization_id)
    receipt = recorded_receipt(snapshot)
    expected_warehouse = (
        receipt.destination_warehouse_id
        if receipt.kind == REPORT_KIND
        else receipt.source_warehouse_id
    )
    if (
        operation.organization_id != organization_id
        or operation.kind != receipt.kind
        or operation.warehouse_id != receipt.warehouse_id
        or receipt.warehouse_id != expected_warehouse
        or (
            receipt.kind == REPORT_KIND
            and receipt.operation_id != receipt.report_operation_id
        )
    ):
        _origin_failure()
    return {
        "operation": operation,
        "causal": causal,
        "request": request,
        "result": result,
        "receipt": receipt,
    }


def _validate_decision_report_link(decision, report):
    decision_receipt = decision["receipt"]
    report_receipt = report["receipt"]
    report_operation = report["operation"]
    if (
        report_receipt.kind != REPORT_KIND
        or report_operation.kind != REPORT_KIND
        or report_operation.operation_id != str(report_receipt.operation_id)
        or report_operation.warehouse_id != report_receipt.destination_warehouse_id
        or report_receipt.operation_id != decision_receipt.report_operation_id
        or report_receipt.source_warehouse_id != decision_receipt.source_warehouse_id
        or report_receipt.destination_warehouse_id != decision_receipt.destination_warehouse_id
        or report_receipt.dispatch_operation_id != decision_receipt.dispatch_operation_id
        or report_receipt.source_unit_id != decision_receipt.source_unit_id
        or report_receipt.transit_unit_id != decision_receipt.transit_unit_id
        or report_receipt.container != decision_receipt.container
        or report_receipt.reported_qty != decision_receipt.reported_qty
    ):
        _origin_failure()


async def _loss_candidates_in_scope(
    session, organization_id, warehouse_ids, *, operations=None,
    historical=None, operation_pk_ceiling=None,
):
    """Strictly parse bounded warehouse candidates and immutable report references."""
    from modules.wms.physical_models import PhysicalOperation

    rows = operations
    if rows is None:
        rows = await _warehouse_operations(session, organization_id, warehouse_ids)
    scoped_by_id = {}
    candidates = []
    for operation in rows:
        if historical is not None and (
            type(operation.id) is not int
            or operation.id > operation_pk_ceiling
            or operation.id >= historical.before_pk
            or operation.organization_id != organization_id
            or operation.warehouse_id not in warehouse_ids
        ):
            _origin_failure()
        if historical is not None and operation.warehouse_id == historical.warehouse_id:
            historical.operation(operation)
        operation_id = str(operation.operation_id)
        if operation_id in scoped_by_id:
            _origin_failure()
        scoped_by_id[operation_id] = operation
        if loss_operation_indicator(operation.kind, operation.result):
            candidates.append(_loss_candidate(operation, organization_id))

    selected_ids = set(warehouse_ids)

    def in_selected_transfer(item):
        receipt = item["receipt"]
        return {receipt.source_warehouse_id, receipt.destination_warehouse_id}.issubset(selected_ids)

    relevant_candidates = (
        candidates if historical is None
        else [item for item in candidates if in_selected_transfer(item)]
    )
    candidates_by_id = {
        str(item["receipt"].operation_id): item for item in relevant_candidates
    }
    decision_ids = {
        str(item["receipt"].report_operation_id)
        for item in relevant_candidates
        if item["receipt"].kind in {RETURN_KIND, CONFIRM_KIND}
    }
    absent_ids = sorted(decision_ids.difference(scoped_by_id))
    fetched = {}
    if historical is not None:
        report_warehouses = {}
        for item in relevant_candidates:
            receipt = item["receipt"]
            if receipt.kind not in {RETURN_KIND, CONFIRM_KIND}:
                continue
            report_id = str(receipt.report_operation_id)
            report_warehouse = receipt.destination_warehouse_id
            if report_id in report_warehouses and report_warehouses[report_id] != report_warehouse:
                _origin_failure()
            report_warehouses[report_id] = report_warehouse
        for report_id in absent_ids:
            warehouse_id = report_warehouses.get(report_id)
            if warehouse_id not in selected_ids:
                _origin_failure()
            report_rows = (await session.scalars(
                select(PhysicalOperation)
                .where(
                    PhysicalOperation.organization_id == organization_id,
                    PhysicalOperation.warehouse_id == warehouse_id,
                    PhysicalOperation.operation_id == report_id,
                    PhysicalOperation.id <= operation_pk_ceiling,
                    PhysicalOperation.id < historical.before_pk,
                )
                .order_by(PhysicalOperation.id)
                .execution_options(populate_existing=True)
            )).all()
            if len(report_rows) != 1:
                _origin_failure()
            fetched[report_id] = report_rows[0]
    elif absent_ids:
        report_rows = (await session.scalars(
            select(PhysicalOperation)
            .where(
                PhysicalOperation.organization_id == organization_id,
                PhysicalOperation.operation_id.in_(absent_ids),
            )
            .order_by(PhysicalOperation.id)
            .execution_options(populate_existing=True)
        )).all()
        for operation in report_rows:
            operation_id = str(operation.operation_id)
            if operation_id in fetched:
                _origin_failure()
            fetched[operation_id] = operation

    referenced_reports = {}
    for decision in candidates:
        receipt = decision["receipt"]
        if historical is not None and not in_selected_transfer(decision):
            continue
        if receipt.kind not in {RETURN_KIND, CONFIRM_KIND}:
            continue
        report_id = str(receipt.report_operation_id)
        report = candidates_by_id.get(report_id)
        if report is None:
            operation = scoped_by_id.get(report_id) or fetched.get(report_id)
            if operation is None:
                _origin_failure()
            report = _loss_candidate(operation, organization_id)
        _validate_decision_report_link(decision, report)
        referenced_reports[report_id] = report

    receipts_by_dispatch = {}
    for item in relevant_candidates + list(referenced_reports.values()):
        receipt = item["receipt"]
        receipts_by_dispatch.setdefault(
            str(receipt.dispatch_operation_id), [],
        ).append(receipt)
    available_rows = dict(scoped_by_id)
    available_rows.update(fetched)
    absent_dispatch_ids = sorted(
        set(receipts_by_dispatch).difference(available_rows),
    )
    if historical is not None:
        dispatch_warehouses = {}
        for dispatch_id, receipts in receipts_by_dispatch.items():
            warehouses = {receipt.source_warehouse_id for receipt in receipts}
            if len(warehouses) != 1:
                _origin_failure()
            dispatch_warehouses[dispatch_id] = next(iter(warehouses))
        for dispatch_id in absent_dispatch_ids:
            warehouse_id = dispatch_warehouses.get(dispatch_id)
            if warehouse_id not in selected_ids:
                _origin_failure()
            dispatch_rows = (await session.scalars(
                select(PhysicalOperation)
                .where(
                    PhysicalOperation.organization_id == organization_id,
                    PhysicalOperation.warehouse_id == warehouse_id,
                    PhysicalOperation.operation_id == dispatch_id,
                    PhysicalOperation.id <= operation_pk_ceiling,
                    PhysicalOperation.id < historical.before_pk,
                )
                .order_by(PhysicalOperation.id)
                .execution_options(populate_existing=True)
            )).all()
            if len(dispatch_rows) != 1:
                _origin_failure()
            available_rows[dispatch_id] = dispatch_rows[0]
    elif absent_dispatch_ids:
        dispatch_rows = (await session.scalars(
            select(PhysicalOperation)
            .where(
                PhysicalOperation.organization_id == organization_id,
                PhysicalOperation.operation_id.in_(absent_dispatch_ids),
            )
            .order_by(PhysicalOperation.id)
            .execution_options(populate_existing=True)
        )).all()
        for operation in dispatch_rows:
            operation_id = str(operation.operation_id)
            if operation_id in available_rows:
                _origin_failure()
            available_rows[operation_id] = operation

    dispatches = {}
    for dispatch_id, receipts in receipts_by_dispatch.items():
        operation = available_rows.get(dispatch_id)
        if operation is None:
            _origin_failure()
        dispatch = _dispatch_parts(
            operation, organization_id, receipts=receipts,
        )
        if dispatch is None:
            _origin_failure()
        dispatches[dispatch_id] = dispatch

    return {
        "warehouse_rows": rows,
        "candidates": candidates,
        "referenced_reports": referenced_reports,
        "dispatches": dispatches,
    }


async def _loss_history(
    session, organization_id, *, warehouse_ids, target_warehouse_id=None,
    operations=None, historical=None, operation_pk_ceiling=None,
    validated_transfer_pair=None,
):
    """Validate closed candidate metadata and direct links, without replaying origins."""
    from modules.wms.item_identity import (
        _origin_qty,
        _origin_transfer_dimensions,
        _OriginUnproven,
    )
    from modules.wms.physical_models import PhysicalLedger, PhysicalOperation

    selected_ids = set(warehouse_ids)
    if historical is not None and (
        validated_transfer_pair is None
        or type(operation_pk_ceiling) is not int
        or not 0 < operation_pk_ceiling < historical.before_pk
        or validated_transfer_pair.get("source_warehouse_id") not in selected_ids
        or validated_transfer_pair.get("destination_warehouse_id") not in selected_ids
    ):
        raise _OriginUnproven
    discovery = await _loss_candidates_in_scope(
        session, organization_id, selected_ids, operations=operations,
        historical=historical, operation_pk_ceiling=operation_pk_ceiling,
    )
    all_candidates = discovery["candidates"]

    def relevant(item):
        receipt = item["receipt"]
        pair = {receipt.source_warehouse_id, receipt.destination_warehouse_id}
        return (
            pair.issubset(selected_ids)
            and (target_warehouse_id is None or target_warehouse_id in pair)
        )

    candidates = [
        item for item in all_candidates if relevant(item)
    ]
    by_id = {str(item["operation"].operation_id): item for item in candidates}
    if len(by_id) != len(candidates):
        raise _OriginUnproven

    ledgers_by_operation = {item["operation"].id: [] for item in candidates}
    if ledgers_by_operation:
        ledger_rows = (await session.scalars(
            select(PhysicalLedger)
            .where(PhysicalLedger.operation_pk.in_(ledgers_by_operation))
            .order_by(PhysicalLedger.id)
        )).all()
        for row in ledger_rows:
            if row.operation_pk not in ledgers_by_operation:
                raise _OriginUnproven
            ledgers_by_operation[row.operation_pk].append(row)

    dispatches = discovery["dispatches"]
    if historical is not None:
        bound_dispatch = dispatches.get(str(validated_transfer_pair["dispatch"]["operation_id"]))
        if (bound_dispatch is None
                or bound_dispatch["operation"].id != validated_transfer_pair["dispatch"]["id"]
                or bound_dispatch["operation"].warehouse_id != validated_transfer_pair["source_warehouse_id"]):
            raise _OriginUnproven
    assignments = {}
    decisions_by_report = {}
    reports_by_transit = {}
    report_kind = REPORT_KIND
    return_kind = RETURN_KIND
    confirm_kind = CONFIRM_KIND

    for item in candidates:
        operation = item["operation"]
        receipt = item["receipt"]
        kind = receipt.kind
        rows = ledgers_by_operation[operation.id]
        if kind in {report_kind, return_kind} and rows:
            raise _OriginUnproven
        if kind == confirm_kind:
            qty = _origin_qty(format(receipt.reported_qty, "f"))
            if (
                len(rows) != 1
                or rows[0].unit_id != str(receipt.transit_unit_id)
                or _origin_qty(format(rows[0].qty_delta, "f")) != -qty
            ):
                raise _OriginUnproven
            dimensions = _origin_transfer_dimensions(rows[0].dimensions)
            if (
                dimensions.get("warehouse_id") != receipt.source_warehouse_id
                or dimensions.get("stock_state") != "in_transit"
                or dimensions.get("dispatch_operation_id") != str(receipt.dispatch_operation_id)
            ):
                raise _OriginUnproven
        if kind == report_kind:
            reports_by_transit.setdefault(str(receipt.transit_unit_id), []).append(item)
        else:
            decisions_by_report.setdefault(str(receipt.report_operation_id), []).append(item)

    for item in candidates:
        receipt = item["receipt"]
        report_kind_item = receipt.kind == report_kind
        expected_warehouse = (
            receipt.destination_warehouse_id if report_kind_item
            else receipt.source_warehouse_id
        )
        if item["operation"].warehouse_id != expected_warehouse:
            raise _OriginUnproven

        dispatch_id = str(receipt.dispatch_operation_id)
        dispatch_item = dispatches.get(dispatch_id)
        if dispatch_item is None:
            raise _OriginUnproven

        if report_kind_item:
            if (
                receipt.operation_id != receipt.report_operation_id
                or receipt.warehouse_id != receipt.destination_warehouse_id
            ):
                raise _OriginUnproven
            continue

        report = by_id.get(str(receipt.report_operation_id))
        if report is None or report["receipt"].kind != report_kind:
            raise _OriginUnproven
        report_receipt = report["receipt"]
        if (
            item["operation"].id <= report["operation"].id
            or receipt.dispatch_operation_id != report_receipt.dispatch_operation_id
            or receipt.source_warehouse_id != report_receipt.source_warehouse_id
            or receipt.destination_warehouse_id != report_receipt.destination_warehouse_id
            or receipt.source_unit_id != report_receipt.source_unit_id
            or receipt.transit_unit_id != report_receipt.transit_unit_id
            or receipt.container != report_receipt.container
            or receipt.reported_qty != report_receipt.reported_qty
        ):
            raise _OriginUnproven
        decisions = decisions_by_report.setdefault(str(receipt.report_operation_id), [])
        if len(decisions) > 1:
            raise _OriginUnproven

        proof = getattr(receipt, "transfer_discrepancy_v1")
        assignment_id = str(proof.assignment_operation_id)
        assignment = assignments.get(assignment_id)
        if assignment is None:
            assignment_query = select(PhysicalOperation).where(
                PhysicalOperation.organization_id == organization_id,
                PhysicalOperation.warehouse_id == receipt.source_warehouse_id,
                PhysicalOperation.operation_id == assignment_id,
            )
            if historical is not None:
                assignment_query = assignment_query.where(
                    PhysicalOperation.id < item["operation"].id,
                    PhysicalOperation.id <= operation_pk_ceiling,
                    PhysicalOperation.id < historical.before_pk,
                )
            assignment_rows = (await session.scalars(assignment_query.order_by(
                PhysicalOperation.id,
            ).execution_options(populate_existing=True))).all()
            if len(assignment_rows) != 1:
                raise _OriginUnproven
            assignment_operation = assignment_rows[0]
            if assignment_operation.kind != "warehouse_responsible_set":
                raise _OriginUnproven
            try:
                from modules.wms.physical_stock import responsible_recorded_receipt

                assignment_receipt = responsible_recorded_receipt(assignment_operation)
            except HTTPException as exc:
                raise _OriginUnproven from exc
            assignment = (assignment_operation, assignment_receipt)
            assignments[assignment_id] = assignment
        assignment_operation, assignment_receipt = assignment
        assignment_proof = assignment_receipt.warehouse_responsible_v1
        if historical is not None:
            pair_warehouses = {
                validated_transfer_pair["source_warehouse_id"],
                validated_transfer_pair["destination_warehouse_id"],
            }
            decision_pk = item["operation"].id
            if (
                type(assignment_operation.id) is not int
                or assignment_operation.id <= 0
                or type(decision_pk) is not int
                or decision_pk <= 0
                or assignment_operation.organization_id != organization_id
                or receipt.source_warehouse_id not in pair_warehouses
                or not assignment_operation.id < decision_pk <= operation_pk_ceiling < historical.before_pk
            ):
                raise _OriginUnproven
        if await session.scalar(select(PhysicalLedger.id).where(
            PhysicalLedger.operation_pk == assignment_operation.id,
        ).limit(1)) is not None:
            raise _OriginUnproven
        if (
            assignment_operation.id >= item["operation"].id
            or assignment_operation.warehouse_id != receipt.source_warehouse_id
            or assignment_receipt.organization_id != organization_id
            or assignment_receipt.warehouse_id != receipt.source_warehouse_id
            or assignment_receipt.operation_id != proof.assignment_operation_id
            or assignment_receipt.active is not True
            or assignment_receipt.revision != proof.assignment_revision
            or assignment_receipt.user_id != proof.actor_user_id
            or assignment_proof.assignee_subject != proof.actor_sub
        ):
            raise _OriginUnproven
        reporter = report_receipt.transfer_discrepancy_v1
        if receipt.kind == confirm_kind and (
            proof.actor_user_id == reporter.actor_user_id
            or proof.actor_sub == reporter.actor_sub
        ):
            raise _OriginUnproven

    for transit_id, reports in reports_by_transit.items():
        reports.sort(key=lambda item: item["operation"].id)
        open_reports = 0
        for index, report in enumerate(reports):
            decisions = decisions_by_report.get(
                str(report["receipt"].operation_id), [],
            )
            if len(decisions) > 1:
                raise _OriginUnproven
            if decisions:
                decision = decisions[0]
                if decision["operation"].id <= report["operation"].id:
                    raise _OriginUnproven
                if (
                    index + 1 < len(reports)
                    and decision["operation"].id >= reports[index + 1]["operation"].id
                ):
                    raise _OriginUnproven
            else:
                open_reports += 1
                if index != len(reports) - 1:
                    raise _OriginUnproven
        if open_reports > 1:
            raise _OriginUnproven

    return {
        "candidates": candidates,
        "scoped_candidates": all_candidates,
        "warehouse_rows": discovery["warehouse_rows"],
        "referenced_reports": discovery["referenced_reports"],
        "by_id": by_id,
        "decisions_by_report": decisions_by_report,
        "reports_by_transit": reports_by_transit,
        "dispatches": dispatches,
        "assignments": assignments,
        "ledgers_by_operation": ledgers_by_operation,
    }


async def _prove_saved_frontier(
    session, organization_id, receipt, dispatch, *, tokens=None, trail=frozenset(), historical=None,
):
    from modules.wms.item_identity import (
        _origin_frontier_tokens,
        _origin_transfer_dimensions,
        _OriginUnproven,
        _prove_unit_boundary,
    )
    from modules.wms.physical_models import PhysicalLedger

    transit_id = str(receipt.transit_unit_id)
    saved_frontier = receipt.physical_causal_v1.prior_frontiers.get(transit_id)
    if saved_frontier is None:
        raise _OriginUnproven
    try:
        saved_tokens = _origin_frontier_tokens(saved_frontier)
        if tokens is not None and saved_tokens != tokens:
            raise _OriginUnproven
        boundary_tokens = tokens if tokens is not None else saved_tokens
    except (TypeError, ValueError) as exc:
        raise _OriginUnproven from exc

    if historical is not None and (dispatch["operation"].id >= historical.before_pk
            or dispatch["operation"].warehouse_id != receipt.source_warehouse_id):
        raise _OriginUnproven
    dispatch_rows = (await session.scalars(select(PhysicalLedger).where(
        PhysicalLedger.operation_pk == dispatch["operation"].id,
        PhysicalLedger.unit_id == transit_id,
    ).order_by(PhysicalLedger.id))).all()
    if len(dispatch_rows) != 1:
        raise _OriginUnproven
    dispatch_dimensions = _origin_transfer_dimensions(dispatch_rows[0].dimensions)
    if (
        dispatch_dimensions.get("stock_state") != "in_transit"
        or dispatch_dimensions.get("dispatch_operation_id")
        != str(dispatch["operation"].operation_id)
        or dispatch_dimensions.get("warehouse_id") != receipt.source_warehouse_id
        or dispatch_dimensions.get("container") != receipt.container
    ):
        raise _OriginUnproven

    boundary = await _prove_unit_boundary(
        session, organization_id, transit_id, boundary_tokens,
        current_unit=None, trail=trail, expected_state="in_transit", allow_zero=True,
        historical=historical,
    )
    dimensions = _origin_transfer_dimensions(boundary["dimensions"])
    if (
        dimensions.get("stock_state") != "in_transit"
        or dimensions.get("dispatch_operation_id")
        != str(dispatch["operation"].operation_id)
        or dimensions.get("warehouse_id") != receipt.source_warehouse_id
        or dimensions.get("container") != receipt.container
        or dimensions != dispatch_dimensions
        or boundary["qty"] != Decimal(receipt.remaining_before)
    ):
        raise _OriginUnproven
    return boundary


async def _prove_return_frontier(session, organization_id, decision, dispatch, *, trail=frozenset()):
    from modules.wms.item_identity import _OriginUnproven

    boundary = await _prove_saved_frontier(
        session, organization_id, decision["receipt"], dispatch, trail=trail,
    )
    receipt = decision["receipt"]
    if (
        receipt.kind != RETURN_KIND
        or boundary["qty"] != Decimal(receipt.remaining_before)
        or receipt.remaining_after != receipt.remaining_before
    ):
        raise _OriginUnproven
    return boundary


async def _prove_report_record(session, organization_id, report, history, *, trail=frozenset(), historical=None):
    from modules.wms.item_identity import _OriginUnproven

    if report["receipt"].kind != REPORT_KIND:
        raise _OriginUnproven
    dispatch = history["dispatches"].get(
        str(report["receipt"].dispatch_operation_id),
    )
    if dispatch is None:
        raise _OriginUnproven
    report_context = None
    if historical is not None:
        report_context = historical.before_warehouse(
            report["receipt"].source_warehouse_id, report["operation"].id,
        )
    boundary = await _prove_saved_frontier(
        session, organization_id, report["receipt"], dispatch, trail=trail,
        historical=report_context,
    )
    if report["receipt"].reported_qty > boundary["qty"]:
        raise _OriginUnproven
    return boundary, dispatch


async def _validate_saved_record(session, organization_id, operation, history, *, trail=frozenset()):
    from modules.wms.item_identity import _origin_frontier_tokens, _OriginUnproven

    item = history["by_id"].get(str(operation.operation_id))
    if item is None:
        raise _OriginUnproven
    receipt = item["receipt"]
    report = history["by_id"].get(str(receipt.report_operation_id)) if receipt.kind != REPORT_KIND else item
    if report is None:
        raise _OriginUnproven
    report_boundary, dispatch = await _prove_report_record(
        session, organization_id, report, history, trail=trail,
    )
    if receipt.kind == RETURN_KIND:
        await _prove_return_frontier(
            session, organization_id, item, dispatch, trail=trail,
        )
    elif receipt.kind == CONFIRM_KIND:
        try:
            report_tokens = _origin_frontier_tokens(
                report["receipt"].physical_causal_v1.prior_frontiers[
                    str(report["receipt"].transit_unit_id)
                ],
            )
            confirm_tokens = _origin_frontier_tokens(
                receipt.physical_causal_v1.prior_frontiers[str(receipt.transit_unit_id)],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise _OriginUnproven from exc
        if (
            confirm_tokens != report_tokens
            or receipt.remaining_before != report["receipt"].remaining_before
            or report_boundary["qty"] != Decimal(receipt.remaining_before)
        ):
            raise _OriginUnproven
        await _confirm_origin_context(
            session, organization_id, _operation_snapshot(operation), trail=trail,
        )
    return item


async def _validate_current_binding(stock, session, organization_id, source_warehouse_id, actor, seed):
    from modules.wms.physical_models import PhysicalOperation
    from modules.wms.physical_stock import responsible_recorded_receipt

    row = await stock._responsibility_row(
        session, organization_id, source_warehouse_id,
    )
    if row is None or row.active is not True:
        raise HTTPException(403, "Для склада-источника не назначен ответственный")
    operation = await session.scalar(select(PhysicalOperation).where(
        PhysicalOperation.organization_id == organization_id,
        PhysicalOperation.warehouse_id == source_warehouse_id,
        PhysicalOperation.operation_id == row.operation_id,
    ).execution_options(populate_existing=True))
    if operation is None or operation.kind != "warehouse_responsible_set":
        _conflict("Текущее назначение ответственного повреждено")
    assignment = responsible_recorded_receipt(operation)
    if (
        assignment.active is not True
        or assignment.revision != row.revision
        or assignment.user_id != row.user_id
        or assignment.warehouse_responsible_v1.assignee_subject != row.subject
        or row.user_id != seed[0]
        or row.subject != seed[1]
    ):
        raise HTTPException(403, "Текущий пользователь не назначен ответственным за склад-источник")
    await stock._fresh_capacity_authority(
        session, organization_id, actor, ("wms.operate", "wms.approve"),
    )
    return row, operation, assignment


async def _lock_native_warehouses(session, organization_id, warehouse_ids):
    from modules.wms.physical_stock import native_receiving_source_warehouse

    identifiers = sorted(set(warehouse_ids))
    if not identifiers or any(type(identifier) is not int or not 1 <= identifier <= 2147483647
                              for identifier in identifiers):
        _conflict("Складской контур межскладской недостачи повреждён")
    controls = {}
    for identifier in identifiers:
        controls[identifier] = await native_receiving_source_warehouse(
            session, organization_id, identifier, lock=True,
        )
    return controls


def _dispatch_parts(operation, organization_id, *, receipts=()):
    from modules.wms.item_identity import (
        _origin_qty,
        _origin_validate_operation,
        _OriginUnproven,
    )

    result = operation.result if isinstance(operation.result, dict) else {}
    if operation.kind != "transfer_dispatch" and result.get("kind") != "transfer_dispatch":
        return None
    _, request, validated_result = _origin_validate_operation(
        _operation_snapshot(operation), organization_id,
    )
    sent = validated_result.get("transfer")
    destination = request.get("destination_warehouse_id")
    if (
        operation.organization_id != organization_id
        or operation.kind != "transfer_dispatch"
        or request.get("kind") != "transfer_dispatch"
        or not isinstance(sent, dict)
        or type(destination) is not int
        or not 1 <= destination <= 2147483647
        or sent.get("dispatch_operation_id") != str(operation.operation_id)
        or sent.get("source_warehouse_id") != operation.warehouse_id
        or sent.get("destination_warehouse_id") != destination
    ):
        raise _OriginUnproven
    for receipt in receipts:
        if (
            operation.warehouse_id != receipt.source_warehouse_id
            or destination != receipt.destination_warehouse_id
            or sent.get("dispatch_operation_id") != str(receipt.dispatch_operation_id)
            or sent.get("source_warehouse_id") != receipt.source_warehouse_id
            or sent.get("destination_warehouse_id") != receipt.destination_warehouse_id
            or sent.get("source_unit_id") != str(receipt.source_unit_id)
            or sent.get("transit_unit_id") != str(receipt.transit_unit_id)
            or sent.get("container") != receipt.container
            or _origin_qty(sent.get("qty"))
            < _origin_qty(format(receipt.reported_qty, "f"))
        ):
            raise _OriginUnproven
    return {
        "operation": operation,
        "request": request,
        "result": validated_result,
        "sent": sent,
        "destination": destination,
    }


async def _blind_guard(session, warehouse_ids):
    from modules.wms.physical_stock import protect_blind_projection

    await protect_blind_projection(session, sorted(set(warehouse_ids)))


async def _discover_report(session, organization_id, report_operation_id):
    from modules.wms.item_identity import _OriginUnproven
    from modules.wms.physical_models import PhysicalOperation

    if not isinstance(report_operation_id, str):
        raise _OriginUnproven
    report = await session.scalar(select(PhysicalOperation).where(
        PhysicalOperation.organization_id == organization_id,
        PhysicalOperation.operation_id == report_operation_id,
    ).execution_options(populate_existing=True))
    if report is None or report.kind != REPORT_KIND:
        _conflict("Ожидающая квитанция расхождения не найдена")
    try:
        causal, request, result = origin_validate_loss(
            _operation_snapshot(report), organization_id,
        )
    except _OriginUnproven as exc:
        raise HTTPException(409, "Квитанция расхождения повреждена") from exc
    receipt = recorded_receipt(_operation_snapshot(report))
    return {
        "operation": report,
        "causal": causal,
        "request": request,
        "result": result,
        "receipt": receipt,
    }


def _make_causal(command_json, frontier):
    return {
        "schema_version": 3,
        "canonical_request": command_json,
        "births": [],
        "count_basis": [],
        "prior_frontiers": frontier,
    }


def _validate_receipt(receipt_values):
    try:
        typed = _RECEIPT_ADAPTER.validate_python(receipt_values)
        dumped = typed.model_dump(mode="json")
    except (ValidationError, TypeError, ValueError) as exc:
        raise HTTPException(409, "Сформированная квитанция недостачи не прошла проверку") from exc
    return typed, dumped


def _public_receipt(operation_snapshot):
    receipt = recorded_receipt(operation_snapshot)
    result = public_loss_result(operation_snapshot)
    command = receipt.transfer_discrepancy_v1.command
    result["evidence"] = command.evidence
    if receipt.kind == RETURN_KIND:
        result["reason"] = command.reason
    return result


async def _existing_operation(session, organization_id, operation_id):
    from modules.wms.physical_models import PhysicalOperation

    return await session.scalar(select(PhysicalOperation).where(
        PhysicalOperation.organization_id == organization_id,
        PhysicalOperation.operation_id == operation_id,
    ).execution_options(populate_existing=True))


async def _replay_loss(
    stock, session, organization_id, command, command_json, actor, seed, existing,
    *, authority_check=None,
):
    from modules.wms.item_identity import _OriginUnproven
    from modules.wms.physical_stock import protect_blind_receipt

    if existing.kind not in LOSS_KINDS:
        _conflict("Идентификатор операции уже использован")
    try:
        causal, request, result = origin_validate_loss(
            _operation_snapshot(existing), organization_id,
        )
    except _OriginUnproven as exc:
        raise HTTPException(409, "Сохранённая квитанция недостачи повреждена") from exc
    receipt = recorded_receipt(_operation_snapshot(existing))
    expected_hash = loss_request_hash(
        organization_id, command.warehouse_id, seed[0], seed[1], command_json,
    )
    if (
        existing.warehouse_id != command.warehouse_id
        or existing.kind != command.kind
        or existing.operation_id != str(command.operation_id)
        or existing.request_hash != expected_hash
        or receipt.operation_id != command.operation_id
        or receipt.kind != command.kind
    ):
        _conflict("Идентификатор операции уже использован с другими данными")

    warehouse_ids = (receipt.source_warehouse_id, receipt.destination_warehouse_id)
    await _lock_native_warehouses(session, organization_id, warehouse_ids)
    permissions = ("wms.operate",) if command.kind == REPORT_KIND else (
        "wms.operate", "wms.approve",
    )
    await _fresh_loss_authority(
        stock, session, organization_id, actor, permissions, authority_check,
    )
    await _blind_guard(session, warehouse_ids)
    current = await _existing_operation(session, organization_id, str(command.operation_id))
    if current is None or current.id != existing.id:
        _conflict("Сохранённая квитанция изменилась; повторите чтение")
    try:
        origin_validate_loss(_operation_snapshot(current), organization_id)
        current_receipt = recorded_receipt(
            _operation_snapshot(current),
        )
        if (
            current.warehouse_id != command.warehouse_id
            or current.kind != command.kind
            or current.request_hash != expected_hash
            or current_receipt.source_warehouse_id != receipt.source_warehouse_id
            or current_receipt.destination_warehouse_id != receipt.destination_warehouse_id
        ):
            _conflict("Сохранённая квитанция изменилась; повторите чтение")
        history = await _loss_history(
            session, organization_id, warehouse_ids=warehouse_ids,
        )
        await _validate_saved_record(
            session, organization_id, current, history,
        )
    except _OriginUnproven as exc:
        raise HTTPException(409, "Связанная история недостачи не подтверждена") from exc
    await protect_blind_receipt(session, current)
    await _fresh_loss_authority(
        stock, session, organization_id, actor, permissions, authority_check,
    )
    return _public_receipt(_operation_snapshot(current))


async def _fresh_loss_authority(
    stock, session, organization_id, actor, permissions, authority_check,
):
    if authority_check is None:
        await stock._fresh_capacity_authority(
            session, organization_id, actor, permissions,
        )
    else:
        await authority_check()


async def _new_report(
    stock, session, organization_id, command, command_json, actor, seed,
    *, authority_check=None,
):
    from modules.wms.item_identity import (
        _origin_frontier_tokens,
        _OriginUnproven,
        _prove_unit_boundary,
    )
    from modules.wms.physical_models import (
        CurrentStockStateError,
        PhysicalOperation,
        StockUnit,
    )
    from modules.wms.physical_stock import (
        _require_physical_causal_snapshot,
        decimal_text,
        physical_unit_frontier,
        policy,
        require_current_in_transit,
    )

    dispatch, sent = await stock._transfer_dispatch_receipt(
        session, organization_id, command,
    )
    if (
        sent["destination_warehouse_id"] != command.warehouse_id
        or sent["transit_unit_id"] != str(command.unit_id)
        or sent["dispatch_operation_id"] != str(command.dispatch_operation_id)
    ):
        _conflict("Отправка относится к другому складу или остатку")
    source_id = sent["source_warehouse_id"]
    destination_id = sent["destination_warehouse_id"]
    warehouse_ids = (source_id, destination_id)
    controls = await _lock_native_warehouses(session, organization_id, warehouse_ids)
    await _fresh_loss_authority(
        stock, session, organization_id, actor, ("wms.operate",), authority_check,
    )
    await _blind_guard(session, warehouse_ids)
    await _require_physical_causal_snapshot(session)

    operation_id = str(command.operation_id)
    if await _existing_operation(session, organization_id, operation_id) is not None:
        _conflict("Идентификатор операции уже использован")
    dispatch, sent = await stock._transfer_dispatch_receipt(
        session, organization_id, command,
    )
    if (
        dispatch.warehouse_id != source_id
        or sent["source_warehouse_id"] != source_id
        or sent["destination_warehouse_id"] != destination_id
        or destination_id not in controls
    ):
        _conflict("Складской контур отправки изменился")

    transit = await session.scalar(select(StockUnit).where(
        StockUnit.id == str(command.unit_id),
        StockUnit.warehouse_id == source_id,
    ).with_for_update().execution_options(populate_existing=True))
    if transit is None:
        _conflict("Остаток в пути не найден")
    try:
        require_current_in_transit(transit)
    except CurrentStockStateError as exc:
        raise HTTPException(409, "Остаток в пути не подтверждён") from exc
    if transit.dispatch_operation_id != dispatch.operation_id:
        _conflict("Остаток относится к другой отправке")
    await policy(session, source_id, transit.sku_code, command.qty)
    frontier, _ = await physical_unit_frontier(
        session, organization_id, transit.id,
    )
    try:
        proven = await _prove_unit_boundary(
            session, organization_id, transit.id, _origin_frontier_tokens(frontier),
            current_unit=transit, expected_state="in_transit", allow_zero=True,
        )
    except _OriginUnproven as exc:
        raise HTTPException(409, "Происхождение остатка в пути не подтверждено") from exc
    qty = Decimal(command.qty)
    if qty <= 0 or qty > proven["qty"]:
        _conflict("Заявленное расхождение превышает подтверждённый остаток в пути")
    if transit.assembly_output_id is not None and qty != Decimal(1):
        _conflict("Изделие сборки учитывается только целиком")

    history = await _loss_history(
        session, organization_id, warehouse_ids=warehouse_ids,
    )
    pending = [
        item for item in history["reports_by_transit"].get(str(transit.id), [])
        if not history["decisions_by_report"].get(str(item["receipt"].operation_id))
    ]
    if pending:
        _conflict("Сначала явно верните незавершённое расхождение")

    causal = _make_causal(command_json, {str(transit.id): frontier})
    report = {
        "protocol": "wms.transfer.discrepancy.v1",
        "kind": REPORT_KIND,
        "operation_id": operation_id,
        "organization_id": organization_id,
        "warehouse_id": destination_id,
        "report_operation_id": operation_id,
        "dispatch_operation_id": str(dispatch.operation_id),
        "source_warehouse_id": source_id,
        "destination_warehouse_id": destination_id,
        "source_unit_id": sent["source_unit_id"],
        "transit_unit_id": str(transit.id),
        "reported_qty": decimal_text(qty),
        "remaining_before": decimal_text(proven["qty"]),
        "remaining_after": decimal_text(proven["qty"]),
        "container": sent["container"],
        "physical_causal_v1": causal,
        "status": "pending",
        "transfer_discrepancy_v1": {
            "actor_user_id": seed[0],
            "actor_sub": seed[1],
            "command": command_json,
        },
    }
    typed, stored = _validate_receipt(report)
    request_hash = loss_request_hash(
        organization_id, destination_id, seed[0], seed[1], command_json,
    )
    operation = PhysicalOperation(
        organization_id=organization_id,
        warehouse_id=destination_id,
        operation_id=operation_id,
        kind=typed.kind,
        request_hash=request_hash,
        result=stored,
        actor=seed[1],
    )
    session.add(operation)
    await session.flush()
    history = await _loss_history(
        session, organization_id, warehouse_ids=warehouse_ids,
    )
    saved = history["by_id"].get(operation_id)
    if (
        saved is None
        or saved["operation"].id != operation.id
        or saved["operation"].request_hash != request_hash
    ):
        _origin_failure()
    await _validate_saved_record(session, organization_id, operation, history)
    await _fresh_loss_authority(
        stock, session, organization_id, actor, ("wms.operate",), authority_check,
    )
    return _public_receipt(_operation_snapshot(operation))


async def _new_decision(
    stock, session, organization_id, command, command_json, actor, seed, report,
):
    from modules.wms.item_identity import (
        _origin_frontier_tokens,
        _OriginUnproven,
        _prove_unit_boundary,
    )
    from modules.wms.physical_models import (
        CurrentStockStateError,
        PhysicalLedger,
        PhysicalOperation,
        StockUnit,
    )
    from modules.wms.physical_stock import (
        _require_physical_causal_snapshot,
        decimal_text,
        physical_unit_frontier,
        require_current_in_transit,
        transfer_dimensions,
    )

    report_receipt = report["receipt"]
    source_id = report_receipt.source_warehouse_id
    destination_id = report_receipt.destination_warehouse_id
    if command.warehouse_id != source_id:
        _conflict("Решение должно быть направлено на склад-источник")
    warehouse_ids = (source_id, destination_id)
    await _lock_native_warehouses(session, organization_id, warehouse_ids)
    await stock._fresh_capacity_authority(
        session, organization_id, actor, ("wms.operate", "wms.approve"),
    )
    await _blind_guard(session, warehouse_ids)
    await _require_physical_causal_snapshot(session)

    current_report = await _discover_report(
        session, organization_id, str(report_receipt.operation_id),
    )
    if (
        current_report["operation"].id != report["operation"].id
        or current_report["operation"].request_hash != report["operation"].request_hash
        or current_report["receipt"].model_dump(mode="json")
        != report_receipt.model_dump(mode="json")
    ):
        _conflict("Квитанция расхождения изменилась")
    history = await _loss_history(
        session, organization_id, warehouse_ids=warehouse_ids,
    )
    current_report = history["by_id"].get(str(report_receipt.operation_id))
    if current_report is None:
        _conflict("Квитанция расхождения не найдена")
    if (
        current_report["operation"].id != report["operation"].id
        or current_report["operation"].request_hash != report["operation"].request_hash
        or current_report["receipt"].model_dump(mode="json")
        != report_receipt.model_dump(mode="json")
    ):
        _conflict("Квитанция расхождения изменилась")
    if history["decisions_by_report"].get(str(report_receipt.operation_id)):
        _conflict("Решение по этому расхождению уже записано")

    _row, assignment_operation, assignment = await _validate_current_binding(
        stock, session, organization_id, source_id, actor, seed,
    )
    if command.kind == CONFIRM_KIND:
        reporter = report_receipt.transfer_discrepancy_v1
        if seed[0] == reporter.actor_user_id or seed[1] == reporter.actor_sub:
            raise HTTPException(403, "Подтверждать потерю должен другой ответственный")

    transit = await session.scalar(select(StockUnit).where(
        StockUnit.id == str(report_receipt.transit_unit_id),
        StockUnit.warehouse_id == source_id,
    ).with_for_update().execution_options(populate_existing=True))
    if transit is None:
        _conflict("Остаток в пути не найден")
    try:
        require_current_in_transit(transit)
    except CurrentStockStateError as exc:
        raise HTTPException(409, "Остаток в пути не подтверждён") from exc
    dispatch = history["dispatches"].get(str(report_receipt.dispatch_operation_id))
    if (
        dispatch is None
        or transit.dispatch_operation_id != dispatch["operation"].operation_id
    ):
        _conflict("Остаток относится к другой отправке")
    frontier, _ = await physical_unit_frontier(
        session, organization_id, transit.id,
    )
    try:
        proven = await _prove_unit_boundary(
            session, organization_id, transit.id, _origin_frontier_tokens(frontier),
            current_unit=transit, expected_state="in_transit", allow_zero=True,
        )
        report_tokens = _origin_frontier_tokens(
            report_receipt.physical_causal_v1.prior_frontiers[str(transit.id)],
        )
        current_tokens = _origin_frontier_tokens(frontier)
        report_boundary, _ = await _prove_report_record(
            session, organization_id, current_report, history,
        )
    except _OriginUnproven as exc:
        raise HTTPException(409, "История остатка или расхождения не подтверждена") from exc

    qty = Decimal(report_receipt.reported_qty)
    if command.kind == CONFIRM_KIND:
        if current_tokens != report_tokens or proven["qty"] != Decimal(report_receipt.remaining_before):
            _conflict("Остаток изменился после сообщения; верните его и создайте новое")
        if qty > proven["qty"]:
            _conflict("Заявленное количество превышает остаток в пути")
        if transit.assembly_output_id is not None and qty != Decimal(1):
            _conflict("Изделие сборки списывается только целиком")
    if report_boundary["qty"] != Decimal(report_receipt.remaining_before):
        _conflict("Сохранённая граница сообщения повреждена")

    operation_id = str(command.operation_id)
    if await _existing_operation(session, organization_id, operation_id) is not None:
        _conflict("Идентификатор операции уже использован")
    causal = _make_causal(command_json, {str(transit.id): frontier})
    current_qty = proven["qty"]
    remaining_after = current_qty - qty if command.kind == CONFIRM_KIND else current_qty
    proof = {
        "actor_user_id": seed[0],
        "actor_sub": seed[1],
        "assignment_operation_id": assignment_operation.operation_id,
        "assignment_revision": assignment.revision,
        "command": command_json,
    }
    receipt_values = {
        "protocol": "wms.transfer.discrepancy.v1",
        "kind": command.kind,
        "operation_id": operation_id,
        "organization_id": organization_id,
        "warehouse_id": source_id,
        "report_operation_id": str(report_receipt.operation_id),
        "dispatch_operation_id": str(report_receipt.dispatch_operation_id),
        "source_warehouse_id": source_id,
        "destination_warehouse_id": destination_id,
        "source_unit_id": str(report_receipt.source_unit_id),
        "transit_unit_id": str(transit.id),
        "reported_qty": decimal_text(qty),
        "remaining_before": decimal_text(current_qty),
        "remaining_after": decimal_text(remaining_after),
        "container": report_receipt.container,
        "physical_causal_v1": causal,
        "status": "confirmed" if command.kind == CONFIRM_KIND else "returned",
        "transfer_discrepancy_v1": proof,
    }
    typed, stored = _validate_receipt(receipt_values)
    request_hash = loss_request_hash(
        organization_id, source_id, seed[0], seed[1], command_json,
    )
    operation = PhysicalOperation(
        organization_id=organization_id,
        warehouse_id=source_id,
        operation_id=operation_id,
        kind=typed.kind,
        request_hash=request_hash,
        result=stored,
        actor=seed[1],
    )
    session.add(operation)

    if command.kind == CONFIRM_KIND:
        transit.qty -= qty
    await session.flush()
    if command.kind == CONFIRM_KIND:
        session.add(PhysicalLedger(
            operation_pk=operation.id,
            unit_id=transit.id,
            qty_delta=-qty,
            dimensions=transfer_dimensions(transit),
        ))
        await session.flush()
        if transit.assembly_output_id is not None:
            await stock._assembly_whole_identity(session, organization_id)
        if stock.event_bus:
            stock.event_bus.emit(
                session,
                "wms.physical.confirmed",
                {
                    **public_loss_result(_operation_snapshot(operation)),
                    "organization_id": organization_id,
                },
            )
            await session.flush()

    history = await _loss_history(
        session, organization_id, warehouse_ids=warehouse_ids,
    )
    saved = history["by_id"].get(operation_id)
    if (
        saved is None
        or saved["operation"].id != operation.id
        or saved["operation"].request_hash != request_hash
    ):
        _origin_failure()
    await _validate_saved_record(session, organization_id, operation, history)
    current_row, current_assignment_operation, current_assignment = (
        await _validate_current_binding(
            stock, session, organization_id, source_id, actor, seed,
        )
    )
    if (
        current_row.operation_id != assignment_operation.operation_id
        or current_assignment_operation.operation_id != assignment_operation.operation_id
        or current_assignment.revision != assignment.revision
    ):
        raise HTTPException(403, "Назначение ответственного изменилось")
    return _public_receipt(_operation_snapshot(operation))


async def execute_loss(stock, session, organization_id, data, actor):
    """Execute or replay one immutable native loss receipt in the caller TX."""
    from modules.wms.item_identity import _OriginUnproven
    from modules.wms.physical_stock import _require_physical_causal_snapshot

    command, command_json = _typed_command(data)
    operation_id = _ids(command)
    seed = _actor_seed(stock, actor)
    if type(organization_id) is not int or not 1 <= organization_id <= 2147483647:
        _conflict("Некорректная организация")
    if not session.in_transaction():
        _conflict("Команда требует транзакцию физической операции")

    try:
        await stock._fresh_capacity_authority(
            session, organization_id, actor, ("wms.operate",),
        )
        await _require_physical_causal_snapshot(session)
        existing = await _existing_operation(session, organization_id, operation_id)
        if existing is not None:
            return await _replay_loss(
                stock, session, organization_id, command, command_json,
                actor, seed, existing,
            )
        if command.kind == REPORT_KIND:
            return await _new_report(
                stock, session, organization_id, command, command_json,
                actor, seed,
            )
        report = await _discover_report(
            session, organization_id, str(command.report_operation_id),
        )
        return await _new_decision(
            stock, session, organization_id, command, command_json,
            actor, seed, report,
        )
    except _OriginUnproven as exc:
        raise HTTPException(409, "История межскладской недостачи не подтверждена") from exc


async def execute_mobile_report(stock, session, organization_id, data, actor):
    """Execute only the signed receiving-side report action; decisions stay native."""
    from modules.wms.item_identity import _OriginUnproven
    from modules.wms.physical_stock import _require_physical_causal_snapshot

    command, command_json = _typed_command(data)
    if command.kind != REPORT_KIND:
        raise HTTPException(403, "Мобильная команда недоступна")
    _ids(command)
    _, identity, _ = _mobile_report_context(
        stock, organization_id, command.warehouse_id,
    )
    if not session.in_transaction():
        _conflict("Команда требует транзакцию физической операции")
    seed = (identity.original_user_id, identity.original_sub)

    async def authority_check():
        await _fresh_mobile_report_authority(
            stock, session, organization_id, command.warehouse_id, actor,
        )

    try:
        await authority_check()
        await _require_physical_causal_snapshot(session)
        existing = await _existing_operation(
            session, organization_id, str(command.operation_id),
        )
        if existing is not None:
            return await _replay_loss(
                stock, session, organization_id, command, command_json,
                actor, seed, existing, authority_check=authority_check,
            )
        return await _new_report(
            stock, session, organization_id, command, command_json,
            actor, seed, authority_check=authority_check,
        )
    except _OriginUnproven as exc:
        raise HTTPException(409, "История межскладской недостачи не подтверждена") from exc


async def read_mobile_report(
    stock, session, organization_id, warehouse_id, operation_id, actor,
):
    """Read only the immutable report owned by this exact signed mobile identity."""
    from modules.wms.item_identity import _OriginUnproven
    from modules.wms.physical_stock import _require_physical_causal_snapshot

    if (
        type(organization_id) is not int or not 1 <= organization_id <= 2147483647
        or type(warehouse_id) is not int or not 1 <= warehouse_id <= 2147483647
    ):
        _conflict("Требуется точная организация и склад")
    if not session.in_transaction():
        _conflict("Чтение квитанции требует транзакцию")
    _, identity = await _fresh_mobile_report_authority(
        stock, session, organization_id, warehouse_id, actor, receipt=True,
    )
    try:
        await _require_physical_causal_snapshot(session)
        operation = await _existing_operation(session, organization_id, operation_id)
        if operation is None or operation.kind != REPORT_KIND:
            raise HTTPException(404, "Квитанция не найдена")
        initial = _operation_snapshot(operation)
        try:
            origin_validate_loss(initial, organization_id)
            receipt = recorded_receipt(initial)
        except _OriginUnproven as exc:
            raise HTTPException(404, "Квитанция не найдена") from exc
        proof = receipt.transfer_discrepancy_v1
        if (
            receipt.kind != REPORT_KIND
            or str(receipt.operation_id) != operation_id
            or receipt.warehouse_id != warehouse_id
            or receipt.organization_id != organization_id
            or proof.actor_user_id != identity.original_user_id
            or proof.actor_sub != identity.original_sub
        ):
            raise HTTPException(404, "Квитанция не найдена")
        warehouse_ids = (receipt.source_warehouse_id, receipt.destination_warehouse_id)
        await _lock_native_warehouses(session, organization_id, warehouse_ids)
        await _fresh_mobile_report_authority(
            stock, session, organization_id, warehouse_id, actor, receipt=True,
        )
        await _blind_guard(session, warehouse_ids)
        current = await _existing_operation(session, organization_id, operation_id)
        if (
            current is None or current.id != initial["id"]
            or _operation_snapshot(current) != initial
        ):
            _conflict("Квитанция изменилась во время чтения")
        history = await _loss_history(
            session, organization_id, warehouse_ids=warehouse_ids,
        )
        await _validate_saved_record(session, organization_id, current, history)
        result = _public_receipt(_operation_snapshot(current))
        await _fresh_mobile_report_authority(
            stock, session, organization_id, warehouse_id, actor, receipt=True,
        )
        return result
    except _OriginUnproven as exc:
        raise HTTPException(404, "Квитанция не найдена") from exc


async def read_loss(
    stock, session, organization_id, warehouse_id, operation_id, actor,
):
    """Read one immutable loss receipt in an explicit warehouse scope."""
    from modules.wms.item_identity import _origin_uuid_text, _OriginUnproven
    from modules.wms.physical_stock import _require_physical_causal_snapshot

    if (
        type(organization_id) is not int or not 1 <= organization_id <= 2147483647
        or type(warehouse_id) is not int or not 1 <= warehouse_id <= 2147483647
        or not _origin_uuid_text(operation_id)
    ):
        _conflict("Требуется точная организация, склад и идентификатор операции")
    if not session.in_transaction():
        _conflict("Чтение квитанции требует транзакцию")
    try:
        await stock._fresh_capacity_authority(
            session, organization_id, actor, ("wms.read",),
        )
        await _require_physical_causal_snapshot(session)
        operation = await _existing_operation(session, organization_id, operation_id)
        if operation is None:
            raise HTTPException(404, "Квитанция недостачи не найдена")
        operation_snapshot = _operation_snapshot(operation)
        if not loss_operation_indicator(operation.kind, operation.result):
            raise HTTPException(404, "Квитанция недостачи не найдена")
        try:
            origin_validate_loss(operation_snapshot, organization_id)
        except _OriginUnproven as exc:
            raise HTTPException(409, "Квитанция недостачи повреждена") from exc
        receipt = recorded_receipt(operation_snapshot)
        warehouse_ids = (receipt.source_warehouse_id, receipt.destination_warehouse_id)
        if warehouse_id not in warehouse_ids:
            raise HTTPException(404, "Квитанция недостачи не найдена")
        await _lock_native_warehouses(session, organization_id, warehouse_ids)
        await stock._fresh_capacity_authority(
            session, organization_id, actor, ("wms.read",),
        )
        await _blind_guard(session, warehouse_ids)
        current = await _existing_operation(session, organization_id, operation_id)
        if (
            current is None
            or current.id != operation_snapshot["id"]
            or _operation_snapshot(current) != operation_snapshot
        ):
            _conflict("Квитанция изменилась во время чтения")
        history = await _loss_history(
            session, organization_id, warehouse_ids=warehouse_ids,
        )
        await _validate_saved_record(session, organization_id, current, history)
        result = _public_receipt(_operation_snapshot(current))
        await stock._fresh_capacity_authority(
            session, organization_id, actor, ("wms.read",),
        )
        return result
    except _OriginUnproven as exc:
        raise HTTPException(409, "Связанная история недостачи не подтверждена") from exc


async def list_loss(stock, session, organization_id, warehouse_id, actor):
    """Read complete status for relevant reports after one sorted warehouse lock pass."""
    from modules.wms.item_identity import (
        _origin_frontier_tokens,
        _OriginUnproven,
        _prove_unit_boundary,
    )
    from modules.wms.physical_models import StockUnit
    from modules.wms.physical_stock import (
        _require_physical_causal_snapshot,
        physical_unit_frontier,
    )

    if (
        type(organization_id) is not int or not 1 <= organization_id <= 2147483647
        or type(warehouse_id) is not int or not 1 <= warehouse_id <= 2147483647
    ):
        _conflict("Требуется точная организация и склад")
    if not session.in_transaction():
        _conflict("Чтение статуса требует транзакцию")
    try:
        await stock._fresh_capacity_authority(
            session, organization_id, actor, ("wms.read",),
        )
        await _require_physical_causal_snapshot(session)
        initial_rows = await _warehouse_operations(
            session, organization_id, {warehouse_id},
        )
        before = await _loss_history(
            session, organization_id, warehouse_ids={warehouse_id},
            target_warehouse_id=warehouse_id, operations=initial_rows,
        )
        warehouse_union = {warehouse_id}
        peer_evidence = (
            before["scoped_candidates"]
            + list(before["referenced_reports"].values())
        )
        for item in peer_evidence:
            receipt = item["receipt"]
            pair = {receipt.source_warehouse_id, receipt.destination_warehouse_id}
            if warehouse_id in pair:
                warehouse_union.update(pair)
        for operation in initial_rows:
            dispatch = before["dispatches"].get(str(operation.operation_id))
            if dispatch is None:
                dispatch = _dispatch_parts(operation, organization_id)
            if dispatch is not None:
                warehouse_union.add(dispatch["destination"])
        await _lock_native_warehouses(session, organization_id, warehouse_union)
        await stock._fresh_capacity_authority(
            session, organization_id, actor, ("wms.read",),
        )
        await _blind_guard(session, warehouse_union)

        after = await _loss_history(
            session, organization_id, warehouse_ids=warehouse_union,
            target_warehouse_id=warehouse_id,
        )
        after_reports = [
            item for item in after["candidates"]
            if item["receipt"].kind == REPORT_KIND
            and warehouse_id in {
                item["receipt"].source_warehouse_id,
                item["receipt"].destination_warehouse_id,
            }
        ]
        reloaded_union = {warehouse_id}
        peer_evidence = (
            after["scoped_candidates"]
            + list(after["referenced_reports"].values())
        )
        for item in peer_evidence:
            receipt = item["receipt"]
            pair = {receipt.source_warehouse_id, receipt.destination_warehouse_id}
            if warehouse_id in pair:
                reloaded_union.update(pair)
        for operation in after["warehouse_rows"]:
            if operation.warehouse_id == warehouse_id:
                dispatch = after["dispatches"].get(str(operation.operation_id))
                if dispatch is None:
                    dispatch = _dispatch_parts(operation, organization_id)
                if dispatch is not None:
                    reloaded_union.add(dispatch["destination"])
        if not reloaded_union.issubset(warehouse_union):
            _conflict("Состав складов изменился во время чтения статуса")

        for item in after_reports:
            await _validate_saved_record(
                session, organization_id, item["operation"], after,
            )
            for decision_item in after["decisions_by_report"].get(
                str(item["receipt"].operation_id), [],
            ):
                await _validate_saved_record(
                    session, organization_id, decision_item["operation"], after,
                )

        proven_by_unit = {}
        for item in after_reports:
            report = item["receipt"]
            transit_id = str(report.transit_unit_id)
            if transit_id in proven_by_unit:
                continue
            transit = await session.scalar(select(StockUnit).where(
                StockUnit.id == transit_id,
                StockUnit.warehouse_id == report.source_warehouse_id,
            ).execution_options(populate_existing=True))
            if transit is None:
                raise _OriginUnproven
            frontier, _ = await physical_unit_frontier(
                session, organization_id, transit_id,
            )
            proof = await _prove_unit_boundary(
                session, organization_id, transit_id,
                _origin_frontier_tokens(frontier),
                current_unit=transit, expected_state="in_transit", allow_zero=True,
            )
            proven_by_unit[transit_id] = (frontier, proof)

        items = []
        for item in after_reports:
            report = item["receipt"]
            transit_id = str(report.transit_unit_id)
            frontier, proof = proven_by_unit[transit_id]
            report_frontier = _origin_frontier_tokens(
                report.physical_causal_v1.prior_frontiers[transit_id],
            )
            current_frontier = _origin_frontier_tokens(frontier)
            decision_items = after["decisions_by_report"].get(
                str(report.operation_id), [],
            )
            decision_item = decision_items[0] if decision_items else None
            if decision_item is not None:
                state = (
                    "confirmed"
                    if decision_item["receipt"].kind == CONFIRM_KIND
                    else "returned"
                )
                decision = _public_receipt(
                    _operation_snapshot(decision_item["operation"]),
                )
            else:
                state = (
                    "pending"
                    if report_frontier == current_frontier
                    and proof["qty"] == Decimal(report.remaining_before)
                    else "stale"
                )
                decision = None
            items.append({
                "report": _public_receipt(
                    _operation_snapshot(item["operation"]),
                ),
                "state": state,
                "current_remaining": format(proof["qty"], ".6f"),
                "decision": decision,
            })
        await stock._fresh_capacity_authority(
            session, organization_id, actor, ("wms.read",),
        )
        return {
            "protocol": "wms.transfer.discrepancy.status.v1",
            "organization_id": organization_id,
            "warehouse_id": warehouse_id,
            "items": items,
        }
    except _OriginUnproven as exc:
        raise HTTPException(409, "Состояние межскладских недостач не подтверждено") from exc


def origin_validate_loss(operation_snapshot, organization_id):
    """Pure strict branch for the canonical origin dispatcher."""
    from modules.wms.item_identity import (
        _origin_uuid_text,
        _OriginUnproven,
    )

    try:
        receipt = recorded_receipt(operation_snapshot)
        if (
            type(organization_id) is not int
            or receipt.organization_id != organization_id
            or not _origin_uuid_text(str(receipt.operation_id))
            or operation_snapshot.get("kind") != receipt.kind
            or operation_snapshot.get("warehouse_id") != receipt.warehouse_id
            or operation_snapshot.get("organization_id") != organization_id
        ):
            raise _OriginUnproven
        causal = receipt.physical_causal_v1.model_dump(mode="json")
        command = getattr(
            receipt,
            "transfer_discrepancy_v1",
        ).command.model_dump(mode="json")
        result = receipt.model_dump(
            mode="json",
            exclude={"physical_causal_v1"},
        )
        return causal, command, result
    except _OriginUnproven:
        raise
    except (
        LossReceiptError,
        ValidationError,
        TypeError,
        ValueError,
        KeyError,
        AttributeError,
        IndexError,
    ) as exc:
        raise _OriginUnproven from exc


async def _confirm_origin_context(
    session, organization_id, operation_snapshot, *, trail=frozenset(), historical=None,
):
    from modules.wms.item_identity import (
        _origin_frontier_tokens,
        _origin_transfer_dimensions,
        _OriginUnproven,
    )
    causal, request, result = origin_validate_loss(
        operation_snapshot, organization_id,
    )
    if operation_snapshot.get("kind") != CONFIRM_KIND:
        raise _OriginUnproven
    warehouse_ids = {result["source_warehouse_id"], result["destination_warehouse_id"]}
    operations = None
    actual_confirmation_pk = None
    validated_transfer_pair = None
    if historical is not None:
        from modules.wms.physical_models import PhysicalOperation
        from modules.wms.item_identity import _origin_operation_snapshot, _origin_transfer_pair

        historical.snapshot(operation_snapshot)
        actual_confirmation_pk = operation_snapshot.get("id")
        source_warehouse_id = result["source_warehouse_id"]
        destination_warehouse_id = result["destination_warehouse_id"]
        if (type(actual_confirmation_pk) is not int or actual_confirmation_pk <= 0
                or historical.warehouse_id != source_warehouse_id
                or source_warehouse_id == destination_warehouse_id):
            raise _OriginUnproven
        confirmation_rows = (await session.scalars(select(PhysicalOperation).where(
            PhysicalOperation.id == actual_confirmation_pk,
            PhysicalOperation.id < historical.before_pk,
            PhysicalOperation.organization_id == organization_id,
            PhysicalOperation.warehouse_id == source_warehouse_id,
            PhysicalOperation.operation_id == result["operation_id"],
            PhysicalOperation.kind == CONFIRM_KIND,
        ).execution_options(populate_existing=True))).all()
        if len(confirmation_rows) != 1:
            raise _OriginUnproven
        actual_confirmation = historical.operation(confirmation_rows[0])
        if _operation_snapshot(actual_confirmation) != operation_snapshot:
            raise _OriginUnproven

        dispatch_rows = (await session.scalars(select(PhysicalOperation).where(
            PhysicalOperation.organization_id == organization_id,
            PhysicalOperation.warehouse_id == source_warehouse_id,
            PhysicalOperation.operation_id == result["dispatch_operation_id"],
            PhysicalOperation.id < actual_confirmation_pk,
            PhysicalOperation.id < historical.before_pk,
        ).order_by(PhysicalOperation.id).execution_options(populate_existing=True))).all()
        if len(dispatch_rows) != 1:
            raise _OriginUnproven
        dispatch_row = dispatch_rows[0]
        dispatch_snapshot = _origin_operation_snapshot(dispatch_row)
        validated_transfer_pair = await _origin_transfer_pair(
            session, organization_id, dispatch_snapshot, historical=historical,
            unit_id=str(result["transit_unit_id"]),
        )
        if (dispatch_row.id >= actual_confirmation_pk
                or validated_transfer_pair["dispatch"]["id"] != dispatch_row.id
                or validated_transfer_pair["dispatch"]["operation_id"] != str(result["dispatch_operation_id"])
                or validated_transfer_pair["source_warehouse_id"] != source_warehouse_id
                or validated_transfer_pair["destination_warehouse_id"] != destination_warehouse_id
                or validated_transfer_pair["source_unit_id"] != str(result["source_unit_id"])
                or validated_transfer_pair["transit_unit_id"] != str(result["transit_unit_id"])
                or validated_transfer_pair["container"] != result["container"]):
            raise _OriginUnproven

        operations = (await session.scalars(select(PhysicalOperation).where(
            PhysicalOperation.organization_id == organization_id,
            PhysicalOperation.warehouse_id.in_(warehouse_ids),
            PhysicalOperation.id <= actual_confirmation_pk,
            PhysicalOperation.id < historical.before_pk,
        ).order_by(PhysicalOperation.id).execution_options(populate_existing=True))).all()
        for scoped in operations:
            if (scoped.organization_id != organization_id
                    or scoped.warehouse_id not in warehouse_ids
                    or scoped.id > actual_confirmation_pk
                    or scoped.id >= historical.before_pk):
                raise _OriginUnproven
    history = await _loss_history(
        session,
        organization_id,
        warehouse_ids=warehouse_ids,
        operations=operations,
        historical=historical,
        operation_pk_ceiling=actual_confirmation_pk,
        validated_transfer_pair=validated_transfer_pair,
    )
    operation = history["by_id"].get(str(result["operation_id"]))
    report = history["by_id"].get(str(result["report_operation_id"]))
    if (
        operation is None or report is None
        or operation["receipt"].kind != CONFIRM_KIND
        or report["receipt"].kind != REPORT_KIND
        or _operation_snapshot(operation["operation"]) != operation_snapshot
    ):
        raise _OriginUnproven
    report_boundary, dispatch = await _prove_report_record(
        session, organization_id, report, history, trail=trail, historical=historical,
    )
    try:
        confirm_tokens = _origin_frontier_tokens(
            causal["prior_frontiers"][str(result["transit_unit_id"])],
        )
        report_tokens = _origin_frontier_tokens(
            report["receipt"].physical_causal_v1.prior_frontiers[
                str(result["transit_unit_id"])
            ],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _OriginUnproven from exc
    if (
        confirm_tokens != report_tokens
        or Decimal(result["remaining_before"])
        != Decimal(report["receipt"].remaining_before)
        or report_boundary["qty"] != Decimal(result["remaining_before"])
    ):
        raise _OriginUnproven

    rows = history["ledgers_by_operation"].get(operation["operation"].id, [])
    if len(rows) != 1:
        raise _OriginUnproven
    row = rows[0]
    dimensions = _origin_transfer_dimensions(row.dimensions)
    qty = Decimal(result["reported_qty"])
    if (
        row.unit_id != str(result["transit_unit_id"])
        or Decimal(row.qty_delta) != -qty
        or dimensions != _origin_transfer_dimensions(report_boundary["dimensions"])
        or dimensions.get("warehouse_id") != result["source_warehouse_id"]
        or dimensions.get("stock_state") != "in_transit"
        or dimensions.get("dispatch_operation_id") != str(result["dispatch_operation_id"])
        or Decimal(result["remaining_after"])
        != Decimal(result["remaining_before"]) - qty
    ):
        raise _OriginUnproven
    return {
        "operation": operation,
        "report": report,
        "dispatch": dispatch,
        "report_boundary": report_boundary,
        "ledger": row,
        "dimensions": dimensions,
        "causal": causal,
        "request": request,
        "result": result,
    }


async def origin_loss_transition(
    session, organization_id, unit_id, group, previous_qty, previous_dimensions,
    trail, *, is_anchor, causal, request, result, rows, historical=None,
):
    """Validate one non-anchor loss ledger transition from its saved report boundary."""
    from modules.wms.item_identity import (
        _origin_qty,
        _origin_transfer_dimensions,
        _OriginUnproven,
    )

    if not isinstance(group, dict):
        raise _OriginUnproven
    operation = group.get("operation")
    if (
        is_anchor
        or not isinstance(operation, dict)
        or not isinstance(causal, dict)
        or not isinstance(request, dict)
        or not isinstance(result, dict)
        or not isinstance(rows, list)
        or operation.get("kind") != CONFIRM_KIND
        or result.get("kind") != CONFIRM_KIND
    ):
        raise _OriginUnproven
    context = await _confirm_origin_context(
        session, organization_id, operation, trail=trail, historical=historical,
    )
    receipt = context["operation"]["receipt"]
    qty = _origin_qty(format(receipt.reported_qty, "f"))
    expected_dimensions = context["dimensions"]
    if (
        unit_id != str(receipt.transit_unit_id)
        or len(rows) != 1
        or rows[0].get("id") != context["ledger"].id
        or rows[0].get("operation_pk") != context["operation"]["operation"].id
        or rows[0].get("unit_id") != str(receipt.transit_unit_id)
        or _origin_qty(rows[0].get("qty_delta")) != -qty
        or _origin_transfer_dimensions(rows[0].get("dimensions")) != expected_dimensions
        or previous_qty != context["report_boundary"]["qty"]
        or previous_dimensions != context["report_boundary"]["dimensions"]
        or previous_qty is None
        or previous_dimensions is None
        or Decimal(result["remaining_after"]) != previous_qty - qty
        or causal != context["causal"]
        or request != context["request"]
    ):
        raise _OriginUnproven
    return {
        "qty": previous_qty - qty,
        "dimensions": expected_dimensions,
        "basis": context["report_boundary"]["basis"],
        "root_operation_id": context["report_boundary"]["root_operation_id"],
        "tail_operation_id": str(receipt.operation_id),
        **(
            {"qc_birth_operation_id": context["report_boundary"]["qc_birth_operation_id"]}
            if "qc_birth_operation_id" in context["report_boundary"] else {}
        ),
    }


async def loss_ledger_projection(
    session, organization_id, operation_snapshot, ledger_row,
):
    """Project the one validated negative transit delta for physical history."""
    from modules.wms.item_identity import (
        _origin_transfer_dimensions,
        _OriginUnproven,
    )

    context = await _confirm_origin_context(
        session, organization_id, operation_snapshot,
    )
    if (
        ledger_row.id != context["ledger"].id
        or ledger_row.operation_pk != context["ledger"].operation_pk
        or ledger_row.unit_id != context["ledger"].unit_id
        or ledger_row.qty_delta != context["ledger"].qty_delta
        or ledger_row.dimensions != context["ledger"].dimensions
    ):
        raise _OriginUnproven
    dimensions = _origin_transfer_dimensions(ledger_row.dimensions)
    if dimensions != context["dimensions"]:
        raise _OriginUnproven
    return dimensions
