"""Narrow, sanitized WMS mobile API over the existing physical ledger."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from core.domain.models import User
from core.runtime.deps import get_core
from core.services import auth as auth_module
from core.services.auth import (
    CurrentUser,
    get_current_user,
    has_permission,
    resolve_effective_oidc_user,
)
from modules.accounting.models import AccessGrant
from modules.wms.mobile_access import (
    PROOF_HEADER,
    MobileMetadataAction,
    OriginalMobileFulfillmentSourceAuthority,
    OriginalMobileMetadataAuthority,
    OriginalMobileReceivingSourceAuthority,
    OriginalMobileTransferAuthority,
    VerifiedProof,
    _decode_proof,
    _deny,
    _native_mobile_receiving_source_seed,
    _public_key,
    _request_digest,
    capture_native_mobile_fulfillment_source_authority,
    capture_native_mobile_receiving_source_authority,
    consume_proof,
    fresh_mobile_identity,
    jwk_thumbprint,
    lock_and_revalidate,
    require_action,
    revalidate_mobile_metadata_authority,
    revalidate_mobile_fulfillment_source_authority,
    revalidate_mobile_transfer_authority,
    revalidate_native_mobile_receiving_source_authority,
)
from modules.wms.mobile_models import MobileDevice, MobileGrant, MobileProofUse
from modules.wms.mobile_schemas import (
    AdminCommand,
    DeviceRevocation,
    EnrollmentRequest,
    GrantApproval,
    GrantRestriction,
    MobileContext,
    MobileCount,
    MobileIncomingTransferManifest,
    MobileReceipt,
    MobileReceivingContext,
    MobileScan,
    MobileState,
    MobileStatus,
    MobileTransferDiscrepancyReportReceipt,
    MobileTransferReceipt,
)
from modules.wms.physical_models import (
    CountLocationLock,
    PhysicalCount,
    PhysicalCountLine,
    PhysicalHold,
    PhysicalLocation,
    PhysicalOperation,
    PhysicalTask,
    PickedAllocation,
    StockUnit,
)
from modules.wms.physical_routes import capacity_transaction
from modules.wms.physical_schemas import (
    ItemIdentityEnsureEnvelope,
    ItemIdentityEnsureRequest,
    ItemLabelPrepareEnvelope,
    ItemLabelPrepareRequest,
    ItemLabelVerifyEnvelope,
    ItemLabelVerifyRequest,
    TransferCommand,
    WarehouseCommand,
)
from modules.wms.physical_stock import (
    PhysicalStock,
    _on_hand_units,
    control,
    decimal_text,
    eligible,
    native_receipt_v2_projection,
    native_pack_owner,
    native_pack_v1_projection,
    native_selector_i32,
    picked_output,
    protect_blind_projection,
    protect_blind_receipt,
    require_native_receipt_owner,
    unit_output,
)
from modules.wms.transfer_loss_protocol import REPORT_KIND, TransferDiscrepancyReport
from modules.wms.warehouse_catalog import transaction

router = APIRouter(prefix="/mobile", tags=["WMS mobile"])
MobilePostBody = WarehouseCommand | AdminCommand


def _transfer_receipt_view(result: dict) -> dict:
    """Versioned acknowledgement: transit is cargo, never an addressable unit."""
    from pydantic import ValidationError

    outcome = {"transfer": result.get("transfer")}
    if isinstance(result.get("unit"), dict):
        outcome["unit"] = {**_unit_view(result["unit"]), "packaging_id": result["unit"].get("packaging_id")}
    try:
        return MobileTransferReceipt.model_validate({
            "operation_id": result.get("operation_id"), "kind": result.get("kind"),
            "warehouse_id": result.get("warehouse_id"), "outcome": outcome,
        }).model_dump(mode="json")
    except ValidationError as exc:
        raise HTTPException(409, "Квитанция перемещения не подтверждена") from exc

_SAFE_FAILURE = "Операция не выполнена; проверьте доступ и данные"
_UNIT_KEYS = {
    "id", "warehouse_id", "location_id", "sku_code", "batch_id", "quality",
    "package_state", "container", "qty", "reserved", "free", "blocked_reason",
}


def _safe_error(exc: HTTPException) -> HTTPException:
    detail = (
        "Недостаточно прав" if exc.status_code == 403
        else "Запись не найдена" if exc.status_code == 404
        else _SAFE_FAILURE
    )
    return HTTPException(status_code=exc.status_code, detail=detail)


def _is_admin(core, user: CurrentUser, *, global_revoke: bool = False) -> None:
    if auth_module.get_settings().auth_mode != "oidc" or user.local_status != "active":
        _deny()
    if not (
        has_permission(core, user, "wms.configure")
        and has_permission(core, user, "wms.approve")
        and (not global_revoke or has_permission(core, user, "system.write"))
    ):
        _deny()


async def _fresh_admin(session, principal: CurrentUser) -> CurrentUser:
    if auth_module.get_settings().auth_mode != "oidc" or not principal.keycloak_user_id:
        _deny()
    try:
        fresh = await resolve_effective_oidc_user(replace(principal, local_status=None), session)
    except Exception as exc:
        raise HTTPException(status_code=403, detail="Текущий доступ недоступен") from exc
    if (
        fresh.keycloak_user_id != principal.keycloak_user_id
        or fresh.local_status != "active"
        or not fresh.roles
    ):
        _deny()
    return fresh


def _grant_view(grant: MobileGrant) -> dict:
    return {
        "grant_id": grant.id,
        "revision": grant.revision,
        "organization_id": grant.organization_id,
        "warehouse_id": grant.warehouse_id,
        "actions": sorted(grant.actions),
    }


def _emit_access_change(
    session,
    core,
    *,
    actor_sub: str,
    entity_type: str,
    entity_id: str,
    organization_id: int | None,
    warehouse_id: int | None,
    old_state: str | None,
    new_state: str | None,
    old_revision: int | None,
    new_revision: int | None,
    old_actions: list[str] | None = None,
    new_actions: list[str] | None = None,
) -> None:
    core.event_bus.emit(session, "wms.mobile.access.changed", {
        "actor_sub": actor_sub,
        "actor": actor_sub,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "entity_ref": f"wms:mobile:{entity_type}:{entity_id}",
        "organization_id": organization_id,
        "warehouse_id": warehouse_id,
        "old_state": old_state,
        "new_state": new_state,
        "old_actions": old_actions,
        "new_actions": new_actions,
        "old_revision": old_revision,
        "new_revision": new_revision,
    })


def _unit_view(source: dict) -> dict:
    return {key: value for key, value in source.items() if key in _UNIT_KEYS}


def _receipt_view(
    result: dict, *, native_v2: dict | None = None, native_pack_v1: dict | None = None,
) -> dict:
    """Project a physical receipt onto operational fields; never return raw result."""
    outcome: dict = {}
    native_binding = result.get("native_receipt_source_v1")
    native_receive = (
        result.get("kind") == "receive"
        and isinstance(native_binding, dict)
        and type(native_binding.get("protocol")) is int
        and native_binding.get("protocol") == 1
        and isinstance(native_binding.get("source_ref"), str)
        and bool(native_binding["source_ref"])
    )
    if isinstance(result.get("unit"), dict):
        projected_unit = _unit_view(result["unit"])
        if native_receive and "packaging_id" in result["unit"]:
            packaging_id = result["unit"]["packaging_id"]
            if packaging_id is None or (type(packaging_id) is int and packaging_id > 0):
                projected_unit["packaging_id"] = packaging_id
        outcome["unit"] = projected_unit
    if native_v2 is not None:
        outcome["unit"] = native_v2["unit"]
        outcome["native_receive_v2"] = native_v2
    elif native_receive:
        outcome["native_receive_v1"] = {
            "protocol": 1, "source_ref": native_binding["source_ref"],
        }
    for key in ("task_id", "count_id", "hold_id", "pick_id", "opening", "status", "quality", "qty"):
        if key in result and isinstance(result[key], (str, int, bool, type(None))):
            outcome[key] = result[key]
    # Existing Android clients require exactly unit and pick_id for a pick receipt.
    if result.get("kind") == "pick":
        outcome.pop("task_id", None)
    for key in ("holds", "tasks"):
        rows = result.get(key)
        if isinstance(rows, list):
            allowed = {"id", "unit_id", "qty", "picked", "reserved", "released", "kind", "status", "task_id"}
            outcome[key] = [
                {name: item[name] for name in allowed if name in item}
                for item in rows if isinstance(item, dict)
            ]
    if result.get("kind") == "pack_v2":
        outcome.update({
            key: result[key]
            for key in ("container", "packed", "protocol")
            if key in result and isinstance(result[key], (str, bool, int))
        })
        if native_pack_v1 is not None:
            outcome["shipping_packages_v1"] = native_pack_v1
    return {
        "operation_id": result["operation_id"],
        "kind": result["kind"],
        "warehouse_id": result["warehouse_id"],
        "status": "recorded",
        "outcome": outcome,
    }


async def _phase_proof(
    request: Request,
    core,
    user: CurrentUser,
    *,
    allowed_states: set[str] | None = None,
    require_grant: bool = False,
    metadata_action: MobileMetadataAction | None = None,
    receiving_source_authority: OriginalMobileReceivingSourceAuthority | None = None,
    receiving_source_action: Literal["transfer_discrepancy_report"] | None = None,
    receiving_source_receipt: bool = False,
    fulfillment_source_authority: OriginalMobileFulfillmentSourceAuthority | None = None,
) -> VerifiedProof:
    try:
        return await consume_proof(
            request, core, user, allowed_states=allowed_states,
            require_grant=require_grant, metadata_action=metadata_action,
            receiving_source_authority=receiving_source_authority,
            receiving_source_action=receiving_source_action,
            receiving_source_receipt=receiving_source_receipt,
            fulfillment_source_authority=fulfillment_source_authority,
        )
    except HTTPException as exc:
        if exc.status_code in {403, 409, 503}:
            raise
        raise _safe_error(exc) from exc


def _metadata_authority(
    proof: VerifiedProof, organization_id: int, warehouse_id: int,
    action: MobileMetadataAction,
) -> OriginalMobileMetadataAuthority:
    if proof.original_user_id is None or proof.original_sub is None:
        _deny()
    return OriginalMobileMetadataAuthority(
        original_user_id=proof.original_user_id,
        original_sub=proof.original_sub,
        verified_proof=proof,
        organization_id=organization_id,
        warehouse_id=warehouse_id,
        action=action,
    )


@router.post("/enroll", response_model=MobileStatus, status_code=201)
async def enroll(
    request: Request,
    data: EnrollmentRequest,
    session=Depends(transaction, scope="function"),
    core=Depends(get_core),
    user: CurrentUser = Depends(get_current_user),
):
    del session
    if auth_module.get_settings().auth_mode != "oidc":
        _deny()
    proof_token = request.headers.get(PROOF_HEADER)
    if not proof_token:
        _deny()
    body = await request.body()
    factory = core.services.db.session_factory
    if factory is None:
        _deny(503)
    jwk = data.public_jwk.model_dump()
    device_id = str(data.device_id)
    try:
        async with factory() as proof_session:
            fresh = await fresh_mobile_identity(proof_session, user)
            claims, _ = _decode_proof(
                proof_token, _public_key(jwk), request, body, fresh
            )
            if (
                claims["device_id"] != device_id
                or claims["device_revision"] != 0
                or claims.get("grant_id") is not None
            ):
                _deny()
            if await proof_session.get(MobileDevice, device_id) is not None:
                _deny(409)
            await core.services.accounting.source_member(proof_session, data.organization_id, fresh)
            warehouse = await control(
                proof_session, data.warehouse_id, data.organization_id, lock=False
            )
            from modules.wms.models import WarehouseDirectory

            directory = await proof_session.get(WarehouseDirectory, data.warehouse_id)
            if directory is None or not directory.is_active or warehouse.organization_id != data.organization_id:
                _deny(404)
            thumbprint = jwk_thumbprint(jwk)
            device = MobileDevice(
                id=device_id,
                owner_sub=fresh.keycloak_user_id,
                public_jwk=jwk,
                jwk_thumbprint=thumbprint,
                enrollment_jti=claims["jti"],
                enrollment_digest=_request_digest(request, body, claims),
                requested_organization_id=data.organization_id,
                requested_warehouse_id=data.warehouse_id,
                requested_actions=list(data.requested_actions),
                state="pending",
                revision=1,
            )
            proof = MobileProofUse(
                device_id=device_id,
                jti=claims["jti"],
                request_digest=device.enrollment_digest,
                expires_at=datetime.fromtimestamp(claims["iat"] + 90, UTC),
                operation_id=None,
                grant_id=None,
                grant_revision=None,
            )
            proof_session.add(device)
            await proof_session.flush()
            proof_session.add(proof)
            await proof_session.commit()
            return {"device_id": device_id, "state": "pending", "revision": 1}
    except HTTPException:
        raise
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Устройство или ключ уже зарегистрированы") from exc
    except Exception as exc:
        raise HTTPException(status_code=403, detail="Мобильное подключение не подтверждено") from exc


@router.get("/status", response_model=MobileStatus)
async def status(
    request: Request,
    session=Depends(transaction, scope="function"),
    core=Depends(get_core),
    user: CurrentUser = Depends(get_current_user),
):
    proof = await _phase_proof(request, core, user, allowed_states={"pending", "active", "revoked"})
    if proof.grant_id is not None:
        _deny()
    device = await session.scalar(select(MobileDevice).where(
        MobileDevice.id == proof.device_id
    ).with_for_update().execution_options(populate_existing=True))
    fresh = await fresh_mobile_identity(session, user)
    if device is None or device.owner_sub != fresh.keycloak_user_id:
        _deny()
    return {"device_id": device.id, "state": device.state, "revision": device.revision}


@router.get("/context", response_model=MobileContext)
async def context(
    request: Request,
    session=Depends(transaction, scope="function"),
    core=Depends(get_core),
    user: CurrentUser = Depends(get_current_user),
):
    proof = await _phase_proof(request, core, user)
    if proof.grant_id is not None:
        _deny()
    device = await session.scalar(select(MobileDevice).where(
        MobileDevice.id == proof.device_id
    ).with_for_update().execution_options(populate_existing=True))
    current_user = await fresh_mobile_identity(session, user)
    if device is None or device.state != "active" or device.owner_sub != current_user.keycloak_user_id:
        _deny()
    grants = (await session.scalars(select(MobileGrant).where(
            MobileGrant.device_id == device.id,
            MobileGrant.state == "active",
        ).order_by(MobileGrant.organization_id, MobileGrant.warehouse_id))).all()
    visible = []
    for grant in grants:
        try:
            await core.services.accounting.source_member(session, grant.organization_id, current_user)
            await control(session, grant.warehouse_id, grant.organization_id, lock=False)
        except HTTPException:
            continue
        visible.append(_grant_view(grant))
    return {"device_id": device.id, "device_revision": device.revision, "grants": visible}


@router.post(
    "/item-identities/ensure",
    status_code=201,
    response_model=ItemIdentityEnsureEnvelope,
)
async def mobile_ensure_item_identity(
    request: Request,
    org_id: Annotated[int, Query(gt=0, le=2_147_483_647)],
    data: ItemIdentityEnsureRequest,
    session=Depends(capacity_transaction, scope="function"),
    core=Depends(get_core),
    user: CurrentUser = Depends(get_current_user),
):
    proof = await _phase_proof(
        request, core, user, require_grant=True,
        metadata_action="item_identity_ensure",
    )
    authority = _metadata_authority(
        proof, org_id, data.warehouse_id, "item_identity_ensure",
    )
    try:
        from modules.wms.item_identity import ensure_item_identity

        return await ensure_item_identity(
            session, core, org_id, data, user, mobile_authority=authority,
        )
    except HTTPException as exc:
        raise _safe_error(exc) from exc


@router.post(
    "/item-labels/prepare",
    status_code=201,
    response_model=ItemLabelPrepareEnvelope,
)
async def mobile_prepare_item_label(
    request: Request,
    org_id: Annotated[int, Query(gt=0, le=2_147_483_647)],
    data: ItemLabelPrepareRequest,
    session=Depends(capacity_transaction, scope="function"),
    core=Depends(get_core),
    user: CurrentUser = Depends(get_current_user),
):
    proof = await _phase_proof(
        request, core, user, require_grant=True,
        metadata_action="item_label_prepare",
    )
    authority = _metadata_authority(
        proof, org_id, data.warehouse_id, "item_label_prepare",
    )
    try:
        from modules.wms.item_identity import prepare_item_label

        return await prepare_item_label(
            session, core, org_id, data, user, mobile_authority=authority,
        )
    except HTTPException as exc:
        raise _safe_error(exc) from exc


@router.post(
    "/item-labels/verify",
    status_code=201,
    response_model=ItemLabelVerifyEnvelope,
)
async def mobile_verify_item_label(
    request: Request,
    org_id: Annotated[int, Query(gt=0, le=2_147_483_647)],
    data: ItemLabelVerifyRequest,
    session=Depends(capacity_transaction, scope="function"),
    core=Depends(get_core),
    user: CurrentUser = Depends(get_current_user),
):
    proof = await _phase_proof(
        request, core, user, require_grant=True,
        metadata_action="item_label_verify",
    )
    authority = _metadata_authority(
        proof, org_id, data.warehouse_id, "item_label_verify",
    )
    try:
        from modules.wms.item_identity import verify_item_label

        return await verify_item_label(
            session, core, org_id, data, user, mobile_authority=authority,
        )
    except HTTPException as exc:
        raise _safe_error(exc) from exc


@router.get("/receiving-context", response_model=MobileReceivingContext)
async def receiving_context(
    request: Request,
    org_id: Annotated[int, Query(gt=0, le=2_147_483_647)],
    warehouse_id: Annotated[int, Query(gt=0, le=2_147_483_647)],
    session=Depends(transaction, scope="function"),
    core=Depends(get_core),
    user: CurrentUser = Depends(get_current_user),
):
    proof = await _phase_proof(
        request, core, user, require_grant=True,
        metadata_action="receiving_context",
    )
    authority = _metadata_authority(
        proof, org_id, warehouse_id, "receiving_context",
    )
    try:
        if authority.warehouse_id != warehouse_id:
            raise HTTPException(403, "Мобильный полномочный склад не совпадает с запросом")
        from core.domain.models import Sku
        from modules.wms.physical_models import Packaging, StoragePolicy

        _, _, _, _, cfg = await revalidate_mobile_metadata_authority(
            session, core, user, authority,
        )
        policy_rows = (await session.execute(
            select(StoragePolicy, Sku)
            .outerjoin(Sku, Sku.code == StoragePolicy.sku_code)
            .where(StoragePolicy.warehouse_id == warehouse_id)
            .order_by(StoragePolicy.sku_code)
            .execution_options(populate_existing=True)
        )).all()
        await revalidate_mobile_metadata_authority(session, core, user, authority)
        policy_sku_codes = {rule.sku_code for rule, _ in policy_rows}
        policies = []
        for rule, sku in policy_rows:
            active_sku = sku if sku is not None and sku.is_active else None
            policies.append({
                "sku_code": rule.sku_code,
                "sku_id": active_sku.id if active_sku is not None else None,
                "sku_title": active_sku.title if active_sku is not None else None,
                "sku_unit": active_sku.unit if active_sku is not None else None,
                "base_unit": rule.base_unit,
                "precision": rule.precision,
                "expiry_required": rule.expiry_required,
                "manufacture_required": rule.manufacture_required,
                "min_shelf_days": rule.min_shelf_days,
                "max_age_days": rule.max_age_days,
                "maintenance_days": rule.maintenance_days,
                "manufacturer_source": rule.manufacturer_source,
                "storage_rule": rule.storage_rule,
                "warranty_rule": rule.warranty_rule,
                "count_class": rule.count_class,
            })

        packages = (await session.scalars(
            select(Packaging)
            .where(
                Packaging.organization_id == org_id,
                Packaging.sku_code.in_(policy_sku_codes),
            )
            .order_by(Packaging.id)
            .execution_options(populate_existing=True)
        )).all()
        await revalidate_mobile_metadata_authority(session, core, user, authority)
        packaging = [{
            "id": row.id,
            "sku_code": row.sku_code,
            "factor": decimal_text(row.factor),
            "sealed_allowed": bool(row.sealed_allowed),
        } for row in packages]

        batch_result = await core.services.stock.list_batches(session, org_id)
        await revalidate_mobile_metadata_authority(session, core, user, authority)
        if (
            not isinstance(batch_result, dict)
            or batch_result.get("truncated") is not False
            or not isinstance(batch_result.get("items"), list)
        ):
            raise HTTPException(503, "Фасад партий вернул неполный контекст")
        batches = []
        for row in batch_result["items"]:
            if not isinstance(row, dict) or not isinstance(row.get("sku_code"), str):
                raise HTTPException(503, "Фасад партий вернул неверные метаданные")
            if row["sku_code"] not in policy_sku_codes:
                continue
            required = {
                "id", "sku_code", "lot_no", "mfg_date", "expiry_date",
                "date_source", "manufacture_precision",
            }
            if (
                not required.issubset(row)
                or type(row["id"]) is not int
                or row["id"] < 1
                or not isinstance(row["lot_no"], str)
                or not isinstance(row["mfg_date"], (str, type(None)))
                or not isinstance(row["expiry_date"], (str, type(None)))
                or not isinstance(row["date_source"], (str, type(None)))
                or not isinstance(row["manufacture_precision"], (str, type(None)))
                or row["manufacture_precision"] not in ("day", "month", "year", None)
            ):
                raise HTTPException(503, "Фасад партий вернул неверные метаданные")
            batches.append({
                "id": row["id"],
                "sku_code": row["sku_code"],
                "lot_no": row["lot_no"],
                "manufacture_date": row["mfg_date"],
                "expiry_date": row["expiry_date"],
                "date_source": row["date_source"],
                "manufacture_precision": row["manufacture_precision"],
            })
        return {
            "organization_id": org_id,
            "warehouse_id": warehouse_id,
            "mode": cfg.mode,
            "owner": {
                "user_id": authority.original_user_id,
                "sub": authority.original_sub,
            },
            "policies": policies,
            "packaging": packaging,
            "batches": batches,
        }
    except HTTPException as exc:
        raise _safe_error(exc) from exc


@router.get("/receiving-sources")
async def native_receiving_sources(
    request: Request,
    org_id: str = Query(...),
    warehouse_id: str = Query(...),
    before_receipt_id: str | None = Query(None),
    session=Depends(transaction, scope="function"),
    core=Depends(get_core),
    user: CurrentUser = Depends(get_current_user),
):
    organization_id = native_selector_i32(org_id, "organization_id")
    selected_warehouse_id = native_selector_i32(warehouse_id, "warehouse_id")
    before_id = native_selector_i32(before_receipt_id, "before_receipt_id") if before_receipt_id is not None else None
    authority = capture_native_mobile_receiving_source_authority(
        user, organization_id, selected_warehouse_id,
    )
    proof = await _phase_proof(
        request, core, user, require_grant=True,
        receiving_source_authority=authority,
    )
    try:
        await revalidate_native_mobile_receiving_source_authority(
            session, core, user, authority, proof, lock_warehouse=True,
        )
        page = await core.services.procurement_source.native_receipt_selector_page(
            session, organization_id, selected_warehouse_id, before_id,
        )
        await revalidate_native_mobile_receiving_source_authority(
            session, core, user, authority, proof, lock_warehouse=False,
        )
        await protect_blind_projection(session, [selected_warehouse_id])
        await revalidate_native_mobile_receiving_source_authority(
            session, core, user, authority, proof, lock_warehouse=False,
        )
        return {
            "protocol": 1, "organization_id": organization_id,
            "warehouse_id": selected_warehouse_id,
            "owner": {"user_id": authority.original_user_id, "sub": authority.original_sub},
            "items": page["items"],
            "next_before_receipt_id": page["next_before_receipt_id"],
        }
    except HTTPException as exc:
        raise _safe_error(exc) from exc
    except ValueError as exc:
        raise _safe_error(HTTPException(409, _SAFE_FAILURE)) from exc


@router.get("/receiving-sources/{receipt_id}")
async def native_receiving_source_detail(
    request: Request,
    receipt_id: str,
    org_id: str = Query(...),
    warehouse_id: str = Query(...),
    expected_version: str = Query(...),
    session=Depends(transaction, scope="function"),
    core=Depends(get_core),
    user: CurrentUser = Depends(get_current_user),
):
    organization_id = native_selector_i32(org_id, "organization_id")
    selected_warehouse_id = native_selector_i32(warehouse_id, "warehouse_id")
    selected_receipt_id = native_selector_i32(receipt_id, "receipt_id")
    version = native_selector_i32(expected_version, "expected_version")
    authority = capture_native_mobile_receiving_source_authority(
        user, organization_id, selected_warehouse_id,
    )
    proof = await _phase_proof(
        request, core, user, require_grant=True,
        receiving_source_authority=authority,
    )
    try:
        _, _, fresh, _, _ = await revalidate_native_mobile_receiving_source_authority(
            session, core, user, authority, proof, lock_warehouse=False,
        )
        source = await core.services.procurement_source.native_receipt_selector_detail(
            session, organization_id, selected_warehouse_id, selected_receipt_id, version,
        )
        await revalidate_native_mobile_receiving_source_authority(
            session, core, user, authority, proof, lock_warehouse=False,
        )
        if source is None:
            raise HTTPException(404, "Запись не найдена")
        stock = PhysicalStock(
            core.services.stock, core.event_bus, services=core.services, user=fresh, core=core,
        )
        history = await stock._native_receipt_history(session, organization_id, source["snapshot"])
        await revalidate_native_mobile_receiving_source_authority(
            session, core, user, authority, proof, lock_warehouse=True,
        )
        await protect_blind_projection(session, [selected_warehouse_id])
        await revalidate_native_mobile_receiving_source_authority(
            session, core, user, authority, proof, lock_warehouse=False,
        )
        await stock._native_receipt_history(
            session, organization_id, source["snapshot"], owners=history["owners"],
        )
        await revalidate_native_mobile_receiving_source_authority(
            session, core, user, authority, proof, lock_warehouse=False,
        )
        return {
            "protocol": 1, "organization_id": organization_id,
            "warehouse_id": selected_warehouse_id,
            "owner": {"user_id": authority.original_user_id, "sub": authority.original_sub},
            "receipt_id": selected_receipt_id, "version": version,
            "lines": source["lines"],
        }
    except HTTPException as exc:
        raise _safe_error(exc) from exc
    except ValueError as exc:
        raise _safe_error(HTTPException(409, _SAFE_FAILURE)) from exc


@router.post("/commands")
async def commands(
    request: Request,
    data: MobilePostBody,
    session=Depends(transaction, scope="function"),
    core=Depends(get_core),
    user: CurrentUser = Depends(get_current_user),
):
    if isinstance(data, (GrantApproval, GrantRestriction, DeviceRevocation)):
        try:
            return await _admin_command(session, core, user, data)
        except HTTPException as exc:
            raise _safe_error(exc) from exc

    receiving_authority = None
    fulfillment_authority = None
    if data.kind == "receive":
        try:
            receiving_org_id = int(request.query_params.get("org_id", "0"))
        except ValueError:
            _deny()
        receiving_authority = capture_native_mobile_receiving_source_authority(
            user, receiving_org_id, data.warehouse_id,
        )
    elif (data.kind == "pack_v2" and data.binding_revision_id is not None
            and data.shipping_packages is not None):
        try:
            pack_org_id = int(request.query_params.get("org_id", "0"))
        except ValueError:
            _deny()
        fulfillment_authority = capture_native_mobile_fulfillment_source_authority(
            user, pack_org_id, data.warehouse_id, purpose="pack",
        )
    principal = (_native_mobile_receiving_source_seed(receiving_authority)
                 if receiving_authority is not None else user)
    proof = await _phase_proof(request, core, principal, require_grant=True,
        receiving_source_authority=receiving_authority,
        fulfillment_source_authority=fulfillment_authority)
    try:
        raw_org_id = request.query_params.get("org_id")
        try:
            organization_id = int(raw_org_id) if raw_org_id is not None else 0
        except ValueError:
            _deny()
        if organization_id < 1:
            _deny()
        device, grant, fresh, actor = await lock_and_revalidate(
            session, core, principal, proof,
            organization_id=organization_id,
            warehouse_id=data.warehouse_id,
        )
        del device
        if grant.organization_id != organization_id:
            _deny()
        kind = data.kind
        if getattr(data, "correction_of", None) is not None or getattr(data, "depends_on", None) is not None:
            _deny()
        if kind == "move" and getattr(data, "destination_warehouse_id", None) not in {None, data.warehouse_id}:
            _deny()
        if kind in {"task_claim", "task_complete"}:
            task = await session.scalar(select(PhysicalTask).where(
                PhysicalTask.id == str(data.task_id),
                PhysicalTask.warehouse_id == data.warehouse_id,
            ))
            if task is None:
                _deny(404)
            if task.kind == "pick":
                if kind != "task_claim" or not {"task_claim", "pick"}.issubset(set(grant.actions)):
                    _deny()
            elif task.kind in {"putaway", "replenish"}:
                required = (
                    {"task_claim", task.kind, "task_complete"}
                    if kind == "task_claim" else {"task_complete", task.kind}
                )
                if not required.issubset(set(grant.actions)):
                    _deny()
            else:
                _deny()
        else:
            grant_action = "pack" if kind == "pack_v2" else kind
            require_action(grant, grant_action, grant_action)
        if receiving_authority is not None:
            _, grant, fresh, actor, _ = await revalidate_native_mobile_receiving_source_authority(
                session, core, principal, receiving_authority, proof, lock_warehouse=False,
            )
        if fulfillment_authority is not None:
            fulfillment_authority = replace(fulfillment_authority, proof=proof)
            _, grant, fresh, actor = await revalidate_mobile_fulfillment_source_authority(
                session, core, user, fulfillment_authority, None,
            )
            if grant.organization_id != organization_id or grant.warehouse_id != data.warehouse_id:
                _deny()
        stock_user = principal if receiving_authority is not None else fresh
        result = await PhysicalStock(
            core.services.stock, core.event_bus, services=core.services, user=stock_user, core=core,
            mobile_fulfillment_authority=fulfillment_authority,
        ).execute(session, organization_id, data, actor)
        if receiving_authority is not None:
            persisted = await session.scalar(select(PhysicalOperation).where(
                PhysicalOperation.organization_id == organization_id,
                PhysicalOperation.operation_id == str(data.operation_id),
                PhysicalOperation.warehouse_id == receiving_authority.warehouse_id,
            ).execution_options(populate_existing=True))
            if persisted is None:
                _deny(404)
            require_native_receipt_owner(persisted.result,
                receiving_authority.original_user_id, receiving_authority.original_sub)
            await protect_blind_receipt(session, persisted)
            await revalidate_native_mobile_receiving_source_authority(
                session, core, principal, receiving_authority, proof, lock_warehouse=False,
            )
            projection = native_receipt_v2_projection(persisted)
            return _receipt_view(persisted.result, native_v2=projection)
        if fulfillment_authority is not None:
            persisted = await session.scalar(select(PhysicalOperation).where(
                PhysicalOperation.organization_id == organization_id,
                PhysicalOperation.operation_id == str(data.operation_id),
                PhysicalOperation.warehouse_id == data.warehouse_id,
            ).execution_options(populate_existing=True))
            if persisted is None:
                _deny(404)
            native_pack_owner(persisted.result, fulfillment_authority, proof)
            projection = native_pack_v1_projection(persisted)
            await protect_blind_receipt(session, persisted)
            current_device, current_grant, _, current_actor = await revalidate_mobile_fulfillment_source_authority(
                session, core, user, fulfillment_authority, None,
            )
            if (current_grant.id != proof.grant_id or "receipt" not in current_grant.actions
                    or current_actor != actor or current_device.id != proof.device_id):
                _deny()
            return _receipt_view(persisted.result, native_pack_v1=projection)
        return _receipt_view(result)
    except HTTPException as exc:
        raise _safe_error(exc) from exc


@router.post("/transfers/commands", response_model=MobileTransferReceipt)
async def transfer_commands(
    request: Request, data: TransferCommand,
    org_id: Annotated[int, Query(gt=0, le=2147483647)],
    session=Depends(transaction, scope="function"), core=Depends(get_core),
    user: CurrentUser = Depends(get_current_user),
):
    try:
        identity = capture_native_mobile_receiving_source_authority(user, org_id, data.warehouse_id)
        principal = _native_mobile_receiving_source_seed(identity)
        proof = await _phase_proof(request, core, principal, require_grant=True)
        authority = OriginalMobileTransferAuthority(identity, data.kind, proof)
        _, _, _, actor = await revalidate_mobile_transfer_authority(session, core, authority)
        result = await PhysicalStock(
            core.services.stock, core.event_bus, services=core.services, user=principal, core=core,
            mobile_transfer_authority=authority,
        ).execute(session, org_id, data, actor)
        return _transfer_receipt_view(result)
    except HTTPException as exc:
        raise _safe_error(exc) from exc


@router.post(
    "/transfers/discrepancies/reports",
    response_model=MobileTransferDiscrepancyReportReceipt,
)
async def transfer_discrepancy_report(
    request: Request,
    data: TransferDiscrepancyReport,
    org_id: Annotated[int, Query(gt=0, le=2147483647)],
    session=Depends(transaction, scope="function"),
    core=Depends(get_core),
    user: CurrentUser = Depends(get_current_user),
):
    try:
        command = data.model_copy(deep=True)
        identity = capture_native_mobile_receiving_source_authority(
            user, org_id, command.warehouse_id,
        )
        principal = _native_mobile_receiving_source_seed(identity)
        proof = await _phase_proof(
            request, core, principal, require_grant=True,
            receiving_source_authority=identity,
            receiving_source_action=REPORT_KIND,
        )
        authority = OriginalMobileTransferAuthority(identity, REPORT_KIND, proof)
        _, _, _, actor = await revalidate_mobile_transfer_authority(
            session, core, authority,
        )
        stock = PhysicalStock(
            core.services.stock, core.event_bus, services=core.services,
            user=principal, core=core, mobile_transfer_authority=authority,
        )
        from modules.wms.transfer_loss_service import execute_mobile_report

        return await execute_mobile_report(stock, session, org_id, command, actor)
    except HTTPException as exc:
        raise _safe_error(exc) from exc


@router.get(
    "/transfers/discrepancies/reports/{operation_id}",
    response_model=MobileTransferDiscrepancyReportReceipt,
)
async def transfer_discrepancy_report_receipt(
    operation_id: UUID,
    request: Request,
    org_id: Annotated[int, Query(gt=0, le=2147483647)],
    session=Depends(transaction, scope="function"),
    core=Depends(get_core),
    user: CurrentUser = Depends(get_current_user),
):
    try:
        frozen = capture_native_mobile_receiving_source_authority(user, org_id, None)
        principal = _native_mobile_receiving_source_seed(frozen)
        proof = await _phase_proof(
            request, core, principal, require_grant=True,
            receiving_source_authority=frozen,
            receiving_source_action=REPORT_KIND,
            receiving_source_receipt=True,
        )
        _, grant, _, actor = await lock_and_revalidate(
            session, core, principal, proof,
            organization_id=org_id, warehouse_id=None,
        )
        if not {REPORT_KIND, "receipt"}.issubset(set(grant.actions)):
            _deny()
        authority = OriginalMobileTransferAuthority(
            replace(frozen, warehouse_id=grant.warehouse_id), REPORT_KIND, proof,
        )
        stock = PhysicalStock(
            core.services.stock, core.event_bus, services=core.services,
            user=principal, core=core, mobile_transfer_authority=authority,
        )
        from modules.wms.transfer_loss_service import read_mobile_report

        return await read_mobile_report(
            stock, session, org_id, grant.warehouse_id, str(operation_id), actor,
        )
    except HTTPException as exc:
        raise _safe_error(exc) from exc


@router.get("/transfers/incoming", response_model=MobileIncomingTransferManifest)
async def incoming_transfers(
    request: Request,
    org_id: Annotated[int, Query(gt=0, le=2147483647)],
    session=Depends(transaction, scope="function"), core=Depends(get_core),
    user: CurrentUser = Depends(get_current_user),
):
    try:
        frozen = capture_native_mobile_receiving_source_authority(user, org_id, None)
        principal = _native_mobile_receiving_source_seed(frozen)
        proof = await _phase_proof(request, core, principal, require_grant=True)
        _, grant, _, _ = await lock_and_revalidate(
            session, core, principal, proof, organization_id=org_id, warehouse_id=None,
        )
        authority = OriginalMobileTransferAuthority(
            replace(frozen, warehouse_id=grant.warehouse_id), "transfer_receive", proof,
        )
        _, _, _, actor = await revalidate_mobile_transfer_authority(session, core, authority, receipt=True)
        return await PhysicalStock(
            core.services.stock, core.event_bus, services=core.services, user=principal, core=core,
            mobile_transfer_authority=authority,
        ).incoming_transfer_manifest(session, org_id, grant.warehouse_id, actor)
    except HTTPException as exc:
        raise _safe_error(exc) from exc


@router.get("/transfers/receipts/{operation_id}", response_model=MobileTransferReceipt)
async def transfer_receipt(
    operation_id: UUID, request: Request,
    org_id: Annotated[int, Query(gt=0, le=2147483647)],
    session=Depends(transaction, scope="function"), core=Depends(get_core),
    user: CurrentUser = Depends(get_current_user),
):
    try:
        frozen = capture_native_mobile_receiving_source_authority(user, org_id, None)
        principal = _native_mobile_receiving_source_seed(frozen)
        proof = await _phase_proof(request, core, principal, require_grant=True)
        _, grant, _, _ = await lock_and_revalidate(
            session, core, principal, proof, organization_id=org_id, warehouse_id=None,
        )
        if "receipt" not in grant.actions:
            _deny()
        row = await session.scalar(select(PhysicalOperation).where(
            PhysicalOperation.organization_id == org_id,
            PhysicalOperation.warehouse_id == grant.warehouse_id,
            PhysicalOperation.operation_id == str(operation_id),
        ))
        if row is None or row.kind not in {"transfer_dispatch", "transfer_receive"}:
            _deny(404)
        authority = OriginalMobileTransferAuthority(replace(frozen, warehouse_id=grant.warehouse_id), row.kind, proof)
        await revalidate_mobile_transfer_authority(session, core, authority, receipt=True)
        await protect_blind_receipt(session, row)
        result = _transfer_receipt_view(row.result)
        if result["operation_id"] != row.operation_id or result["kind"] != row.kind or result["warehouse_id"] != row.warehouse_id:
            _deny(409)
        await revalidate_mobile_transfer_authority(session, core, authority, receipt=True)
        return result
    except HTTPException as exc:
        raise _safe_error(exc) from exc


async def _admin_command(session, core, principal: CurrentUser, data: AdminCommand):
    if isinstance(data, GrantApproval):
        device = await session.scalar(select(MobileDevice).where(
            MobileDevice.id == str(data.device_id)
        ).with_for_update().execution_options(populate_existing=True))
        if device is None or device.state == "revoked":
            _deny(404)
        old_device_state = device.state
        old_device_revision = device.revision
        admin = await _fresh_admin(session, principal)
        _is_admin(core, admin)
        admin_subject = admin.keycloak_user_id
        await core.services.accounting.source_member(session, data.organization_id, admin)
        warehouse = await control(session, data.warehouse_id, data.organization_id)
        from modules.wms.models import WarehouseDirectory

        directory = await session.get(WarehouseDirectory, data.warehouse_id)
        if directory is None or not directory.is_active or warehouse.organization_id != data.organization_id:
            _deny(404)
        if admin_subject == device.owner_sub:
            _deny()
        owner = await session.scalar(select(User).where(User.keycloak_user_id == device.owner_sub))
        if owner is None or owner.status != "active" or owner.role != "wms_mobile":
            _deny()
        owner_member = await session.scalar(select(AccessGrant.role).where(
            AccessGrant.organization_id == data.organization_id,
            AccessGrant.subject == device.owner_sub,
        ))
        if owner_member not in {"reader", "accountant", "chief"}:
            _deny()
        previous = await session.scalar(select(MobileGrant).where(
            MobileGrant.device_id == device.id,
            MobileGrant.organization_id == data.organization_id,
            MobileGrant.warehouse_id == data.warehouse_id,
        ).with_for_update().execution_options(populate_existing=True))
        old_grant_state = previous.state if previous is not None else None
        old_grant_revision = previous.revision if previous is not None else None
        old_grant_actions = sorted(previous.actions) if previous is not None else None
        now = datetime.now(UTC)
        if previous is None:
            previous = MobileGrant(
                id=str(uuid4()),
                device_id=device.id,
                organization_id=data.organization_id,
                warehouse_id=data.warehouse_id,
                actions=list(data.actions),
                state="active",
                revision=1,
                approved_by=admin_subject,
                approved_at=now,
                changed_by=admin_subject,
                changed_at=now,
            )
            session.add(previous)
        else:
            previous.actions = list(data.actions)
            previous.state = "active"
            previous.revision += 1
            previous.changed_by = admin_subject
            previous.changed_at = now
        device.state = "active"
        device.revision += 1
        device.approved_by = admin_subject
        device.approved_at = now
        await session.flush()
        _emit_access_change(
            session, core,
            actor_sub=admin_subject,
            entity_type="device",
            entity_id=device.id,
            organization_id=data.organization_id,
            warehouse_id=data.warehouse_id,
            old_state=old_device_state,
            new_state=device.state,
            old_revision=old_device_revision,
            new_revision=device.revision,
        )
        _emit_access_change(
            session, core,
            actor_sub=admin_subject,
            entity_type="grant",
            entity_id=previous.id,
            organization_id=previous.organization_id,
            warehouse_id=previous.warehouse_id,
            old_state=old_grant_state,
            new_state=previous.state,
            old_revision=old_grant_revision,
            new_revision=previous.revision,
            old_actions=old_grant_actions,
            new_actions=sorted(previous.actions),
        )
        return _grant_view(previous)

    if isinstance(data, GrantRestriction):
        snapshot = await session.scalar(select(MobileGrant).where(MobileGrant.id == str(data.grant_id)))
        if snapshot is None:
            _deny(404)
        device = await session.scalar(select(MobileDevice).where(
            MobileDevice.id == snapshot.device_id
        ).with_for_update().execution_options(populate_existing=True))
        if device is None:
            _deny(404)
        admin = await _fresh_admin(session, principal)
        _is_admin(core, admin)
        await core.services.accounting.source_member(session, snapshot.organization_id, admin)
        grant = await session.scalar(select(MobileGrant).where(
            MobileGrant.id == snapshot.id,
            MobileGrant.device_id == device.id,
        ).with_for_update().execution_options(populate_existing=True))
        if grant is None:
            _deny(404)
        if data.action == "grant_restrict":
            if device.state == "revoked" or grant.state != "active":
                _deny()
            requested = set(data.actions or [])
            if not requested.issubset(set(grant.actions)):
                _deny()
            old_grant_state = grant.state
            old_grant_revision = grant.revision
            old_grant_actions = sorted(grant.actions)
            old_device_state = device.state
            old_device_revision = device.revision
            grant.actions = sorted(requested)
            grant.revision += 1
            grant.changed_by = admin.keycloak_user_id
            grant.changed_at = datetime.now(UTC)
            device.revision += 1
            await session.flush()
            _emit_access_change(
                session, core,
                actor_sub=admin.keycloak_user_id,
                entity_type="grant",
                entity_id=grant.id,
                organization_id=grant.organization_id,
                warehouse_id=grant.warehouse_id,
                old_state=old_grant_state,
                new_state=grant.state,
                old_revision=old_grant_revision,
                new_revision=grant.revision,
                old_actions=old_grant_actions,
                new_actions=sorted(grant.actions),
            )
            _emit_access_change(
                session, core,
                actor_sub=admin.keycloak_user_id,
                entity_type="device",
                entity_id=device.id,
                organization_id=grant.organization_id,
                warehouse_id=grant.warehouse_id,
                old_state=old_device_state,
                new_state=device.state,
                old_revision=old_device_revision,
                new_revision=device.revision,
            )
        elif grant.state == "active":
            old_grant_state = grant.state
            old_grant_revision = grant.revision
            old_grant_actions = sorted(grant.actions)
            old_device_state = device.state
            old_device_revision = device.revision
            grant.state = "revoked"
            grant.revision += 1
            grant.changed_by = admin.keycloak_user_id
            grant.changed_at = datetime.now(UTC)
            device.revision += 1
            await session.flush()
            _emit_access_change(
                session, core,
                actor_sub=admin.keycloak_user_id,
                entity_type="grant",
                entity_id=grant.id,
                organization_id=grant.organization_id,
                warehouse_id=grant.warehouse_id,
                old_state=old_grant_state,
                new_state=grant.state,
                old_revision=old_grant_revision,
                new_revision=grant.revision,
                old_actions=old_grant_actions,
                new_actions=sorted(grant.actions),
            )
            _emit_access_change(
                session, core,
                actor_sub=admin.keycloak_user_id,
                entity_type="device",
                entity_id=device.id,
                organization_id=grant.organization_id,
                warehouse_id=grant.warehouse_id,
                old_state=old_device_state,
                new_state=device.state,
                old_revision=old_device_revision,
                new_revision=device.revision,
            )
        return _grant_view(grant)

    if isinstance(data, DeviceRevocation):
        device = await session.scalar(select(MobileDevice).where(
            MobileDevice.id == str(data.device_id)
        ).with_for_update().execution_options(populate_existing=True))
        if device is None:
            _deny(404)
        admin = await _fresh_admin(session, principal)
        _is_admin(core, admin, global_revoke=True)
        if device.state != "revoked":
            old_device_state = device.state
            old_device_revision = device.revision
            device.state = "revoked"
            device.revision += 1
            device.revoked_by = admin.keycloak_user_id
            device.revoked_at = datetime.now(UTC)
            grants = (await session.scalars(select(MobileGrant).where(
                MobileGrant.device_id == device.id,
                MobileGrant.state == "active",
            ).with_for_update())).all()
            for grant in grants:
                old_grant_state = grant.state
                old_grant_revision = grant.revision
                old_grant_actions = sorted(grant.actions)
                grant.state = "revoked"
                grant.revision += 1
                grant.changed_by = admin.keycloak_user_id
                grant.changed_at = datetime.now(UTC)
                _emit_access_change(
                    session, core,
                    actor_sub=admin.keycloak_user_id,
                    entity_type="grant",
                    entity_id=grant.id,
                    organization_id=grant.organization_id,
                    warehouse_id=grant.warehouse_id,
                    old_state=old_grant_state,
                    new_state=grant.state,
                    old_revision=old_grant_revision,
                    new_revision=grant.revision,
                    old_actions=old_grant_actions,
                    new_actions=sorted(grant.actions),
                )
            _emit_access_change(
                session, core,
                actor_sub=admin.keycloak_user_id,
                entity_type="device",
                entity_id=device.id,
                organization_id=None,
                warehouse_id=None,
                old_state=old_device_state,
                new_state=device.state,
                old_revision=old_device_revision,
                new_revision=device.revision,
            )
            await session.flush()
        return {"device_id": device.id, "state": device.state, "revision": device.revision}
    _deny()


@router.get("/state", response_model=MobileState)
async def state(
    request: Request,
    org_id: Annotated[int, Query(gt=0)],
    warehouse_id: Annotated[int, Query(gt=0)],
    session=Depends(transaction, scope="function"),
    core=Depends(get_core),
    user: CurrentUser = Depends(get_current_user),
):
    source_authority: OriginalMobileFulfillmentSourceAuthority = capture_native_mobile_fulfillment_source_authority(
        user, org_id, warehouse_id, purpose="state",
    )
    proof = await _phase_proof(
        request, core, user, require_grant=True,
        fulfillment_source_authority=source_authority,
    )
    source_authority = replace(source_authority, proof=proof)
    try:
        _, grant, fresh_user, _ = await revalidate_mobile_fulfillment_source_authority(
            session, core, user, source_authority, None,
        )
        # Enter and finish every exact producer-source lock set before the locked WMS gate.
        await control(session, warehouse_id, org_id, lock=False)
        tasks = (await session.scalars(select(PhysicalTask).where(
            PhysicalTask.warehouse_id == warehouse_id,
            PhysicalTask.kind != "cap_place",
            PhysicalTask.status.in_(["open", "claimed"]),
        ).order_by(PhysicalTask.created_at, PhysicalTask.id))).all()
        from modules.wms.physical_packing import task_deliveries

        stock = PhysicalStock(
            core.services.stock, core.event_bus, services=core.services,
            user=fresh_user, core=core,
        )
        delivery_by_task = await task_deliveries(
            session, stock, org_id, warehouse_id, tasks,
            mobile_authority=source_authority,
        )
        bound_task_ids = {task.id for task in tasks if task.binding_revision_id is not None}
        if not set(delivery_by_task).issubset(bound_task_ids):
            raise HTTPException(409, "Снимок доставки не совпадает с заданиями склада")

        # The first locked WMS control gate follows prepare-all/collect-all.
        cfg = await control(session, warehouse_id, org_id)
        places = (await session.scalars(select(PhysicalLocation).where(
            PhysicalLocation.warehouse_id == warehouse_id
        ).order_by(PhysicalLocation.code))).all()
        locked = set((await session.scalars(select(CountLocationLock.location_id)
            .join(PhysicalLocation, CountLocationLock.location_id == PhysicalLocation.location_id)
            .where(PhysicalLocation.warehouse_id == warehouse_id))).all())
        units = (await session.scalars(select(StockUnit).where(
            StockUnit.warehouse_id == warehouse_id, StockUnit.qty > 0
        ).order_by(StockUnit.id))).all()
        units = _on_hand_units(units)
        units = [unit for unit in units if unit.location_id not in locked]
        unit_rows = []
        for unit in units:
            reason, _ = await eligible(session, core.services.stock, cfg, unit)
            unit_data = unit_output(unit, reason)
            unit_rows.append({**_unit_view(unit_data), "packaging_id": unit_data["packaging_id"]})
        visible_ids = {unit.id for unit in units}
        task_rows = []
        visible_tasks = []
        for task in tasks:
            if task.unit_id and task.unit_id not in visible_ids:
                continue
            visible_tasks.append(task)
            hold = await session.get(PhysicalHold, task.hold_id) if task.hold_id else None
            task_rows.append({
                "id": task.id, "kind": task.kind, "status": task.status,
                "unit_id": task.unit_id, "hold_id": task.hold_id,
                "qty": decimal_text(task.qty), "destination_id": task.destination_id,
                "container": hold.container if hold else None,
                "delivery": None,
            })
        visible_by_id = {task.id: task for task in visible_tasks}
        for task_row in task_rows:
            task = visible_by_id[task_row["id"]]
            task_row["delivery"] = (
                delivery_by_task.get(task.id) if task.binding_revision_id is not None else None
            )
        counts = (await session.scalars(select(PhysicalCount).where(
            PhysicalCount.warehouse_id == warehouse_id
        ).order_by(PhysicalCount.created_at.desc()))).all()
        picks = []
        if {"pack", "ship"}.intersection(set(grant.actions)):
            clone_type, original_type = aliased(StockUnit), aliased(StockUnit)
            picked_rows = (await session.execute(
                select(PickedAllocation, PhysicalHold, clone_type, original_type)
                .join(PhysicalHold, PickedAllocation.hold_id == PhysicalHold.id)
                .outerjoin(clone_type, PickedAllocation.unit_id == clone_type.id)
                .outerjoin(original_type, PhysicalHold.unit_id == original_type.id)
                .where(
                    PhysicalHold.warehouse_id == warehouse_id,
                    PickedAllocation.canceled.is_(False),
                    PickedAllocation.shipped < PickedAllocation.qty,
                )
                .order_by(PickedAllocation.id)
                .execution_options(populate_existing=True)
            )).all()
            for pick, hold, clone, original in picked_rows:
                if clone is None or original is None:
                    raise HTTPException(409, "Подобранная единица или исходная партия не найдена")
                if clone.location_id in locked or (
                    original.location_id in locked
                ):
                    continue
                picks.append(await picked_output(
                    session, core.services.stock, cfg, pick, hold, clone, original,
                ))
                picks[-1]["binding_revision_id"] = pick.binding_revision_id
        return {
            "warehouse_id": warehouse_id,
            "mode": cfg.mode,
            "locations": [{
                "id": item.location_id, "code": item.code, "kind": item.kind,
                "count_locked": item.location_id in locked,
                "capacity_qty": decimal_text(item.capacity_qty) if item.capacity_qty is not None else None,
            } for item in places],
            "units": unit_rows,
            "picks": picks,
            "tasks": task_rows,
            "counts": [{
                "id": item.id, "kind": item.kind, "status": item.status,
                "locations": item.locations, "completed_locations": item.completed_locations,
            } for item in counts],
        }
    except HTTPException as exc:
        raise _safe_error(exc) from exc


@router.get("/scan", response_model=MobileScan)
async def scan(
    request: Request,
    org_id: Annotated[int, Query(gt=0)],
    warehouse_id: Annotated[int, Query(gt=0)],
    value: Annotated[str, Query(min_length=1, max_length=512)],
    session=Depends(transaction, scope="function"),
    core=Depends(get_core),
    user: CurrentUser = Depends(get_current_user),
):
    proof = await _phase_proof(request, core, user, require_grant=True)
    try:
        _, grant, _, _ = await lock_and_revalidate(
            session, core, user, proof, organization_id=org_id, warehouse_id=warehouse_id
        )
        require_action(grant, "scan")
        await control(session, warehouse_id, org_id)
        from modules.wms.item_identity import resolve_item_scan

        resolved = await resolve_item_scan(
            session, org_id, value, warehouse_id=warehouse_id,
        )
        return {
            "packaging_id": resolved["packaging_id"], "sku_code": resolved["sku_code"],
            "factor": resolved["factor"], "sealed_allowed": resolved["sealed_allowed"],
            "gs1": resolved["gs1"],
            "dates_require_confirmation": resolved["dates_require_confirmation"],
        }
    except HTTPException as exc:
        raise _safe_error(exc) from exc


@router.get("/receipts/{operation_id}", response_model=MobileReceipt)
async def receipt(
    operation_id: UUID,
    request: Request,
    org_id: Annotated[int, Query(gt=0)],
    session=Depends(transaction, scope="function"),
    core=Depends(get_core),
    user: CurrentUser = Depends(get_current_user),
):
    # The warehouse is discovered only through a verified current server grant.
    frozen = capture_native_mobile_receiving_source_authority(user, org_id, None)
    principal = _native_mobile_receiving_source_seed(frozen)
    proof = await _phase_proof(request, core, principal, require_grant=True, receiving_source_authority=frozen)
    try:
        device, grant, _, _ = await lock_and_revalidate(
            session, core, principal, proof, organization_id=org_id, warehouse_id=None
        )
        if "receipt" not in grant.actions:
            _deny()
        row = await session.scalar(select(PhysicalOperation).where(
            PhysicalOperation.organization_id == org_id,
            PhysicalOperation.operation_id == str(operation_id),
            PhysicalOperation.warehouse_id == grant.warehouse_id,
        ))
        if row is None:
            raise HTTPException(status_code=404, detail="Запись не найдена")
        if row.kind in ("transfer_dispatch", "transfer_receive"):
            raise HTTPException(status_code=404, detail="Запись не найдена")
        pack_owner_present = isinstance(row.result, dict) and "native_pack_owner_v1" in row.result
        pack_owner = native_pack_owner(row.result)
        package_proof = row.result.get("shipping_packages_v1") if isinstance(row.result, dict) else None
        causal_request = (
            row.result.get("physical_causal_v1", {}).get("canonical_request")
            if isinstance(row.result, dict) and isinstance(row.result.get("physical_causal_v1"), dict)
            else None
        )
        binding_claimed = (
            isinstance(row.result, dict) and "binding_revision_id" in row.result
            or isinstance(package_proof, dict) and "binding_revision_id" in package_proof
            or isinstance(causal_request, dict) and "binding_revision_id" in causal_request
        )
        bound_measured_pack = (
            row.kind == "pack_v2" and (pack_owner_present or binding_claimed)
        )
        if bound_measured_pack and pack_owner is None:
            _deny(404)
        if pack_owner is not None:
            if not bound_measured_pack:
                _deny(404)
            pack_identity = replace(frozen, warehouse_id=grant.warehouse_id)
            pack_authority = OriginalMobileFulfillmentSourceAuthority(
                pack_identity, "pack", proof,
            )
            current_device, current_grant, _, _ = await revalidate_mobile_fulfillment_source_authority(
                session, core, principal, pack_authority, None,
            )
            if (current_device.id != device.id or current_grant.id != grant.id
                    or not {"pack", "receipt"}.issubset(set(current_grant.actions))):
                _deny(404)
            native_pack_owner(row.result, pack_authority, proof)
            measured_projection = native_pack_v1_projection(row)
            await protect_blind_receipt(session, row)
            final_device, final_grant, _, _ = await revalidate_mobile_fulfillment_source_authority(
                session, core, principal, pack_authority, None,
            )
            if (final_device.id != device.id or final_grant.id != grant.id
                    or not {"pack", "receipt"}.issubset(set(final_grant.actions))):
                _deny(404)
            return _receipt_view(row.result, native_pack_v1=measured_projection)
        from modules.wms.item_identity import ITEM_IDENTITY_METADATA_KINDS
        from modules.wms.physical_capacity import measurement_operation_indicator
        from modules.wms.physical_stock import responsible_operation_indicator
        from modules.wms.placement_models import work_operation_indicator, placement_operation_indicator
        from modules.wms.transfer_loss_protocol import loss_operation_indicator

        if placement_operation_indicator(row.kind, row.result) or loss_operation_indicator(row.kind, row.result) or responsible_operation_indicator(row.kind, row.result) or work_operation_indicator(row.kind, row.result) or measurement_operation_indicator(row.kind, row.result) or row.kind in ITEM_IDENTITY_METADATA_KINDS:
            raise HTTPException(status_code=404, detail="Запись не найдена")
        owner_present = isinstance(row.result, dict) and "native_receipt_owner_v1" in row.result
        native_receive = owner_present or (
            row.kind == "receive" and isinstance(row.result, dict) and "native_receipt_source_v1" in row.result
        )
        if native_receive:
            authority = replace(frozen, warehouse_id=grant.warehouse_id)
            require_native_receipt_owner(row.result, authority.original_user_id, authority.original_sub)
            if owner_present:
                native_receipt_v2_projection(row)
            _, current_grant, _, _, _ = await revalidate_native_mobile_receiving_source_authority(
                session, core, principal, authority, proof, lock_warehouse=False,
            )
            if "receipt" not in current_grant.actions:
                _deny()
        await protect_blind_receipt(session, row)
        if native_receive:
            _, current_grant, _, _, _ = await revalidate_native_mobile_receiving_source_authority(
                session, core, principal, authority, proof, lock_warehouse=False,
            )
            if "receipt" not in current_grant.actions:
                _deny()
            return _receipt_view(row.result, native_v2=native_receipt_v2_projection(row))
        _, current_grant, _, _ = await lock_and_revalidate(
            session, core, principal, proof, organization_id=org_id, warehouse_id=grant.warehouse_id,
        )
        if "receipt" not in current_grant.actions:
            _deny()
        return _receipt_view(row.result)
    except HTTPException as exc:
        raise _safe_error(exc) from exc


@router.get("/counts/{count_id}", response_model=MobileCount)
async def count_view(
    count_id: UUID,
    request: Request,
    org_id: Annotated[int, Query(gt=0)],
    session=Depends(transaction, scope="function"),
    core=Depends(get_core),
    user: CurrentUser = Depends(get_current_user),
):
    proof = await _phase_proof(request, core, user, require_grant=True)
    try:
        _, grant, _, _ = await lock_and_revalidate(
            session, core, user, proof, organization_id=org_id, warehouse_id=None
        )
        if "count_read" not in grant.actions:
            _deny()
        count = await session.get(PhysicalCount, str(count_id))
        if count is None or count.warehouse_id != grant.warehouse_id:
            raise HTTPException(status_code=404, detail="Запись не найдена")
        rows = (await session.scalars(select(PhysicalCountLine).where(
            PhysicalCountLine.count_id == count.id
        ).order_by(PhysicalCountLine.id))).all()
        return {
            "count_id": count.id,
            "status": count.status,
            "locations": count.locations,
            "completed_locations": count.completed_locations,
            "lines": [{
                "location_id": row.location_id,
                "sku_code": row.dimensions["sku_code"],
                "batch_id": row.dimensions["batch_id"],
                "qty": decimal_text(row.qty) if row.qty is not None else None,
                "quality": row.dimensions["quality"],
                "package_state": row.dimensions["package_state"],
                "container": row.dimensions["container"],
            } for row in rows],
        }
    except HTTPException as exc:
        raise _safe_error(exc) from exc
