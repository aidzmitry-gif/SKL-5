from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Any, Literal, TypedDict
from uuid import UUID

from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select

from core.domain.models import Sku, User
from modules.wms.gs1 import parse_gs1
from modules.wms.models import WarehouseDirectory
from modules.wms.physical_models import (
    CurrentStockStateError,
    Packaging,
    PhysicalOperation,
    StoragePolicy,
    require_current_on_hand,
)
from modules.wms.physical_schemas import TransferCommand, WarehouseCommand

_ORIGIN_WAREHOUSE_COMMAND_ADAPTER = TypeAdapter(WarehouseCommand)
_ORIGIN_TRANSFER_COMMAND_ADAPTER = TypeAdapter(TransferCommand)

MAX_DATABASE_ID = 2_147_483_647
MAX_SCAN_LENGTH = 256
_BASE_SCAN = re.compile(r"SKU-([1-9][0-9]*)\Z")
_PACKAGE_SCAN = re.compile(r"ERP-P-([1-9][0-9]*)\Z")
_RESERVED_PREFIXES = ("sku-", "erp-p-")


class ItemScanDTO(TypedDict):
    sku_id: int
    sku_code: str
    packaging_id: int | None
    internal_code: str
    namespace: Literal["internal_base", "internal_package", "factory", "gs1"]
    factor: str
    sealed_allowed: bool
    gs1: dict[str, Any] | None
    dates_require_confirmation: bool


def _reserved_looking(value: str) -> bool:
    normalized = value.strip().casefold()
    return normalized.startswith(_RESERVED_PREFIXES)


def _native_identity(value: str) -> tuple[str, int] | None:
    match = _BASE_SCAN.fullmatch(value)
    if match:
        identity, raw_id = "internal_base", match.group(1)
    else:
        match = _PACKAGE_SCAN.fullmatch(value)
        if match:
            identity, raw_id = "internal_package", match.group(1)
        elif _reserved_looking(value):
            raise HTTPException(422, "Внутренний код товара или упаковки имеет неверный формат")
        else:
            return None

    if len(raw_id) > 10 or int(raw_id) > MAX_DATABASE_ID:
        raise HTTPException(422, "Числовая часть внутреннего кода выходит за допустимый диапазон")
    return identity, int(raw_id)


def reject_reserved_packaging_barcode(value: str) -> None:
    if _reserved_looking(value):
        raise HTTPException(422, "Этот префикс зарезервирован для внутреннего кода ERP")


async def _reject_legacy_collision(session, organization_id: int, code: str) -> None:
    # Python casefold handles Unicode folds that SQL lower() may not match.
    candidates = (await session.scalars(
        select(Packaging.barcode).where(Packaging.organization_id == organization_id)
    )).all()
    normalized_code = code.casefold()
    if any(candidate.strip().casefold() == normalized_code for candidate in candidates):
        raise HTTPException(409, "В каталоге организации внутренний код конфликтует со старым штрихкодом")


async def _warehouse_policy(session, organization_id: int, warehouse_id: int, sku_code: str, factor):
    warehouse = await session.scalar(select(WarehouseDirectory).where(
        WarehouseDirectory.id == warehouse_id,
        WarehouseDirectory.organization_id == organization_id,
        WarehouseDirectory.is_active.is_(True),
    ))
    if warehouse is None:
        raise HTTPException(404, "Активный склад организации не найден")

    # Keep the shared resolver independent of physical_stock's module import graph.
    from modules.wms.physical_stock import control, decimal_text, policy

    await control(session, warehouse_id, organization_id, lock=False)
    await policy(session, warehouse_id, sku_code, factor)
    return decimal_text


async def resolve_item_scan(
    session,
    organization_id: int,
    raw_scan: str,
    *,
    warehouse_id: int | None = None,
) -> ItemScanDTO:
    if not isinstance(raw_scan, str) or not raw_scan or len(raw_scan) > MAX_SCAN_LENGTH:
        raise HTTPException(422, "Скан должен содержать от 1 до 256 символов")
    if warehouse_id is not None and (
        type(warehouse_id) is not int or warehouse_id <= 0 or warehouse_id > MAX_DATABASE_ID
    ):
        raise HTTPException(422, "Идентификатор склада выходит за допустимый диапазон")

    native = _native_identity(raw_scan)
    gs1 = None
    if native is not None:
        namespace, native_id = native
        internal_code = f"SKU-{native_id}" if namespace == "internal_base" else f"ERP-P-{native_id}"
        await _reject_legacy_collision(session, organization_id, internal_code)
        if namespace == "internal_base":
            sku = await session.get(Sku, native_id)
            if sku is None or not sku.is_active:
                raise HTTPException(404, "Активный товар с таким внутренним кодом не найден")
            packaging = None
        else:
            packaging = await session.scalar(select(Packaging).where(
                Packaging.id == native_id,
                Packaging.organization_id == organization_id,
            ))
            if packaging is None:
                raise HTTPException(404, "Упаковка с таким внутренним кодом не найдена в организации")
            sku = await session.scalar(select(Sku).where(
                Sku.code == packaging.sku_code,
                Sku.is_active.is_(True),
            ))
            if sku is None:
                raise HTTPException(404, "Активный товар упаковки не найден")
    else:
        if raw_scan.startswith(("]", "(01)", "01")) and len(raw_scan) >= 16:
            try:
                gs1 = parse_gs1(raw_scan)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        barcode = gs1["gtin"] if gs1 else raw_scan
        packaging = await session.scalar(select(Packaging).where(
            Packaging.organization_id == organization_id,
            Packaging.barcode == barcode,
        ))
        if packaging is None:
            raise HTTPException(404, "Штрихкод не имеет подтверждённого соответствия товару и упаковке")
        sku = await session.scalar(select(Sku).where(
            Sku.code == packaging.sku_code,
            Sku.is_active.is_(True),
        ))
        if sku is None:
            raise HTTPException(404, "Активный товар упаковки не найден")
        namespace = "gs1" if gs1 else "factory"
        internal_code = f"ERP-P-{packaging.id}"

    if warehouse_id is not None:
        factor = packaging.factor if packaging is not None else None
        decimal_text = await _warehouse_policy(
            session, organization_id, warehouse_id, sku.code, factor,
        )
    else:
        from modules.wms.physical_stock import decimal_text

    factor_text = decimal_text(packaging.factor) if packaging is not None else "1"
    return {
        "sku_id": sku.id,
        "sku_code": sku.code,
        "packaging_id": packaging.id if packaging is not None else None,
        "internal_code": internal_code,
        "namespace": namespace,
        "factor": factor_text,
        "sealed_allowed": bool(packaging.sealed_allowed) if packaging is not None else False,
        "gs1": gs1 if namespace == "gs1" else None,
        "dates_require_confirmation": namespace == "gs1",
    }

ITEM_IDENTITY_ENSURE_PROTOCOL = "wms.item-label-metadata.v1"
ITEM_IDENTITY_ENSURE_KIND = "item_identity_ensure"
ITEM_IDENTITY_METADATA_KINDS = frozenset({
    ITEM_IDENTITY_ENSURE_KIND,
    "item_label_prepare",
    "item_label_verify",
})
ITEM_LABEL_PREPARE_KIND = "item_label_prepare"
ITEM_LABEL_VERIFY_KIND = "item_label_verify"


def _ensure_hash_payload(organization_id: int, request: dict, owner: dict) -> dict:
    return {
        "protocol": ITEM_IDENTITY_ENSURE_PROTOCOL,
        "schema_version": 1,
        "operation_id": request["operation_id"],
        "kind": ITEM_IDENTITY_ENSURE_KIND,
        "organization_id": organization_id,
        "warehouse_id": request["warehouse_id"],
        "owner": owner,
        "request": request,
    }


async def _ensure_fresh_identity(session, core, principal, expected_sub: str):
    from core.services.auth import (
        EffectiveIdentityLookupError,
        has_permission,
        resolve_effective_oidc_user,
    )

    try:
        fresh = await resolve_effective_oidc_user(
            replace(principal, local_status=None), session,
        )
    except EffectiveIdentityLookupError as exc:
        raise HTTPException(503, "Текущую учётную запись нельзя проверить") from exc
    if (
        fresh.keycloak_user_id != expected_sub
        or fresh.local_status != "active"
        or not has_permission(core, fresh, "wms.operate")
    ):
        raise HTTPException(403, "Текущий доступ не разрешает операцию WMS")
    return fresh


async def _ensure_initial_owner(
    session, core, organization_id: int, principal, *,
    mobile_authority=None, required_action=None, expected_warehouse_id=None,
):
    if mobile_authority is not None:
        from modules.wms.mobile_access import (
            OriginalMobileMetadataAuthority,
        )

        if (
            not isinstance(mobile_authority, OriginalMobileMetadataAuthority)
            or mobile_authority.organization_id != organization_id
            or mobile_authority.action != required_action
            or type(expected_warehouse_id) is not int
            or mobile_authority.warehouse_id != expected_warehouse_id
        ):
            raise HTTPException(403, "Мобильное полномочие метаданных не подтверждено")
        return mobile_authority.original_user_id, mobile_authority.original_sub

    subject = principal.keycloak_user_id
    if (
        not isinstance(subject, str)
        or not subject.strip()
        or len(subject) > 64
    ):
        raise HTTPException(403, "Требуется действующая управляемая OIDC-учётная запись")
    fresh = await _ensure_fresh_identity(session, core, principal, subject)
    managed = await session.scalar(select(User).where(
        User.keycloak_user_id == subject,
    ).execution_options(populate_existing=True))
    if (
        managed is None
        or managed.status != "active"
        or managed.keycloak_user_id != subject
        or not managed.role
        or managed.role not in fresh.roles
    ):
        raise HTTPException(403, "Требуется активная управляемая OIDC-учётная запись")
    owner_id = managed.id

    accounting = getattr(getattr(core, "services", None), "accounting", None)
    if accounting is None:
        raise HTTPException(503, "Сервис проверки членства недоступен")
    actor = await accounting.source_member(session, organization_id, fresh)
    if actor != subject:
        raise HTTPException(403, "Текущее членство организации не подтверждено")
    return owner_id, subject


async def _ensure_revalidate_after_wait(
    session, core, organization_id: int, principal, user_id: int, subject: str,
    *, mobile_authority=None, mobile_only=False,
):
    if mobile_only and mobile_authority is None:
        return principal
    if mobile_authority is not None:
        from modules.wms.mobile_access import revalidate_mobile_metadata_authority

        if (
            mobile_authority.organization_id != organization_id
            or mobile_authority.original_user_id != user_id
            or mobile_authority.original_sub != subject
        ):
            raise HTTPException(403, "Мобильное полномочие метаданных изменилось")
        _, _, fresh, _, _ = await revalidate_mobile_metadata_authority(
            session, core, principal, mobile_authority,
        )
        return fresh

    managed = await session.scalar(select(User).where(
        User.id == user_id,
    ).with_for_update().execution_options(populate_existing=True))
    if (
        managed is None
        or managed.id != user_id
        or managed.keycloak_user_id != subject
        or managed.status != "active"
        or not managed.role
    ):
        raise HTTPException(403, "Управляемая OIDC-учётная запись изменилась")

    fresh = await _ensure_fresh_identity(session, core, principal, subject)
    if managed.role not in fresh.roles:
        raise HTTPException(403, "Текущая роль управляемой учётной записи изменилась")
    accounting = getattr(getattr(core, "services", None), "accounting", None)
    if accounting is None:
        raise HTTPException(503, "Сервис проверки членства недоступен")
    actor = await accounting.source_member(session, organization_id, fresh)
    if actor != subject:
        raise HTTPException(403, "Текущее членство организации не подтверждено")
    return fresh


async def _ensure_existing_receipt(
    session,
    organization_id: int,
    data,
    owner: dict,
    request_hash: str,
):
    from modules.wms.physical_schemas import ItemIdentityEnsureEnvelope
    from modules.wms.physical_stock import digest

    row = await session.scalar(select(PhysicalOperation).where(
        PhysicalOperation.organization_id == organization_id,
        PhysicalOperation.operation_id == str(data.operation_id),
    ).execution_options(populate_existing=True))
    if row is None:
        return None
    if row.kind != ITEM_IDENTITY_ENSURE_KIND:
        raise HTTPException(409, "Идентификатор операции уже использован другим видом")
    try:
        saved = ItemIdentityEnsureEnvelope.model_validate(row.result)
    except ValidationError as exc:
        raise HTTPException(409, "Сохранённая запись идентичности повреждена") from exc

    saved_owner = saved.owner.model_dump(mode="json")
    if row.actor != saved.owner.sub:
        raise HTTPException(409, "Автор сохранённой записи не совпадает с конвертом")
    if saved.owner.user_id != owner["user_id"] or saved.owner.sub != owner["sub"]:
        raise HTTPException(403, "Сохранённая запись принадлежит другой учётной записи")

    saved_request = saved.request.model_dump(mode="json")
    current_request = data.model_dump(mode="json")
    try:
        saved_hash = digest(_ensure_hash_payload(
            organization_id, saved_request, saved_owner,
        ))
    except (TypeError, ValueError) as exc:
        raise HTTPException(409, "Хеш сохранённой записи не проверен") from exc
    if (
        row.organization_id != organization_id
        or row.operation_id != str(data.operation_id)
        or row.warehouse_id != data.warehouse_id
        or saved.organization_id != organization_id
        or saved.operation_id != data.operation_id
        or saved.warehouse_id != data.warehouse_id
        or saved_request != current_request
        or saved.request_hash != row.request_hash
        or saved_hash != row.request_hash
        or saved_hash != request_hash
    ):
        raise HTTPException(409, "Идентификатор операции уже использован с другими данными")
    return saved


async def ensure_item_identity(
    session, core, organization_id: int, data, principal, *, mobile_authority=None,
):
    """Persist one immutable metadata receipt; the route owns the root transaction."""
    from modules.wms.physical_schemas import ItemIdentityEnsureEnvelope
    from modules.wms.physical_stock import control, digest, policy

    if not session.in_transaction():
        raise HTTPException(409, "Требуется транзакция маршрута метаданных")
    if session.new or session.dirty or session.deleted:
        raise HTTPException(409, "Транзакция содержит посторонние несохранённые изменения")

    user_id, subject = await _ensure_initial_owner(
        session, core, organization_id, principal,
        mobile_authority=mobile_authority,
        required_action=ITEM_IDENTITY_ENSURE_KIND,
        expected_warehouse_id=data.warehouse_id,
    )
    # source_member holds the organization lock; capture identity first, then
    # lock that exact persisted row and re-resolve current access after the wait.
    await _ensure_revalidate_after_wait(
        session, core, organization_id, principal, user_id, subject,
        mobile_authority=mobile_authority,
    )
    owner = {"user_id": user_id, "sub": subject}
    request = data.model_dump(mode="json")
    request_hash = digest(_ensure_hash_payload(organization_id, request, owner))

    existing = await _ensure_existing_receipt(
        session, organization_id, data, owner, request_hash,
    )
    if existing is not None:
        await _ensure_revalidate_after_wait(
            session, core, organization_id, principal, user_id, subject,
            mobile_authority=mobile_authority, mobile_only=True,
        )
        return existing

    await control(session, data.warehouse_id, organization_id, lock=True)
    await _ensure_revalidate_after_wait(
        session, core, organization_id, principal, user_id, subject,
        mobile_authority=mobile_authority,
    )
    # The scoped organization lock serializes the UUID. Recheck after the
    # warehouse wait and before consulting any current selected entity.
    existing = await _ensure_existing_receipt(
        session, organization_id, data, owner, request_hash,
    )
    if existing is not None:
        await _ensure_revalidate_after_wait(
            session, core, organization_id, principal, user_id, subject,
            mobile_authority=mobile_authority, mobile_only=True,
        )
        return existing

    async def store_domain_rejection(code):
        envelope = ItemIdentityEnsureEnvelope.model_validate({
            **_ensure_hash_payload(organization_id, request, owner),
            "request_hash": request_hash,
            "recorded_at": datetime.now(timezone.utc),
            "outcome": {"status": "metadata_rejected", "code": code},
        })
        session.add(PhysicalOperation(
            organization_id=organization_id,
            operation_id=str(data.operation_id),
            warehouse_id=data.warehouse_id,
            kind=ITEM_IDENTITY_ENSURE_KIND,
            request_hash=request_hash,
            result=envelope.model_dump(mode="json"),
            actor=subject,
        ))
        await session.flush()
        await _ensure_revalidate_after_wait(
            session, core, organization_id, principal, user_id, subject,
            mobile_authority=mobile_authority, mobile_only=True,
        )
        return envelope

    warehouse = await session.scalar(select(WarehouseDirectory).where(
        WarehouseDirectory.id == data.warehouse_id,
        WarehouseDirectory.organization_id == organization_id,
        WarehouseDirectory.is_active.is_(True),
    ).execution_options(populate_existing=True))
    await _ensure_revalidate_after_wait(
        session, core, organization_id, principal, user_id, subject,
        mobile_authority=mobile_authority, mobile_only=True,
    )
    if warehouse is None:
        raise HTTPException(404, "Активный склад организации не найден")

    sku = await session.scalar(select(Sku).where(
        Sku.id == data.sku_id,
    ).with_for_update().execution_options(populate_existing=True))
    await _ensure_revalidate_after_wait(
        session, core, organization_id, principal, user_id, subject,
        mobile_authority=mobile_authority,
    )
    if sku is None or not sku.is_active:
        return await store_domain_rejection("source_unavailable")

    packaging = None
    if data.packaging_id is not None:
        packaging = await session.scalar(select(Packaging).where(
            Packaging.id == data.packaging_id,
            Packaging.organization_id == organization_id,
        ).with_for_update().execution_options(populate_existing=True))
        if packaging is None:
            await _ensure_revalidate_after_wait(
                session, core, organization_id, principal, user_id, subject,
                mobile_authority=mobile_authority, mobile_only=True,
            )
            raise HTTPException(404, "Упаковка не найдена в организации")
        await _ensure_revalidate_after_wait(
            session, core, organization_id, principal, user_id, subject,
            mobile_authority=mobile_authority,
        )
        if packaging.sku_code != sku.code:
            raise HTTPException(409, "Упаковка не принадлежит выбранной номенклатуре")

    resolved = await resolve_item_scan(
        session,
        organization_id,
        f"SKU-{sku.id}" if packaging is None else f"ERP-P-{packaging.id}",
    )
    await _ensure_revalidate_after_wait(
        session, core, organization_id, principal, user_id, subject,
        mobile_authority=mobile_authority, mobile_only=True,
    )
    if (
        resolved["sku_id"] != sku.id
        or resolved["packaging_id"] != data.packaging_id
        or resolved["sku_code"] != sku.code
        or resolved["internal_code"] != (
            f"SKU-{sku.id}" if packaging is None else f"ERP-P-{packaging.id}"
        )
    ):
        raise HTTPException(409, "Разрешённая идентичность не совпадает с выбранными объектами")

    selected_policy = await session.scalar(select(StoragePolicy).where(
        StoragePolicy.warehouse_id == data.warehouse_id,
        StoragePolicy.sku_code == sku.code,
    ).execution_options(populate_existing=True))
    if selected_policy is None:
        await _ensure_revalidate_after_wait(
            session, core, organization_id, principal, user_id, subject,
            mobile_authority=mobile_authority,
        )
        return await store_domain_rejection("source_unavailable")
    await _ensure_revalidate_after_wait(
        session, core, organization_id, principal, user_id, subject,
        mobile_authority=mobile_authority, mobile_only=True,
    )
    selected_policy = await policy(
        session,
        data.warehouse_id,
        sku.code,
        packaging.factor if packaging is not None else None,
    )
    await _ensure_revalidate_after_wait(
        session, core, organization_id, principal, user_id, subject,
        mobile_authority=mobile_authority, mobile_only=True,
    )
    if sku.unit != selected_policy.base_unit:
        raise HTTPException(409, "Единица товара не совпадает с подтверждённой единицей политики")

    if session.new or session.dirty or session.deleted:
        raise HTTPException(409, "Транзакция содержит посторонние несохранённые изменения")

    outcome = {
        "sku_id": sku.id,
        "sku_code": sku.code,
        "packaging_id": packaging.id if packaging is not None else None,
        "internal_code": f"SKU-{sku.id}" if packaging is None else f"ERP-P-{packaging.id}",
        "snapshot": {
            "sku_unit": sku.unit,
            "base_unit": selected_policy.base_unit,
            "precision": selected_policy.precision,
            "factor": resolved["factor"] if packaging is not None else "1",
            "sealed_allowed": resolved["sealed_allowed"] if packaging is not None else False,
        },
    }
    try:
        envelope = ItemIdentityEnsureEnvelope.model_validate({
            **_ensure_hash_payload(organization_id, request, owner),
            "request_hash": request_hash,
            "recorded_at": datetime.now(timezone.utc),
            "outcome": outcome,
        })
    except ValidationError as exc:
        raise HTTPException(409, "Снимок идентичности не прошёл проверку") from exc

    session.add(PhysicalOperation(
        organization_id=organization_id,
        operation_id=str(data.operation_id),
        warehouse_id=data.warehouse_id,
        kind=ITEM_IDENTITY_ENSURE_KIND,
        request_hash=request_hash,
        result=envelope.model_dump(mode="json"),
        actor=subject,
    ))
    await session.flush()
    await _ensure_revalidate_after_wait(
        session, core, organization_id, principal, user_id, subject,
        mobile_authority=mobile_authority, mobile_only=True,
    )
    return envelope


class _ItemLabelDomainRejection(Exception):
    def __init__(self, code):
        self.code = code


def _item_label_hash_payload(kind, organization_id, request, owner):
    return {
        "protocol": ITEM_IDENTITY_ENSURE_PROTOCOL,
        "schema_version": 1,
        "operation_id": request["operation_id"],
        "kind": kind,
        "organization_id": organization_id,
        "warehouse_id": request["warehouse_id"],
        "owner": owner,
        "request": request,
    }


async def _item_label_existing_receipt(session, organization_id, data, owner, kind, request_hash):
    from modules.wms.physical_schemas import ItemLabelPrepareEnvelope, ItemLabelVerifyEnvelope
    from modules.wms.physical_stock import digest

    row = await session.scalar(select(PhysicalOperation).where(
        PhysicalOperation.organization_id == organization_id,
        PhysicalOperation.operation_id == str(data.operation_id),
    ).execution_options(populate_existing=True))
    if row is None:
        return None
    if row.kind != kind:
        raise HTTPException(409, "Идентификатор операции уже использован другим видом")
    envelope_type = (
        ItemLabelPrepareEnvelope if kind == ITEM_LABEL_PREPARE_KIND
        else ItemLabelVerifyEnvelope
    )
    try:
        saved = envelope_type.model_validate(row.result)
    except ValidationError as exc:
        raise HTTPException(409, "Сохранённая запись метаданных повреждена") from exc
    saved_owner = saved.owner.model_dump(mode="json")
    if row.actor != saved.owner.sub:
        raise HTTPException(409, "Автор сохранённой записи не совпадает с конвертом")
    if saved.owner.user_id != owner["user_id"] or saved.owner.sub != owner["sub"]:
        raise HTTPException(403, "Сохранённая запись принадлежит другой учётной записи")
    saved_request = saved.request.model_dump(mode="json")
    current_request = data.model_dump(mode="json")
    try:
        saved_hash = digest(_item_label_hash_payload(
            kind, organization_id, saved_request, saved_owner,
        ))
    except (TypeError, ValueError) as exc:
        raise HTTPException(409, "Хеш сохранённой записи не проверен") from exc
    if (
        row.organization_id != organization_id
        or row.operation_id != str(data.operation_id)
        or row.warehouse_id != data.warehouse_id
        or saved.organization_id != organization_id
        or saved.operation_id != data.operation_id
        or saved.warehouse_id != data.warehouse_id
        or saved_request != current_request
        or saved.request_hash != row.request_hash
        or saved_hash != row.request_hash
        or saved_hash != request_hash
    ):
        raise HTTPException(409, "Идентификатор операции уже использован с другими данными")
    return saved, row


async def _item_label_start(
    session, core, organization_id, data, principal, kind, *, mobile_authority=None,
):
    from modules.wms.physical_stock import control, digest, protect_blind_receipt

    if not session.in_transaction():
        raise HTTPException(409, "Требуется транзакция маршрута метаданных")
    if session.new or session.dirty or session.deleted:
        raise HTTPException(409, "Транзакция содержит посторонние несохранённые изменения")
    user_id, subject = await _ensure_initial_owner(
        session, core, organization_id, principal,
        mobile_authority=mobile_authority,
        required_action={
            ITEM_LABEL_PREPARE_KIND: "item_label_prepare",
            ITEM_LABEL_VERIFY_KIND: "item_label_verify",
        }.get(kind),
        expected_warehouse_id=data.warehouse_id,
    )
    await _ensure_revalidate_after_wait(
        session, core, organization_id, principal, user_id, subject,
        mobile_authority=mobile_authority,
    )
    owner = {"user_id": user_id, "sub": subject}
    request = data.model_dump(mode="json")
    request_hash = digest(_item_label_hash_payload(kind, organization_id, request, owner))

    await _item_label_existing_receipt(
        session, organization_id, data, owner, kind, request_hash,
    )
    cfg = await control(session, data.warehouse_id, organization_id, lock=True)
    await _ensure_revalidate_after_wait(
        session, core, organization_id, principal, user_id, subject,
        mobile_authority=mobile_authority,
    )
    # Serialize the blind-count barrier with count admission, then re-read the
    # immutable receipt before projecting it or consulting current source rows.
    existing = await _item_label_existing_receipt(
        session, organization_id, data, owner, kind, request_hash,
    )
    if existing is not None:
        saved, row = existing
        await protect_blind_receipt(session, row)
        await _ensure_revalidate_after_wait(
            session, core, organization_id, principal, user_id, subject,
            mobile_authority=mobile_authority,
        )
        return {"saved": saved}
    await protect_blind_receipt(session, SimpleNamespace(
        kind=kind, organization_id=organization_id,
        warehouse_id=data.warehouse_id, result={},
    ))
    await _ensure_revalidate_after_wait(
        session, core, organization_id, principal, user_id, subject,
        mobile_authority=mobile_authority,
    )
    return {
        "cfg": cfg,
        "owner": owner,
        "subject": subject,
        "request": request,
        "request_hash": request_hash,
        "user_id": user_id,
    }


async def _item_label_saved_reference(
    session, organization_id, warehouse_id, operation_id, expected_kind, owner,
):
    from modules.wms.physical_schemas import (
        ItemIdentityEnsureEnvelope,
        ItemLabelPrepareEnvelope,
    )
    from modules.wms.physical_stock import digest

    row = await session.scalar(select(PhysicalOperation).where(
        PhysicalOperation.organization_id == organization_id,
        PhysicalOperation.operation_id == str(operation_id),
    ).execution_options(populate_existing=True))
    if row is None or row.kind != expected_kind:
        raise HTTPException(409, "Ссылка на подтверждённую метаданную запись не найдена")
    envelope_type = {
        ITEM_IDENTITY_ENSURE_KIND: ItemIdentityEnsureEnvelope,
        ITEM_LABEL_PREPARE_KIND: ItemLabelPrepareEnvelope,
    }.get(expected_kind)
    if envelope_type is None:
        raise HTTPException(409, "Недопустимый вид ссылки метаданных")
    try:
        saved = envelope_type.model_validate(row.result)
    except ValidationError as exc:
        raise HTTPException(409, "Сохранённая ссылка метаданных повреждена") from exc
    saved_owner = saved.owner.model_dump(mode="json")
    if row.actor != saved.owner.sub:
        raise HTTPException(409, "Автор сохранённой ссылки не совпадает с конвертом")
    if saved.owner.user_id != owner["user_id"] or saved.owner.sub != owner["sub"]:
        raise HTTPException(403, "Ссылка метаданных принадлежит другой учётной записи")
    saved_request = saved.request.model_dump(mode="json")
    try:
        saved_hash = (
            digest(_ensure_hash_payload(organization_id, saved_request, saved_owner))
            if expected_kind == ITEM_IDENTITY_ENSURE_KIND
            else digest(_item_label_hash_payload(
                expected_kind, organization_id, saved_request, saved_owner,
            ))
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(409, "Хеш ссылки метаданных не проверен") from exc
    if (
        row.organization_id != organization_id
        or row.operation_id != str(operation_id)
        or row.warehouse_id != warehouse_id
        or saved.organization_id != organization_id
        or saved.operation_id != operation_id
        or saved.warehouse_id != warehouse_id
        or saved.request_hash != row.request_hash
        or saved_hash != row.request_hash
    ):
        raise HTTPException(409, "Ссылка метаданных не совпадает с сохранённой записью")
    return saved


async def _item_label_store(
    session, organization_id, data, owner, subject, request, request_hash, kind, outcome,
):
    from modules.wms.physical_schemas import ItemLabelPrepareEnvelope, ItemLabelVerifyEnvelope

    envelope_type = (
        ItemLabelPrepareEnvelope if kind == ITEM_LABEL_PREPARE_KIND
        else ItemLabelVerifyEnvelope
    )
    try:
        envelope = envelope_type.model_validate({
            "protocol": ITEM_IDENTITY_ENSURE_PROTOCOL,
            "schema_version": 1,
            "operation_id": request["operation_id"],
            "kind": kind,
            "organization_id": organization_id,
            "warehouse_id": request["warehouse_id"],
            "owner": owner,
            "request": request,
            "request_hash": request_hash,
            "recorded_at": datetime.now(timezone.utc),
            "outcome": outcome,
        })
    except ValidationError as exc:
        raise HTTPException(409, "Результат метаданных не прошёл проверку") from exc
    if session.new or session.dirty or session.deleted:
        raise HTTPException(409, "Транзакция содержит посторонние несохранённые изменения")
    session.add(PhysicalOperation(
        organization_id=organization_id,
        operation_id=str(data.operation_id),
        warehouse_id=data.warehouse_id,
        kind=kind,
        request_hash=request_hash,
        result=envelope.model_dump(mode="json"),
        actor=subject,
    ))
    await session.flush()
    return envelope


async def _item_label_resolve_scan(
    session, core, organization_id, principal, user_id, subject, raw_scan,
    rejection_code, *, mobile_authority=None,
):
    try:
        resolved = await resolve_item_scan(session, organization_id, raw_scan)
    except HTTPException as exc:
        if exc.status_code in (404, 422):
            await _ensure_revalidate_after_wait(
                session, core, organization_id, principal, user_id, subject,
                mobile_authority=mobile_authority,
            )
            raise _ItemLabelDomainRejection(rejection_code) from exc
        raise
    await _ensure_revalidate_after_wait(
        session, core, organization_id, principal, user_id, subject,
        mobile_authority=mobile_authority,
    )
    return resolved


async def _item_label_current_context(
    session, core, organization_id, warehouse_id, warehouse_control, unit_id,
    mode, expected_snapshot, principal, user_id, subject, *, mobile_authority=None,
):
    from core.domain.models import Sku
    from modules.wms.models import Location, WarehouseDirectory
    from modules.wms.physical_models import Packaging, PhysicalLocation, StockUnit, StoragePolicy
    from modules.wms.physical_stock import decimal_text, digest, dimensions, policy

    directory = await session.scalar(select(WarehouseDirectory).where(
        WarehouseDirectory.id == warehouse_id,
        WarehouseDirectory.organization_id == organization_id,
        WarehouseDirectory.is_active.is_(True),
    ).execution_options(populate_existing=True))
    await _ensure_revalidate_after_wait(
        session, core, organization_id, principal, user_id, subject,
        mobile_authority=mobile_authority,
    )
    if directory is None:
        raise _ItemLabelDomainRejection("context_stale")

    unit = await session.scalar(select(StockUnit).where(
        StockUnit.id == str(unit_id),
        StockUnit.warehouse_id == warehouse_id,
    ).with_for_update().execution_options(populate_existing=True))
    await _ensure_revalidate_after_wait(
        session, core, organization_id, principal, user_id, subject,
        mobile_authority=mobile_authority,
    )
    if unit is None:
        raise _ItemLabelDomainRejection("source_unavailable")
    try:
        require_current_on_hand(unit)
    except CurrentStockStateError as exc:
        raise _ItemLabelDomainRejection("source_unavailable") from exc

    sku = await session.scalar(select(Sku).where(
        Sku.code == unit.sku_code,
        Sku.is_active.is_(True),
    ).with_for_update().execution_options(populate_existing=True))
    await _ensure_revalidate_after_wait(
        session, core, organization_id, principal, user_id, subject,
        mobile_authority=mobile_authority,
    )
    if sku is None:
        raise _ItemLabelDomainRejection("source_unavailable")

    packaging = None
    if unit.packaging_id is not None:
        packaging = await session.scalar(select(Packaging).where(
            Packaging.id == unit.packaging_id,
            Packaging.organization_id == organization_id,
        ).with_for_update().execution_options(populate_existing=True))
        await _ensure_revalidate_after_wait(
            session, core, organization_id, principal, user_id, subject,
            mobile_authority=mobile_authority,
        )
        if packaging is None:
            raise HTTPException(409, "Ссылка остатка на упаковку не подтверждена организацией")
        if packaging.sku_code != sku.code:
            raise HTTPException(409, "Ссылка остатка на упаковку не совпадает с товаром")

    selected_policy = await session.scalar(select(StoragePolicy).where(
        StoragePolicy.warehouse_id == warehouse_id,
        StoragePolicy.sku_code == sku.code,
    ).with_for_update().execution_options(populate_existing=True))
    await _ensure_revalidate_after_wait(
        session, core, organization_id, principal, user_id, subject,
        mobile_authority=mobile_authority,
    )
    if selected_policy is None:
        raise _ItemLabelDomainRejection("source_unavailable")
    # Keep the shared facade's packaging-factor and precision checks after the
    # scoped absence check so only a confirmed missing policy is durable.
    try:
        await policy(
            session, warehouse_id, sku.code,
            packaging.factor if packaging is not None else None,
        )
    except HTTPException as exc:
        if exc.status_code == 422:
            await _ensure_revalidate_after_wait(
                session, core, organization_id, principal, user_id, subject,
                mobile_authority=mobile_authority,
            )
            raise _ItemLabelDomainRejection("source_unavailable") from exc
        raise
    await _ensure_revalidate_after_wait(
        session, core, organization_id, principal, user_id, subject,
        mobile_authority=mobile_authority,
    )
    if sku.unit != selected_policy.base_unit:
        raise _ItemLabelDomainRejection("context_stale")

    batch_row = await core.services.stock.batch_by_id(
        session, organization_id, unit.batch_id,
    )
    await _ensure_revalidate_after_wait(
        session, core, organization_id, principal, user_id, subject,
        mobile_authority=mobile_authority,
    )
    if batch_row is None:
        raise _ItemLabelDomainRejection("source_unavailable")
    if not isinstance(batch_row, dict):
        raise HTTPException(503, "Фасад партии вернул результат неверного типа")
    if batch_row.get("sku_code") != unit.sku_code:
        raise _ItemLabelDomainRejection("source_unavailable")
    required_batch_fields = {
        "id", "sku_code", "lot_no", "mfg_date", "expiry_date",
        "date_source", "manufacture_precision",
    }
    if not required_batch_fields.issubset(batch_row):
        raise HTTPException(503, "Фасад партии вернул неполные метаданные")
    if (
        type(batch_row["id"]) is not int
        or batch_row["id"] != unit.batch_id
        or batch_row["id"] < 1
        or batch_row["id"] > MAX_DATABASE_ID
        or not isinstance(batch_row["lot_no"], str)
        or not isinstance(batch_row["sku_code"], str)
        or not isinstance(batch_row["mfg_date"], (str, type(None)))
        or not isinstance(batch_row["expiry_date"], (str, type(None)))
        or not isinstance(batch_row["date_source"], (str, type(None)))
        or not isinstance(batch_row["manufacture_precision"], (str, type(None)))
    ):
        raise HTTPException(503, "Фасад партии вернул метаданные неверного типа")
    if len(batch_row["lot_no"]) > 64:
        raise _ItemLabelDomainRejection("source_unavailable")
    for date_key in ("mfg_date", "expiry_date"):
        if batch_row[date_key] is not None:
            try:
                if date.fromisoformat(batch_row[date_key]).isoformat() != batch_row[date_key]:
                    raise ValueError
            except ValueError as exc:
                raise HTTPException(503, "Фасад партии вернул некорректную дату") from exc
    batch_view = {key: batch_row[key] for key in (
        "id", "sku_code", "lot_no", "mfg_date", "expiry_date",
        "date_source", "manufacture_precision",
    )}
    if unit.assembly_output_id is not None:
        from modules.wms.physical_stock import effective_lot
        effective = await effective_lot(session, core.services.stock, organization_id, unit)
        batch_view = {key: effective["batch_id"] if key == "id" else effective[key] for key in batch_view}
        await _ensure_revalidate_after_wait(session, core, organization_id, principal, user_id, subject,
            mobile_authority=mobile_authority)
    if (
        (
            selected_policy.expiry_required
            or selected_policy.min_shelf_days
            or selected_policy.manufacture_required
            or selected_policy.max_age_days
        )
        and not batch_view["date_source"]
    ) or (
        (selected_policy.expiry_required or selected_policy.min_shelf_days)
        and batch_view["expiry_date"] is None
    ) or (
        (selected_policy.manufacture_required or selected_policy.max_age_days)
        and batch_view["mfg_date"] is None
    ) or (
        selected_policy.max_age_days
        and batch_view["manufacture_precision"] != "day"
    ):
        raise _ItemLabelDomainRejection("source_unavailable")

    place = await session.scalar(select(PhysicalLocation).where(
        PhysicalLocation.location_id == unit.location_id,
        PhysicalLocation.warehouse_id == warehouse_id,
    ).with_for_update().execution_options(populate_existing=True))
    await _ensure_revalidate_after_wait(
        session, core, organization_id, principal, user_id, subject,
        mobile_authority=mobile_authority,
    )
    if place is None:
        raise _ItemLabelDomainRejection("source_unavailable")
    original_place = await session.get(Location, place.location_id)
    await _ensure_revalidate_after_wait(
        session, core, organization_id, principal, user_id, subject,
        mobile_authority=mobile_authority,
    )
    if original_place is None or not original_place.is_active:
        raise _ItemLabelDomainRejection("source_unavailable")
    if (
        not isinstance(sku.title, str)
        or len(sku.title) > 255
        or not isinstance(place.code, str)
        or len(place.code) > 64
    ):
        raise _ItemLabelDomainRejection("source_unavailable")

    native_code = f"SKU-{sku.id}" if packaging is None else f"ERP-P-{packaging.id}"
    native = await _item_label_resolve_scan(
        session, core, organization_id, principal, user_id, subject,
        native_code, "source_unavailable", mobile_authority=mobile_authority,
    )
    if (
        native["sku_id"] != sku.id
        or native["packaging_id"] != unit.packaging_id
        or native["sku_code"] != sku.code
        or native["internal_code"] != native_code
    ):
        raise _ItemLabelDomainRejection("context_stale")

    if mode == "internal":
        item_code = native_code
    else:
        if packaging is None or not packaging.barcode:
            raise _ItemLabelDomainRejection("source_unavailable")
        if len(packaging.barcode) > 64:
            raise _ItemLabelDomainRejection("source_unavailable")
        item_code = packaging.barcode
        factory = await _item_label_resolve_scan(
            session, core, organization_id, principal, user_id, subject,
            item_code, "source_unavailable", mobile_authority=mobile_authority,
        )
        if (
            factory["namespace"] not in ("factory", "gs1")
            or factory["sku_id"] != sku.id
            or factory["packaging_id"] != unit.packaging_id
            or factory["sku_code"] != sku.code
        ):
            raise _ItemLabelDomainRejection("source_unavailable")

    snapshot = {
        "sku_unit": sku.unit,
        "base_unit": selected_policy.base_unit,
        "precision": selected_policy.precision,
        "factor": decimal_text(packaging.factor) if packaging is not None else "1",
        "sealed_allowed": bool(packaging.sealed_allowed) if packaging is not None else False,
    }
    if expected_snapshot is not None:
        expected = expected_snapshot.model_dump(mode="json") if hasattr(expected_snapshot, "model_dump") else expected_snapshot
        if expected != snapshot:
            raise _ItemLabelDomainRejection("context_stale")

    origin = await resolve_current_unit_origin(
        session, organization_id, warehouse_id, unit.id,
    )
    await _ensure_revalidate_after_wait(
        session, core, organization_id, principal, user_id, subject,
        mobile_authority=mobile_authority,
    )
    if (
        not isinstance(origin, dict)
        or origin.get("status") != "proven"
        or origin.get("organization_id") != organization_id
        or origin.get("warehouse_id") != warehouse_id
        or origin.get("current_unit") != {
            "unit_id": unit.id,
            "sku_code": unit.sku_code,
            "batch_id": unit.batch_id,
            "packaging_id": unit.packaging_id,
        }
        or not _origin_digest_text(origin.get("proof_digest"))
    ):
        raise _ItemLabelDomainRejection("origin_unproven")

    unit_view = {
        "id": unit.id,
        **dimensions(unit),
        "qty": decimal_text(unit.qty),
        "reserved": decimal_text(unit.reserved),
    }
    unit_digest = digest(unit_view)
    batch_code = f"LOT-{unit.batch_id}-{unit.id.upper()}"
    location_code = f"LOC-{place.location_id}"
    policy_view = {
        "warehouse_id": selected_policy.warehouse_id,
        "sku_code": selected_policy.sku_code,
        "precision": selected_policy.precision,
        "base_unit": selected_policy.base_unit,
        "expiry_required": selected_policy.expiry_required,
        "manufacture_required": selected_policy.manufacture_required,
        "min_shelf_days": selected_policy.min_shelf_days,
        "max_age_days": selected_policy.max_age_days,
        "maintenance_days": selected_policy.maintenance_days,
        "manufacturer_source": selected_policy.manufacturer_source,
        "storage_rule": selected_policy.storage_rule,
        "warranty_rule": selected_policy.warranty_rule,
        "count_class": selected_policy.count_class,
    }
    context_basis = {
        "protocol": ITEM_IDENTITY_ENSURE_PROTOCOL,
        "schema_version": 1,
        "organization_id": organization_id,
        "warehouse_id": warehouse_id,
        "mode": warehouse_control.mode,
        "label_mode": mode,
        "unit_digest": unit_digest,
        "sku_id": sku.id,
        "sku_code": sku.code,
        "packaging_id": packaging.id if packaging is not None else None,
        "snapshot": snapshot,
        "policy": policy_view,
        "batch": batch_view,
        "location_id": place.location_id,
        "location_title": place.code,
        "item_code": item_code,
        "batch_code": batch_code,
        "location_code": location_code,
        "proof_digest": origin["proof_digest"],
    }
    return {
        "unit": unit,
        "sku": sku,
        "packaging": packaging,
        "snapshot": snapshot,
        "batch": batch_view,
        "place": place,
        "origin": origin,
        "unit_digest": unit_digest,
        "context_digest": digest(context_basis),
        "proof_digest": origin["proof_digest"],
        "item_code": item_code,
        "batch_code": batch_code,
        "location_code": location_code,
    }


async def prepare_item_label(
    session, core, organization_id: int, data, principal, *, mobile_authority=None,
):
    from modules.wms.physical_schemas import ItemIdentityEnsureRejectedOutcome

    started = await _item_label_start(
        session, core, organization_id, data, principal, ITEM_LABEL_PREPARE_KIND,
        mobile_authority=mobile_authority,
    )
    if "saved" in started:
        return started["saved"]
    owner, subject = started["owner"], started["subject"]
    request, request_hash, user_id = started["request"], started["request_hash"], started["user_id"]
    try:
        ensure = await _item_label_saved_reference(
            session, organization_id, data.warehouse_id, data.ensure_operation_id,
            ITEM_IDENTITY_ENSURE_KIND, owner,
        )
        await _ensure_revalidate_after_wait(
            session, core, organization_id, principal, user_id, subject,
            mobile_authority=mobile_authority,
        )
        if isinstance(ensure.outcome, ItemIdentityEnsureRejectedOutcome):
            raise _ItemLabelDomainRejection("source_unavailable")
        current = await _item_label_current_context(
            session, core, organization_id, data.warehouse_id, started["cfg"],
            data.unit_id, data.label_mode, ensure.outcome.snapshot,
            principal, user_id, subject, mobile_authority=mobile_authority,
        )
        if (
            ensure.outcome.sku_id == current["sku"].id
            and ensure.outcome.sku_code != current["sku"].code
        ):
            raise HTTPException(409, "Сохранённый код товара противоречит неизменяемой идентичности")
        if (
            ensure.outcome.sku_id != current["sku"].id
            or ensure.outcome.packaging_id != current["unit"].packaging_id
            or ensure.request.warehouse_id != data.warehouse_id
        ):
            raise _ItemLabelDomainRejection("context_stale")
        unit_id = UUID(current["unit"].id)
        payload = {
            "schema_version": 1,
            "label_mode": data.label_mode,
            "sku_code": current["sku"].code,
            "sku_title": current["sku"].title,
            "item_code": current["item_code"],
            "batch_code": current["batch_code"],
            "lot_no": current["batch"]["lot_no"],
            "location_code": current["location_code"],
            "location_title": current["place"].code,
        }
        outcome = {
            "status": "label_prepared",
            "ensure_operation_id": ensure.operation_id,
            "ensure_request_hash": ensure.request_hash,
            "unit_id": unit_id,
            "sku_id": current["sku"].id,
            "sku_code": current["sku"].code,
            "packaging_id": current["unit"].packaging_id,
            "snapshot": current["snapshot"],
            "label_mode": data.label_mode,
            "batch_id": current["unit"].batch_id,
            "location_id": current["place"].location_id,
            "unit_digest": current["unit_digest"],
            "context_digest": current["context_digest"],
            "proof_digest": current["proof_digest"],
            "item_code": current["item_code"],
            "batch_code": current["batch_code"],
            "location_code": current["location_code"],
            "print_payload": payload,
        }
    except _ItemLabelDomainRejection as rejected:
        await _ensure_revalidate_after_wait(
            session, core, organization_id, principal, user_id, subject,
            mobile_authority=mobile_authority,
        )
        stored = await _item_label_store(
            session, organization_id, data, owner, subject, request,
            request_hash, ITEM_LABEL_PREPARE_KIND,
            {"status": "metadata_rejected", "code": rejected.code},
        )
        await _ensure_revalidate_after_wait(
            session, core, organization_id, principal, user_id, subject,
            mobile_authority=mobile_authority, mobile_only=True,
        )
        return stored
    await _ensure_revalidate_after_wait(
        session, core, organization_id, principal, user_id, subject,
        mobile_authority=mobile_authority,
    )
    stored = await _item_label_store(
        session, organization_id, data, owner, subject, request,
        request_hash, ITEM_LABEL_PREPARE_KIND, outcome,
    )
    await _ensure_revalidate_after_wait(
        session, core, organization_id, principal, user_id, subject,
        mobile_authority=mobile_authority, mobile_only=True,
    )
    return stored


async def verify_item_label(
    session, core, organization_id: int, data, principal, *, mobile_authority=None,
):
    from modules.wms.physical_schemas import (
        ItemIdentityEnsureRejectedOutcome,
        ItemLabelPrepareOutcome,
    )

    started = await _item_label_start(
        session, core, organization_id, data, principal, ITEM_LABEL_VERIFY_KIND,
        mobile_authority=mobile_authority,
    )
    if "saved" in started:
        return started["saved"]
    owner, subject = started["owner"], started["subject"]
    request, request_hash, user_id = started["request"], started["request_hash"], started["user_id"]
    try:
        prepared = await _item_label_saved_reference(
            session, organization_id, data.warehouse_id, data.prepare_operation_id,
            ITEM_LABEL_PREPARE_KIND, owner,
        )
        await _ensure_revalidate_after_wait(
            session, core, organization_id, principal, user_id, subject,
            mobile_authority=mobile_authority,
        )
        if isinstance(prepared.outcome, ItemIdentityEnsureRejectedOutcome):
            raise _ItemLabelDomainRejection("source_unavailable")
        if not isinstance(prepared.outcome, ItemLabelPrepareOutcome):
            raise HTTPException(409, "Сохранённая подготовка имеет неверный результат")
        prepared_outcome = prepared.outcome
        ensure = await _item_label_saved_reference(
            session, organization_id, data.warehouse_id,
            prepared_outcome.ensure_operation_id, ITEM_IDENTITY_ENSURE_KIND, owner,
        )
        await _ensure_revalidate_after_wait(
            session, core, organization_id, principal, user_id, subject,
            mobile_authority=mobile_authority,
        )
        if isinstance(ensure.outcome, ItemIdentityEnsureRejectedOutcome):
            raise _ItemLabelDomainRejection("source_unavailable")
        if (
            ensure.request_hash != prepared_outcome.ensure_request_hash
            or ensure.outcome.sku_id != prepared_outcome.sku_id
            or ensure.outcome.sku_code != prepared_outcome.sku_code
            or ensure.outcome.packaging_id != prepared_outcome.packaging_id
            or ensure.outcome.snapshot.model_dump(mode="json")
            != prepared_outcome.snapshot.model_dump(mode="json")
        ):
            raise HTTPException(409, "Подготовка не совпадает с исходной записью ENSURE")

        scanned = await _item_label_resolve_scan(
            session, core, organization_id, principal, user_id, subject,
            data.item_scan, "scan_mismatch", mobile_authority=mobile_authority,
        )
        if prepared_outcome.label_mode == "internal":
            matches_item = (
                data.item_scan == prepared_outcome.item_code
                and scanned["namespace"] in ("internal_base", "internal_package")
            )
        else:
            matches_item = scanned["namespace"] in ("factory", "gs1")
        if (
            not matches_item
            or scanned["sku_id"] != prepared_outcome.sku_id
            or scanned["packaging_id"] != prepared_outcome.packaging_id
            or scanned["sku_code"] != prepared_outcome.sku_code
        ):
            raise _ItemLabelDomainRejection("scan_mismatch")

        current = await _item_label_current_context(
            session, core, organization_id, data.warehouse_id, started["cfg"],
            prepared_outcome.unit_id, prepared_outcome.label_mode,
            prepared_outcome.snapshot, principal, user_id, subject,
            mobile_authority=mobile_authority,
        )
        if (
            current["unit"].id != str(prepared_outcome.unit_id)
            or current["sku"].id != prepared_outcome.sku_id
            or current["unit"].packaging_id != prepared_outcome.packaging_id
            or current["unit"].batch_id != prepared_outcome.batch_id
            or current["place"].location_id != prepared_outcome.location_id
            or current["item_code"] != prepared_outcome.item_code
            or current["batch_code"] != prepared_outcome.batch_code
            or current["location_code"] != prepared_outcome.location_code
            or current["unit_digest"] != prepared_outcome.unit_digest
            or current["context_digest"] != prepared_outcome.context_digest
            or current["proof_digest"] != prepared_outcome.proof_digest
        ):
            raise _ItemLabelDomainRejection("context_stale")
        if (
            data.batch_scan != current["batch_code"]
            or data.location_scan != current["location_code"]
        ):
            raise _ItemLabelDomainRejection("scan_mismatch")

        gs1 = scanned["gs1"]
        if gs1 is not None:
            batch_view = current["batch"]
            if (
                (gs1.get("lot_no") is not None and gs1["lot_no"] != batch_view["lot_no"])
                or (
                    gs1.get("manufacture") is not None
                    and (
                        gs1["manufacture"]["value"] != batch_view["mfg_date"]
                        or gs1["manufacture"]["precision"]
                        != batch_view["manufacture_precision"]
                    )
                )
                or (
                    gs1.get("expiry") is not None
                    and gs1["expiry"]["value"] != batch_view["expiry_date"]
                )
            ):
                raise _ItemLabelDomainRejection("scan_mismatch")

        outcome = {
            "status": "scan_verified",
            "prepare_operation_id": prepared.operation_id,
            "prepare_request_hash": prepared.request_hash,
            "ensure_operation_id": ensure.operation_id,
            "unit_id": prepared_outcome.unit_id,
            "sku_id": prepared_outcome.sku_id,
            "packaging_id": prepared_outcome.packaging_id,
            "label_mode": prepared_outcome.label_mode,
            "unit_digest": current["unit_digest"],
            "context_digest": current["context_digest"],
            "proof_digest": current["proof_digest"],
            "item_scan": data.item_scan,
            "batch_scan": data.batch_scan,
            "location_scan": data.location_scan,
        }
    except _ItemLabelDomainRejection as rejected:
        await _ensure_revalidate_after_wait(
            session, core, organization_id, principal, user_id, subject,
            mobile_authority=mobile_authority,
        )
        stored = await _item_label_store(
            session, organization_id, data, owner, subject, request,
            request_hash, ITEM_LABEL_VERIFY_KIND,
            {"status": "metadata_rejected", "code": rejected.code},
        )
        await _ensure_revalidate_after_wait(
            session, core, organization_id, principal, user_id, subject,
            mobile_authority=mobile_authority, mobile_only=True,
        )
        return stored
    await _ensure_revalidate_after_wait(
        session, core, organization_id, principal, user_id, subject,
        mobile_authority=mobile_authority,
    )
    stored = await _item_label_store(
        session, organization_id, data, owner, subject, request,
        request_hash, ITEM_LABEL_VERIFY_KIND, outcome,
    )
    await _ensure_revalidate_after_wait(
        session, core, organization_id, principal, user_id, subject,
        mobile_authority=mobile_authority, mobile_only=True,
    )
    return stored


class _OriginUnproven(Exception):
    """A stored physical-history inconsistency, not an infrastructure failure."""


_ORIGIN_DIMENSION_KEYS = (
    "warehouse_id", "location_id", "sku_code", "batch_id", "characteristic", "purpose",
    "quality", "packaging_id", "package_state", "container", "last_checked", "next_service",
    "inspection_ref", "service_result", "arrived_at", "arrival_source",
)


def _origin_qty(value):
    if (
        not isinstance(value, str)
        or len(value) > 22
        or re.fullmatch(r"-?(?:0|[1-9][0-9]{0,13})(?:\.[0-9]{1,6})?", value) is None
    ):
        raise _OriginUnproven
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise _OriginUnproven from exc
    try:
        quantized = result.quantize(Decimal("0.000001"))
    except InvalidOperation as exc:
        raise _OriginUnproven from exc
    if not result.is_finite() or result != quantized:
        raise _OriginUnproven
    return result


def _origin_uuid_text(value):
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _origin_digest_text(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _origin_frontier_tokens(frontier):
    from modules.wms.physical_stock import _origin_frontier_key

    tokens = _origin_frontier_key(frontier)
    for row in frontier["ledgers"]:
        if type(row["ledger_id"]) is not int or row["ledger_id"] <= 0 or not _origin_digest_text(row["row_digest"]):
            raise ValueError("physical_origin_ledger_token")
    for row in frontier["observations"]:
        if row.get("kind") == "placement_zero_v1":
            continue
        if (
            type(row["operation_pk"]) is not int or row["operation_pk"] <= 0
            or type(row["count_line_id"]) is not int or row["count_line_id"] <= 0
            or not _origin_digest_text(row["observation_digest"])
        ):
            raise ValueError("physical_origin_observation_token")
    for row in frontier["opaque_counts"]:
        if (
            type(row["operation_pk"]) is not int or row["operation_pk"] <= 0
            or not _origin_uuid_text(row["operation_id"])
            or not _origin_digest_text(row["row_digest"])
        ):
            raise ValueError("physical_origin_opaque_count_token")
    return tokens


def _origin_dimensions(value):
    if not isinstance(value, dict) or set(value) != set(_ORIGIN_DIMENSION_KEYS):
        raise _OriginUnproven
    from modules.wms.physical_stock import _origin_dimension_snapshot_valid

    if not _origin_dimension_snapshot_valid(value):
        raise _OriginUnproven
    return {key: value[key] for key in _ORIGIN_DIMENSION_KEYS}


def _origin_result_dimensions(value):
    if not isinstance(value, dict) or not set(_ORIGIN_DIMENSION_KEYS).issubset(value):
        raise _OriginUnproven
    return _origin_dimensions({key: value[key] for key in _ORIGIN_DIMENSION_KEYS})


def _origin_transfer_dimensions(value):
    if not isinstance(value, dict) or set(value) != set(_ORIGIN_DIMENSION_KEYS) | {"stock_state", "dispatch_operation_id"}:
        raise _OriginUnproven
    old = {key: value[key] for key in _ORIGIN_DIMENSION_KEYS}
    if value["stock_state"] == "on_hand" and value["dispatch_operation_id"] is None:
        return _origin_dimensions(old)
    if (value["stock_state"] != "in_transit" or value["location_id"] is not None
            or not _origin_uuid_text(value["dispatch_operation_id"])):
        raise _OriginUnproven
    # Reuse the complete legacy cargo validator; transit has no physical cell.
    _origin_dimensions({**old, "location_id": 1})
    return dict(value)


def _origin_validate_operation(operation, organization_id):
    from modules.wms.physical_schemas import TransferReceipt
    from modules.wms.physical_stock import canonical, digest

    if (
        not isinstance(operation, dict)
        or type(operation.get("id")) is not int
        or operation["id"] <= 0
        or type(operation.get("organization_id")) is not int
        or operation["organization_id"] != organization_id
        or type(operation.get("warehouse_id")) is not int
        or operation["warehouse_id"] <= 0
        or not _origin_uuid_text(operation.get("operation_id"))
        or not isinstance(operation.get("kind"), str)
        or not isinstance(operation.get("request_hash"), str)
    ):
        raise _OriginUnproven
    result = operation.get("result")
    from modules.wms.placement_models import placement_operation_indicator
    if placement_operation_indicator(operation["kind"], result):
        from modules.wms.placement_service import validate_placement_envelope
        try:
            proof = validate_placement_envelope(operation)
            if proof["kind"] != "capacity_placement_complete":
                raise ValueError("physical_origin_placement_not_complete")
            causal = result["physical_causal_v1"]
            expected_keys = {"schema_version", "canonical_request", "births", "count_basis", "prior_frontiers"}
            if (type(causal) is not dict or set(causal) != expected_keys
                    or type(causal["schema_version"]) is not int or causal["schema_version"] != 2
                    or causal["births"] != [] or causal["count_basis"] != []
                    or causal["canonical_request"] != proof["request"]
                    or type(causal["prior_frontiers"]) is not dict
                    or set(causal["prior_frontiers"]) != {row["id"] for row in proof["source_before"]["members"]}):
                raise ValueError("physical_origin_placement_causal_shape")
            for prior in causal["prior_frontiers"].values():
                _origin_frontier_tokens(prior)
        except (TypeError, ValueError, KeyError) as exc:
            raise _OriginUnproven from exc
        return causal, proof["request"], {key: value for key, value in result.items() if key != "physical_causal_v1"}
    from modules.wms.transfer_loss_protocol import loss_operation_indicator

    if loss_operation_indicator(operation["kind"], result):
        from modules.wms.transfer_loss_service import origin_validate_loss

        return origin_validate_loss(operation, organization_id)
    causal = result.get("physical_causal_v1") if isinstance(result, dict) else None
    transfer_kind = operation["kind"] in {"transfer_dispatch", "transfer_receive"}
    expected_causal_keys = {"schema_version", "canonical_request", "births", "count_basis", "prior_frontiers"}
    if operation["kind"] == "task_complete":
        expected_causal_keys.add("delegated_move")
    if (
        not isinstance(causal, dict)
        or type(causal.get("schema_version")) is not int
        or causal.get("schema_version") != (3 if transfer_kind else 2)
        or set(causal) != expected_causal_keys
        or not isinstance(causal.get("births"), list)
        or not isinstance(causal.get("count_basis"), list)
        or not isinstance(causal.get("prior_frontiers"), dict)
        or (operation["kind"] != "count_approve" and causal["count_basis"] != [])
    ):
        raise _OriginUnproven
    request = causal.get("canonical_request")
    try:
        parsed_request = (_ORIGIN_TRANSFER_COMMAND_ADAPTER if transfer_kind else _ORIGIN_WAREHOUSE_COMMAND_ADAPTER).validate_python(request)
        canonical_request = parsed_request.model_dump(mode="json")
        from modules.wms.physical_delivery import normalize_legacy_optional_fields

        canonical_request = normalize_legacy_optional_fields(canonical_request, operation["kind"])
        if operation["kind"] == "pack_v2":
            if parsed_request.kind != "pack_v2":
                raise _OriginUnproven
            expected_packages = parsed_request.shipping_package_proof()
            if expected_packages is None:
                canonical_request.pop("shipping_packages")
                if "shipping_packages_v1" in result:
                    raise _OriginUnproven
            elif (
                "shipping_packages_v1" not in result
                or canonical(result["shipping_packages_v1"]) != canonical(expected_packages)
            ):
                raise _OriginUnproven
        if operation["kind"] == "ship":
            for key, default in (("container_scans", {}), ("shipment_mode", "full"), ("partial_reason", "")):
                if canonical_request[key] == default:
                    canonical_request.pop(key)
        request_digest = digest(request)
    except (TypeError, ValueError, ValidationError) as exc:
        raise _OriginUnproven from exc
    if (
        not isinstance(request, dict)
        or canonical_request != request
        or request.get("kind") != operation.get("kind")
        or request.get("operation_id") != operation.get("operation_id")
        or type(request.get("warehouse_id")) is not int
        or request.get("warehouse_id") != operation.get("warehouse_id")
        or request_digest != operation.get("request_hash")
        or result.get("operation_id") != operation.get("operation_id")
        or result.get("kind") != operation.get("kind")
        or type(result.get("warehouse_id")) is not int
        or result.get("warehouse_id") != operation.get("warehouse_id")
    ):
        raise _OriginUnproven
    for unit_key, prior in causal["prior_frontiers"].items():
        if not _origin_uuid_text(unit_key):
            raise _OriginUnproven
        try:
            _origin_frontier_tokens(prior)
        except (TypeError, ValueError) as exc:
            raise _OriginUnproven from exc
    _origin_birth(causal, "")
    if transfer_kind:
        try:
            sent = TransferReceipt.model_validate(result.get("transfer")).model_dump(mode="json")
        except (TypeError, ValueError) as exc:
            raise _OriginUnproven from exc
        expected_result_keys = {"operation_id", "kind", "warehouse_id", "transfer", "physical_causal_v1"}
        if operation["kind"] == "transfer_receive":
            expected_result_keys.add("unit")
        parent_id = request["unit_id"]
        child_id = sent["transit_unit_id"] if operation["kind"] == "transfer_dispatch" else sent["received_unit_id"]
        if (set(result) != expected_result_keys or sent != result["transfer"]
                or causal["count_basis"] != [] or set(causal["prior_frontiers"]) != {parent_id, child_id}
                or causal["births"] != [{"unit_id": child_id, "basis": "clone", "source_unit_id": parent_id}]
                or _origin_frontier_tokens(causal["prior_frontiers"][child_id])):
            raise _OriginUnproven
        if operation["kind"] == "transfer_dispatch":
            if (sent["dispatch_operation_id"] != operation["operation_id"]
                    or sent["source_warehouse_id"] != operation["warehouse_id"]
                    or sent["destination_warehouse_id"] != request["destination_warehouse_id"]
                    or sent["source_unit_id"] != parent_id or sent["received_unit_id"] is not None
                    or sent["container"] != request["container"]
                    or _origin_qty(sent["remaining"]) != _origin_qty(request["qty"])):
                raise _OriginUnproven
        elif (sent["dispatch_operation_id"] != request["dispatch_operation_id"]
                or sent["destination_warehouse_id"] != operation["warehouse_id"]
                or sent["transit_unit_id"] != parent_id or child_id is None):
            raise _OriginUnproven
        if _origin_qty(sent["qty"]) != _origin_qty(request["qty"]):
            raise _OriginUnproven
    return causal, request, {key: value for key, value in result.items() if key != "physical_causal_v1"}


def _origin_group_frontier(group, unit_id):
    causal = group["causal"]
    frontiers = causal.get("prior_frontiers")
    if not isinstance(frontiers, dict) or unit_id not in frontiers:
        raise _OriginUnproven
    prior = frontiers[unit_id]
    try:
        return prior, _origin_frontier_tokens(prior)
    except (TypeError, ValueError) as exc:
        raise _OriginUnproven from exc


@dataclass(frozen=True)
class _HistoricalOriginContext:
    """Internal native proof boundary; every recursive read precedes a real OP PK."""
    organization_id: int
    warehouse_id: int
    before_pk: int
    frozen_units: tuple[tuple[str, str], ...] = ()

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in
               (self.organization_id, self.warehouse_id, self.before_pk)):
            raise _OriginUnproven
        if type(self.frozen_units) is not tuple or len({key for key, _ in self.frozen_units}) != len(self.frozen_units):
            raise _OriginUnproven

    def before(self, operation_pk):
        if type(operation_pk) is not int or not 0 < operation_pk < self.before_pk:
            raise _OriginUnproven
        return replace(self, before_pk=operation_pk, frozen_units=())

    def operation(self, row):
        if (row is None or row.organization_id != self.organization_id
                or row.warehouse_id != self.warehouse_id or not 0 < row.id < self.before_pk):
            raise _OriginUnproven
        return row

    def snapshot(self, operation):
        if (not isinstance(operation, dict) or operation.get("organization_id") != self.organization_id
                or operation.get("warehouse_id") != self.warehouse_id
                or type(operation.get("id")) is not int or not 0 < operation["id"] < self.before_pk):
            raise _OriginUnproven
        return operation

    def before_warehouse(self, warehouse_id, operation_pk):
        if (type(warehouse_id) is not int or warehouse_id <= 0
                or type(operation_pk) is not int or not 0 < operation_pk < self.before_pk):
            raise _OriginUnproven
        return _HistoricalOriginContext(self.organization_id, warehouse_id, operation_pk, ())

    async def identity(self, session, unit_id, *, trail=frozenset()):
        # This resolves immutable identity only. Quantity/dimensions/whole packets
        # are still proved by the single existing _prove_unit_boundary below.
        from modules.wms.physical_stock import canonical
        import json
        if unit_id in trail or len(trail) >= 64:
            raise _OriginUnproven
        _, _, groups = await _origin_history(session, self.organization_id, unit_id, historical=self)
        identities = []
        for group in groups.values():
            operation = group['operation']
            transfer_pair = None
            if operation['kind'] in {'transfer_dispatch', 'transfer_receive'}:
                transfer_pair = await _origin_transfer_pair(session, self.organization_id, operation,
                    historical=self, unit_id=unit_id)
            causal, _request, result = _origin_validate_operation(operation, self.organization_id)
            birth = _origin_birth(causal, unit_id)
            if birth is None and operation['kind'] == 'pick' and result.get('unit', {}).get('id') == unit_id:
                receipt = await _origin_pick_receipt(session, self.organization_id, operation['warehouse_id'],
                    result.get('pick_id'), historical=self)
                value = await self.before(operation['id']).identity(session, receipt['source_unit_id'], trail=trail | {unit_id})
            elif birth is None:
                continue
            elif birth['basis'] == 'clone':
                parent_context = self.before(operation['id'])
                if operation['kind'] == 'transfer_receive':
                    if (transfer_pair is None or unit_id != transfer_pair.get('received_unit_id')
                            or birth['source_unit_id'] != transfer_pair.get('transit_unit_id')):
                        raise _OriginUnproven
                    parent_context = self.before_warehouse(
                        transfer_pair['source_warehouse_id'], operation['id'])
                value = await parent_context.identity(
                    session, birth['source_unit_id'], trail=trail | {unit_id})
            elif birth['basis'] in {'assembly_output', 'assembly_return'}:
                from modules.wms.assembly_quality import physical_assembly_proof
                from modules.wms.physical_schemas import AssemblyOutputProof, AssemblyReturnProof
                model = AssemblyOutputProof if birth['basis'] == 'assembly_output' else AssemblyReturnProof
                typed = physical_assembly_proof(model, result.get(birth['basis'] + '_v1'))
                value = str(typed.identity_id)
            elif birth['basis'] in {'received', 'approved_count'}:
                value = None  # A typed native non-assembly birth, never missing-row fallback.
            else:
                raise _OriginUnproven
            unit_result = result.get('unit')
            if isinstance(unit_result, dict) and unit_result.get('id') == unit_id and unit_result.get('assembly_output_id') != value:
                raise _OriginUnproven
            identities.append(value)
        if not identities or any(value != identities[0] for value in identities):
            raise _OriginUnproven
        frozen = dict(self.frozen_units).get(unit_id)
        if frozen is not None:
            frame = json.loads(frozen)
            if canonical(frame) != frozen or frame.get('id') != unit_id or frame.get('assembly_output_id') != identities[0]:
                raise _OriginUnproven
        return identities[0]


async def _origin_transfer_pair(session, organization_id, operation, *, historical=None, unit_id=None):
    """Check an actual typed transfer packet before any warehouse-scope fork."""
    from modules.wms.physical_models import PhysicalLedger, PhysicalOperation, WarehouseControl

    if not isinstance(operation, dict) or operation.get("kind") not in {"transfer_dispatch", "transfer_receive"}:
        raise _OriginUnproven
    causal, request, result = _origin_validate_operation(operation, organization_id)
    sent = result.get("transfer")
    if not isinstance(sent, dict):
        raise _OriginUnproven
    if historical is not None and (operation["id"] >= historical.before_pk
            or operation["organization_id"] != historical.organization_id):
        raise _OriginUnproven

    if operation["kind"] == "transfer_dispatch":
        dispatch, dispatch_request, dispatch_result = operation, request, result
        receive = None
    else:
        receive = operation
        dispatch_rows = (await session.scalars(select(PhysicalOperation).where(
            PhysicalOperation.organization_id == organization_id,
            PhysicalOperation.operation_id == request.get("dispatch_operation_id"),
        ).order_by(PhysicalOperation.id).execution_options(populate_existing=True))).all()
        if len(dispatch_rows) != 1:
            raise _OriginUnproven
        dispatch_row = dispatch_rows[0]
        dispatch = _origin_operation_snapshot(dispatch_row)
        if (dispatch_row.kind != "transfer_dispatch" or dispatch_row.id >= operation["id"]
                or (historical is not None and dispatch_row.id >= historical.before_pk)):
            raise _OriginUnproven
        _, dispatch_request, dispatch_result = _origin_validate_operation(dispatch, organization_id)

    dispatch_sent = dispatch_result.get("transfer")
    if (dispatch["kind"] != "transfer_dispatch" or not isinstance(dispatch_sent, dict)
            or dispatch_sent.get("dispatch_operation_id") != dispatch["operation_id"]
            or dispatch_sent.get("source_warehouse_id") != dispatch["warehouse_id"]
            or dispatch_sent.get("destination_warehouse_id") != dispatch_request.get("destination_warehouse_id")
            or dispatch_sent.get("source_unit_id") != dispatch_request.get("unit_id")
            or dispatch_sent.get("transit_unit_id") == dispatch_sent.get("source_unit_id")
            or dispatch_sent.get("received_unit_id") is not None
            or dispatch_sent.get("container") != dispatch_request.get("container")
            or _origin_qty(dispatch_sent.get("qty")) != _origin_qty(dispatch_request.get("qty"))
            or _origin_qty(dispatch_sent.get("remaining")) != _origin_qty(dispatch_request.get("qty"))):
        raise _OriginUnproven
    source_warehouse_id = dispatch_sent["source_warehouse_id"]
    destination_warehouse_id = dispatch_sent["destination_warehouse_id"]
    if source_warehouse_id == destination_warehouse_id:
        raise _OriginUnproven
    for warehouse_id in (source_warehouse_id, destination_warehouse_id):
        warehouse_org = await session.scalar(select(WarehouseControl.organization_id).where(
            WarehouseControl.warehouse_id == warehouse_id,
        ))
        if warehouse_org != organization_id:
            raise _OriginUnproven

    dispatch_pair = (await session.execute(select(
        PhysicalLedger.unit_id, PhysicalLedger.qty_delta, PhysicalLedger.dimensions,
    ).where(PhysicalLedger.operation_pk == dispatch["id"]).order_by(PhysicalLedger.id))).all()
    source_id, transit_id = dispatch_sent["source_unit_id"], dispatch_sent["transit_unit_id"]
    dispatch_qty = _origin_qty(dispatch_sent["qty"])
    if (dispatch_qty <= 0 or len(dispatch_pair) != 2 or {row[0] for row in dispatch_pair} != {source_id, transit_id}
            or len({row[0] for row in dispatch_pair}) != 2):
        raise _OriginUnproven
    dispatch_by_unit = {row[0]: row for row in dispatch_pair}
    source_dimensions = _origin_transfer_dimensions(dispatch_by_unit[source_id][2])
    transit_dimensions = _origin_transfer_dimensions(dispatch_by_unit[transit_id][2])
    expected_transit = {**source_dimensions, "location_id": None, "container": dispatch_sent["container"],
        "stock_state": "in_transit", "dispatch_operation_id": dispatch["operation_id"]}
    if (_origin_qty(str(dispatch_by_unit[source_id][1])) != -dispatch_qty
            or _origin_qty(str(dispatch_by_unit[transit_id][1])) != dispatch_qty
            or "stock_state" in source_dimensions
            or source_dimensions["warehouse_id"] != source_warehouse_id
            or transit_dimensions != expected_transit):
        raise _OriginUnproven

    binding = {"dispatch": dispatch, "dispatch_request": dispatch_request, "dispatch_result": dispatch_result,
        "source_warehouse_id": source_warehouse_id, "destination_warehouse_id": destination_warehouse_id,
        "source_unit_id": source_id, "transit_unit_id": transit_id, "container": dispatch_sent["container"],
        "qty": dispatch_qty, "source_dimensions": source_dimensions, "transit_dimensions": transit_dimensions,
        "receive": None}
    if receive is not None:
        received_sent = sent
        received_id = received_sent.get("received_unit_id")
        received_qty = _origin_qty(request.get("qty"))
        receive_pair = (await session.execute(select(
            PhysicalLedger.unit_id, PhysicalLedger.qty_delta, PhysicalLedger.dimensions,
        ).where(PhysicalLedger.operation_pk == operation["id"]).order_by(PhysicalLedger.id))).all()
        if (operation["kind"] != "transfer_receive" or operation["warehouse_id"] != destination_warehouse_id
                or request.get("warehouse_id") != destination_warehouse_id
                or request.get("unit_id") != transit_id
                or request.get("dispatch_operation_id") != dispatch["operation_id"]
                or received_sent.get("dispatch_operation_id") != dispatch["operation_id"]
                or received_sent.get("source_warehouse_id") != source_warehouse_id
                or received_sent.get("destination_warehouse_id") != destination_warehouse_id
                or received_sent.get("source_unit_id") != source_id
                or received_sent.get("transit_unit_id") != transit_id
                or received_sent.get("container") != dispatch_sent["container"]
                or not _origin_uuid_text(received_id) or received_id in {source_id, transit_id}
                or received_qty <= 0 or _origin_qty(received_sent.get("qty")) != received_qty
                or len(receive_pair) != 2 or {row[0] for row in receive_pair} != {transit_id, received_id}
                or len({row[0] for row in receive_pair}) != 2):
            raise _OriginUnproven
        receive_by_unit = {row[0]: row for row in receive_pair}
        receive_transit_dimensions = _origin_transfer_dimensions(receive_by_unit[transit_id][2])
        received_dimensions = _origin_transfer_dimensions(receive_by_unit[received_id][2])
        expected_received = {key: value for key, value in transit_dimensions.items()
            if key not in {"stock_state", "dispatch_operation_id"}}
        expected_received.update(warehouse_id=destination_warehouse_id, location_id=request.get("destination_id"))
        if (_origin_qty(str(receive_by_unit[transit_id][1])) != -received_qty
                or _origin_qty(str(receive_by_unit[received_id][1])) != received_qty
                or receive_transit_dimensions != transit_dimensions
                or received_dimensions != expected_received
                or type(received_sent.get("remaining")) not in (int, float, str, Decimal)
                or _origin_qty(received_sent.get("remaining")) < 0):
            raise _OriginUnproven
        receive_unit = result.get("unit")
        if (not isinstance(receive_unit, dict) or receive_unit.get("id") != received_id
                or _origin_result_dimensions(receive_unit) != expected_received
                or _origin_qty(receive_unit.get("qty")) != received_qty
                or _origin_qty(receive_unit.get("reserved")) != 0
                or _origin_qty(receive_unit.get("free")) != received_qty):
            raise _OriginUnproven
        binding.update(receive=operation, received_unit_id=received_id,
            receive_qty=received_qty, received_dimensions=expected_received)

    if historical is not None:
        if unit_id == transit_id:
            if historical.warehouse_id != source_warehouse_id:
                raise _OriginUnproven
        elif receive is not None and unit_id == binding["received_unit_id"]:
            if historical.warehouse_id != destination_warehouse_id:
                raise _OriginUnproven
        elif unit_id == source_id and operation["kind"] == "transfer_dispatch":
            if historical.warehouse_id != source_warehouse_id:
                raise _OriginUnproven
        else:
            raise _OriginUnproven
        if ((receive is not None and not dispatch["id"] < operation["id"] < historical.before_pk)
                or operation["id"] >= historical.before_pk):
            raise _OriginUnproven
    return binding


async def _origin_history(session, organization_id: int, unit_id: str, *, historical=None):
    from modules.wms.physical_stock import canonical, physical_unit_frontier

    frontier, records = await physical_unit_frontier(session, organization_id, unit_id, historical=historical)
    current_tokens = _origin_frontier_tokens(frontier)
    groups = {}
    for ledger in records["ledgers"]:
        operation = ledger["operation"]
        if operation is None or operation.get("organization_id") != organization_id:
            raise _OriginUnproven
        if historical is not None:
            if type(operation.get("id")) is not int or not 0 < operation["id"] < historical.before_pk:
                raise _OriginUnproven
            if operation.get("warehouse_id") == historical.warehouse_id:
                historical.snapshot(operation)
            elif operation.get("kind") == "transfer_receive":
                await _origin_transfer_pair(session, organization_id, operation,
                    historical=historical, unit_id=unit_id)
            else:
                raise _OriginUnproven
        group = groups.setdefault(operation["id"], {
            "operation": operation, "ledgers": [], "observations": [],
        })
        group["ledgers"].append(ledger)
    for operation in records["count_operations"]:
        observations = operation.get("observation_tokens")
        if observations is None:
            opaque_group = groups.get(operation["id"])
            if opaque_group is not None:
                opaque_group["opaque_count_baseline"] = True
            continue
        if observations:
            if historical is not None:
                historical.snapshot(operation)
            group = groups.setdefault(operation["id"], {
                "operation": operation, "ledgers": [], "observations": [],
            })
            group["observations"].extend(observations)

    for operation in records.get("placement_zero_operations", []):
        if historical is not None:
            historical.snapshot(operation)
        group = groups.setdefault(operation["id"], {"operation": operation, "ledgers": [], "observations": []})
        group["observations"].append(operation["placement_zero_token"])

    for group in groups.values():
        group["ledgers"].sort(key=lambda row: row["id"])
        group["observations"].sort(key=canonical)
        group["tokens"] = frozenset(
            [
                canonical(row["token"])
                for row in group["ledgers"]
            ] + [
                canonical(row)
                for row in group["observations"]
            ]
        )
        operation_result = group["operation"].get("result")
        causal = operation_result.get("physical_causal_v1") if isinstance(operation_result, dict) else None
        group["causal"] = causal if isinstance(causal, dict) else None
    return frontier, current_tokens, groups


def _origin_birth(causal, unit_id):
    births = causal.get("births")
    if not isinstance(births, list):
        raise _OriginUnproven
    matches = []
    seen = set()
    for row in births:
        if (
            not isinstance(row, dict)
            or set(row) != {"unit_id", "basis", "source_unit_id"}
            or not _origin_uuid_text(row.get("unit_id"))
            or row["unit_id"] in seen
            or type(row.get("basis")) is not str
            or row.get("basis") not in {"received", "clone", "approved_count", "assembly_output", "assembly_return"}
        ):
            raise _OriginUnproven
        seen.add(row["unit_id"])
        if row["basis"] == "clone":
            if not _origin_uuid_text(row.get("source_unit_id")) or row["source_unit_id"] == row["unit_id"]:
                raise _OriginUnproven
        elif row.get("source_unit_id") is not None:
            raise _OriginUnproven
        if row["unit_id"] == unit_id:
            matches.append(row)
    if len(matches) > 1:
        raise _OriginUnproven
    return matches[0] if matches else None


def _origin_group_rows(group, unit_id):
    rows = group["ledgers"]
    operation = group["operation"]
    if any(
        type(row.get("id")) is not int or row["id"] <= 0
        or type(row.get("operation_pk")) is not int or row["operation_pk"] != operation["id"]
        or row.get("unit_id") != unit_id
        for row in rows
    ):
        raise _OriginUnproven
    for row in rows:
        if group["causal"].get("schema_version") == 3:
            _origin_transfer_dimensions(row.get("dimensions"))
        else:
            _origin_dimensions(row.get("dimensions"))
        _origin_qty(row["qty_delta"])
    return rows


async def _prove_unit_boundary(
    session,
    organization_id: int,
    unit_id: str,
    boundary_tokens: frozenset[str],
    *,
    current_unit=None,
    trail=frozenset(),
    allow_zero=False,
    expected_state="on_hand",
    historical=None,
):
    from modules.wms.physical_models import StockUnit, require_current_in_transit
    from modules.wms.physical_stock import digest, dimensions, transfer_dimensions

    if current_unit is not None:
        try:
            if expected_state == "on_hand":
                require_current_on_hand(current_unit)
            elif expected_state == "in_transit":
                require_current_in_transit(current_unit)
            else:
                raise _OriginUnproven
        except CurrentStockStateError as exc:
            raise _OriginUnproven from exc

    identity_id = (await session.scalar(select(StockUnit.assembly_output_id).where(StockUnit.id == unit_id))
        if historical is None else await historical.identity(session, unit_id))

    marker = (unit_id, tuple(sorted(boundary_tokens)))
    if marker in trail or len(trail) >= 64:
        raise _OriginUnproven
    frontier, all_tokens, groups = await _origin_history(session, organization_id, unit_id, historical=historical)
    if not boundary_tokens.issubset(all_tokens):
        raise _OriginUnproven
    selected = {
        key: group for key, group in groups.items()
        if group["tokens"] & boundary_tokens and not group.get("opaque_count_baseline")
    }
    if any(not group["tokens"].issubset(boundary_tokens) for group in selected.values()):
        raise _OriginUnproven
    dependencies = {}
    for operation_pk, group in selected.items():
        causal = group["causal"]
        if (not isinstance(causal, dict) or type(causal.get("schema_version")) is not int
                or causal.get("schema_version") not in {2, 3}):
            dependencies[operation_pk] = ()
            continue
        prior, prior_tokens = _origin_group_frontier(group, unit_id)
        if not prior_tokens.issubset(boundary_tokens) or prior_tokens & group["tokens"]:
            raise _OriginUnproven
        predecessors = []
        for predecessor_pk, predecessor in selected.items():
            overlap = predecessor["tokens"] & prior_tokens
            if overlap:
                if not predecessor["tokens"].issubset(prior_tokens):
                    raise _OriginUnproven
                predecessors.append(predecessor_pk)
        dependencies[operation_pk] = tuple(predecessors)
    colors = {}
    for operation_pk in dependencies:
        if colors.get(operation_pk) == 2:
            continue
        colors[operation_pk] = 1
        stack = [(operation_pk, iter(dependencies[operation_pk]))]
        while stack:
            current_pk, predecessors = stack[-1]
            try:
                predecessor_pk = next(predecessors)
            except StopIteration:
                colors[current_pk] = 2
                stack.pop()
                continue
            color = colors.get(predecessor_pk, 0)
            if color == 1:
                raise _OriginUnproven
            if color == 2:
                continue
            colors[predecessor_pk] = 1
            stack.append((predecessor_pk, iter(dependencies[predecessor_pk])))
    anchors = []
    for operation_pk, group in selected.items():
        causal = group["causal"]
        if (not isinstance(causal, dict) or type(causal.get("schema_version")) is not int
                or causal.get("schema_version") not in {2, 3}):
            continue
        try:
            _, prior = _origin_group_frontier(group, unit_id)
            if not prior.issubset(boundary_tokens):
                continue
            operation = group["operation"]
            if operation["kind"] == "receive":
                birth = _origin_birth(causal, unit_id)
                if birth and birth.get("basis") == "received" and not prior:
                    anchors.append((operation_pk, group, prior, "received", operation["operation_id"]))
            elif operation["kind"] in {"assembly_output", "assembly_return"}:
                birth = _origin_birth(causal, unit_id)
                if birth and birth.get("basis") == operation["kind"] and not prior and identity_id is not None:
                    anchors.append((operation_pk, group, prior, operation["kind"], operation["operation_id"]))
            elif operation["kind"] == "count_approve" and group["observations"] and identity_id is None:
                anchors.append((operation_pk, group, prior, "approved_count", operation["operation_id"]))
            elif operation["kind"] == "pick" and not prior:
                unit_result = operation["result"].get("unit")
                if isinstance(unit_result, dict) and unit_result.get("id") == unit_id:
                    anchors.append((operation_pk, group, prior, "clone", None))
            else:
                birth = _origin_birth(causal, unit_id)
                if birth and birth.get("basis") == "clone":
                    anchors.append((operation_pk, group, prior, "clone", birth.get("source_unit_id")))
        except (TypeError, ValueError):
            continue
    valid = []
    for operation_pk, anchor_group, prior, anchor_kind, anchor_source in anchors:
        try:
            with session.no_autoflush:
                state = await _origin_apply_group(
                    session, organization_id, unit_id, anchor_group,
                    None, None, trail | {marker}, is_anchor=True,
                    anchor_kind=anchor_kind, anchor_source=anchor_source,
                    historical=historical,
                )
            proof_basis = state["basis"]
            proof_root = state["root_operation_id"]
            qc_birth = state.get("qc_birth_operation_id")
            covered = set(prior) | set(anchor_group["tokens"])
            applied = {operation_pk}
            while True:
                if covered == set(boundary_tokens):
                    break
                next_groups = []
                for candidate_pk, candidate in selected.items():
                    if candidate_pk in applied:
                        continue
                    causal = candidate["causal"]
                    if (not isinstance(causal, dict) or type(causal.get("schema_version")) is not int
                            or causal.get("schema_version") not in {2, 3}):
                        continue
                    saved_prior, saved_set = _origin_group_frontier(candidate, unit_id)
                    if saved_set == frozenset(covered):
                        next_groups.append((candidate_pk, candidate, saved_prior))
                if len(next_groups) != 1:
                    raise _OriginUnproven
                next_pk, next_group, _ = next_groups[0]
                with session.no_autoflush:
                    state = await _origin_apply_group(
                        session, organization_id, unit_id, next_group,
                        state["qty"], state["dimensions"], trail | {marker},
                        is_anchor=False, anchor_kind=None, anchor_source=None,
                        historical=historical,
                    )
                state["basis"] = proof_basis
                state["root_operation_id"] = proof_root
                if qc_birth is not None:
                    state["qc_birth_operation_id"] = qc_birth
                covered.update(next_group["tokens"])
                applied.add(next_pk)
            if covered != set(boundary_tokens):
                raise _OriginUnproven
            valid.append({**state, "basis": state.get("basis") or anchor_kind,
                "root_operation_id": state.get("root_operation_id") or (
                    anchor_source if anchor_kind == "clone" else anchor_group["operation"]["operation_id"]
                ), "tail_operation_id": state.get("tail_operation_id") or anchor_group["operation"]["operation_id"],
                "anchor_prior": prior})
        except _OriginUnproven:
            continue
    if not valid:
        raise _OriginUnproven
    # Prefer the latest independently attested full-count boundary. Exact saved
    # token sets, rather than database IDs or timestamps, determine this choice.
    strongest = max(len(item["anchor_prior"]) for item in valid)
    candidates = [item for item in valid if len(item["anchor_prior"]) == strongest]
    if len({(item["basis"], item["root_operation_id"], item["tail_operation_id"]) for item in candidates}) != 1:
        raise _OriginUnproven
    result = candidates[0]
    if current_unit is not None:
        current_dimensions = transfer_dimensions(current_unit) if expected_state == "in_transit" else dimensions(current_unit)
        if result["qty"] != Decimal(current_unit.qty) or result["dimensions"] != current_dimensions:
            raise _OriginUnproven
    if result["qty"] < 0 or (result["qty"] == 0 and not allow_zero):
        raise _OriginUnproven
    proof = {
        "organization_id": organization_id,
        "unit_id": unit_id,
        "boundary": sorted(boundary_tokens),
        "basis": result["basis"],
        "root_operation_id": result["root_operation_id"],
        "tail_operation_id": result["tail_operation_id"],
        "dimensions": result["dimensions"],
        "qty": format(result["qty"], "f"),
    }
    if "qc_birth_operation_id" in result:
        proof["qc_birth_operation_id"] = result["qc_birth_operation_id"]
    return {**result, "proof_digest": digest(proof)}


def _origin_same_identity(left, right):
    return all(left.get(key) == right.get(key) for key in (
        "sku_code", "batch_id", "packaging_id", "characteristic", "purpose",
    ))


def _origin_validate_clone_transition(kind, request, causal, source_unit_id, source_dimensions, child_dimensions):
    if not _origin_same_identity(source_dimensions, child_dimensions):
        raise _OriginUnproven
    allowed = {
        "quality": {"quality", "inspection_ref"},
        "maintenance": {"quality", "last_checked", "next_service", "service_result"},
        "open_package": {"package_state"},
        "move": {"warehouse_id", "location_id"},
        "task_complete": {"warehouse_id", "location_id"},
        "assembly_qc": {"quality", "inspection_ref"},
        "assembly_kit_move": {"location_id", "container"},
        "assembly_unused_return": {"location_id", "container"},
    }
    if kind not in allowed:
        raise _OriginUnproven
    changed = {key for key in _ORIGIN_DIMENSION_KEYS if source_dimensions[key] != child_dimensions[key]}
    if not changed.issubset(allowed[kind]):
        raise _OriginUnproven
    move = request
    if kind == "task_complete":
        move = causal.get("delegated_move")
        if (
            not isinstance(move, dict)
            or move.get("kind") != "move"
            or move.get("operation_id") != request.get("operation_id")
            or move.get("unit_id") != source_unit_id
        ):
            raise _OriginUnproven
    elif kind not in {"assembly_kit_move", "assembly_unused_return"} and request.get("unit_id") != source_unit_id:
        raise _OriginUnproven
    if kind == "assembly_qc":
        if child_dimensions["quality"] != "released" or child_dimensions["inspection_ref"] != request.get("evidence"):
            raise _OriginUnproven
    elif kind in {"assembly_kit_move", "assembly_unused_return"}:
        if child_dimensions["quality"] != "released":
            raise _OriginUnproven
    elif kind == "quality":
        if (
            child_dimensions["quality"] != request.get("quality")
            or child_dimensions["inspection_ref"] != request.get("evidence")
        ):
            raise _OriginUnproven
    elif kind == "maintenance":
        expected_quality = source_dimensions["quality"] if request.get("result") == "passed" else "blocked"
        expected_next = request.get("next_service") if request.get("result") == "passed" else None
        expected_service = {
            "evidence": request.get("evidence"),
            "measurements": request.get("measurements"),
            "result": request.get("result"),
        }
        if (
            child_dimensions["quality"] != expected_quality
            or child_dimensions["last_checked"] != request.get("checked_on")
            or child_dimensions["next_service"] != expected_next
            or child_dimensions["service_result"] != expected_service
        ):
            raise _OriginUnproven
    elif kind == "open_package":
        if child_dimensions["package_state"] != "open" or source_dimensions["package_state"] != "sealed":
            raise _OriginUnproven
    else:
        if (
            child_dimensions["location_id"] != move.get("destination_id")
            or child_dimensions["warehouse_id"] != (move.get("destination_warehouse_id") or move.get("warehouse_id"))
        ):
            raise _OriginUnproven


async def _origin_transfer_transition(session, organization_id, unit_id, group, previous_qty,
                                      previous_dimensions, trail, *, is_anchor, causal, request, result, rows, historical=None):
    from modules.wms.physical_models import PhysicalLedger, PhysicalOperation, WarehouseControl

    operation = group["operation"]
    pair_binding = await _origin_transfer_pair(session, organization_id, operation,
        historical=historical, unit_id=unit_id)
    sent = result["transfer"]
    parent_id = request["unit_id"]
    child_id = sent["transit_unit_id"] if operation["kind"] == "transfer_dispatch" else sent["received_unit_id"]
    pair = (await session.execute(select(PhysicalLedger.unit_id, PhysicalLedger.qty_delta, PhysicalLedger.dimensions)
                                  .where(PhysicalLedger.operation_pk == operation["id"]))).all()
    if len(pair) != 2 or len({row[0] for row in pair}) != 2 or {row[0] for row in pair} != {parent_id, child_id}:
        raise _OriginUnproven
    by_unit = {row[0]: row for row in pair}
    qty = _origin_qty(request["qty"])
    if qty <= 0 or _origin_qty(str(by_unit[parent_id][1])) != -qty or _origin_qty(str(by_unit[child_id][1])) != qty:
        raise _OriginUnproven
    for warehouse_id in (sent["source_warehouse_id"], sent["destination_warehouse_id"]):
        org = await session.scalar(select(WarehouseControl.organization_id).where(WarehouseControl.warehouse_id == warehouse_id))
        if org != organization_id:
            raise _OriginUnproven
    parent_context = None if historical is None else historical.before_warehouse(
        pair_binding["source_warehouse_id"], operation["id"])
    parent = await _prove_unit_boundary(session, organization_id, parent_id,
        _origin_frontier_tokens(causal["prior_frontiers"][parent_id]), trail=trail,
        historical=parent_context)
    parent_dimensions = _origin_transfer_dimensions(by_unit[parent_id][2])
    child_dimensions = _origin_transfer_dimensions(by_unit[child_id][2])
    if parent["qty"] < qty or parent["dimensions"] != parent_dimensions:
        raise _OriginUnproven
    if operation["kind"] == "transfer_dispatch":
        if ("stock_state" in parent_dimensions
                or parent_dimensions["warehouse_id"] != sent["source_warehouse_id"]
                or request["location_scan"] != f"LOC-{parent_dimensions['location_id']}"
                or request["batch_scan"] != f"LOT-{parent_dimensions['batch_id']}-{parent_id.upper()}"
                or request["container_scan"] != f"BOX-{sent['container']}"):
            raise _OriginUnproven
        expected_child = {**parent_dimensions, "location_id": None, "container": sent["container"],
                          "stock_state": "in_transit", "dispatch_operation_id": operation["operation_id"]}
    else:
        dispatch = await session.scalar(select(PhysicalOperation).where(
            PhysicalOperation.organization_id == organization_id,
            PhysicalOperation.operation_id == request["dispatch_operation_id"],
        ).execution_options(populate_existing=True))
        if dispatch is None or dispatch.kind != "transfer_dispatch":
            raise _OriginUnproven
        _, original_request, original_result = _origin_validate_operation(_origin_operation_snapshot(dispatch), organization_id)
        original = original_result["transfer"]
        if (original_request["destination_warehouse_id"] != operation["warehouse_id"]
                or original["transit_unit_id"] != parent_id
                or any(original[key] != sent[key] for key in (
                    "dispatch_operation_id", "source_warehouse_id", "destination_warehouse_id", "source_unit_id", "container"))
                or parent_dimensions.get("stock_state") != "in_transit"
                or parent_dimensions.get("dispatch_operation_id") != dispatch.operation_id
                or parent_dimensions["warehouse_id"] != dispatch.warehouse_id
                or _origin_qty(sent["remaining"]) != parent["qty"] - qty
                or request["location_scan"] != f"LOC-{request['destination_id']}"
                or request["batch_scan"] != f"LOT-{parent_dimensions['batch_id']}-{parent_id.upper()}"
                or request["container_scan"] != f"BOX-{sent['container']}"):
            raise _OriginUnproven
        expected_child = {key: value for key, value in parent_dimensions.items()
                          if key not in {"stock_state", "dispatch_operation_id"}}
        expected_child.update(warehouse_id=operation["warehouse_id"], location_id=request["destination_id"])
        unit_result = result["unit"]
        if (not isinstance(unit_result, dict) or unit_result.get("id") != child_id
                or _origin_result_dimensions(unit_result) != expected_child
                or _origin_qty(unit_result.get("qty")) != qty
                or _origin_qty(unit_result.get("reserved")) != 0
                or _origin_qty(unit_result.get("free")) != qty
                or unit_result.get("batch_scan") != f"LOT-{expected_child['batch_id']}-{child_id.upper()}"):
            raise _OriginUnproven
    if child_dimensions != expected_child:
        raise _OriginUnproven
    if (len(rows) != 1 or unit_id not in by_unit
            or rows[0]["dimensions"] != by_unit[unit_id][2]
            or _origin_qty(rows[0]["qty_delta"]) != _origin_qty(str(by_unit[unit_id][1]))):
        raise _OriginUnproven
    if unit_id == child_id:
        if not is_anchor or previous_qty is not None:
            raise _OriginUnproven
        new_qty, new_dimensions = qty, child_dimensions
    else:
        if is_anchor or previous_qty != parent["qty"] or previous_dimensions != parent_dimensions:
            raise _OriginUnproven
        new_qty, new_dimensions = parent["qty"] - qty, parent_dimensions
    return {"qty": new_qty, "dimensions": new_dimensions, "basis": "clone" if is_anchor else "clone_suffix",
            "root_operation_id": parent["root_operation_id"], "tail_operation_id": operation["operation_id"],
            **({"qc_birth_operation_id": parent["qc_birth_operation_id"]} if "qc_birth_operation_id" in parent else {})}


async def _origin_apply_group(
    session, organization_id, unit_id, group, previous_qty, previous_dimensions, trail,
    *, is_anchor, anchor_kind, anchor_source, historical=None,
):
    from modules.wms.physical_models import (
        PhysicalLedger,
        WarehouseControl,
    )
    from modules.wms.physical_stock import _origin_count_observations, canonical

    operation = group["operation"]
    prior_historical = None if historical is None else historical.before(operation["id"])
    causal, request, result = _origin_validate_operation(operation, organization_id)
    with session.no_autoflush:
        warehouse_org = await session.scalar(select(WarehouseControl.organization_id).where(
            WarehouseControl.warehouse_id == operation["warehouse_id"],
        ))
    if warehouse_org != organization_id:
        raise _OriginUnproven
    if operation["kind"] == "capacity_placement_complete":
        from modules.wms.placement_service import validate_placement_receipt, _native_dimensions
        try:
            connection = await session.connection()
            proof = await connection.run_sync(lambda conn: validate_placement_receipt(conn, operation))
            source = next(row for row in proof["source_before"]["members"] if row["id"] == unit_id)
            moved = next(row for row in proof["destination_after"]["members"] if row["id"] == unit_id)
            quantity = Decimal(source["qty"])
            if is_anchor or previous_qty != quantity or previous_dimensions != _native_dimensions(source):
                raise ValueError("physical_origin_placement_suffix_boundary")
            if quantity == 0:
                tokens = group["observations"]
                if group["ledgers"] or len(tokens) != 1 or tokens[0].get("kind") != "placement_zero_v1":
                    raise ValueError("physical_origin_placement_zero_group")
            elif len(group["ledgers"]) != 2 or group["observations"]:
                raise ValueError("physical_origin_placement_positive_group")
            return {"qty": previous_qty, "dimensions": _native_dimensions(moved), "basis": "clone_suffix",
                    "root_operation_id": None, "tail_operation_id": operation["operation_id"]}
        except (ValueError, TypeError, KeyError, StopIteration) as exc:
            raise _OriginUnproven from exc
    rows = _origin_group_rows(group, unit_id)
    birth = _origin_birth(causal, unit_id)
    from modules.wms.transfer_loss_protocol import CONFIRM_KIND, LOSS_KINDS

    if operation["kind"] in LOSS_KINDS:
        if operation["kind"] != CONFIRM_KIND:
            raise _OriginUnproven
        from modules.wms.transfer_loss_service import origin_loss_transition

        return await origin_loss_transition(session, organization_id, unit_id, group,
            previous_qty, previous_dimensions, trail, is_anchor=is_anchor,
            causal=causal, request=request, result=result, rows=rows, historical=historical)
    if operation["kind"] in {"transfer_dispatch", "transfer_receive"}:
        return await _origin_transfer_transition(session, organization_id, unit_id, group,
            previous_qty, previous_dimensions, trail, is_anchor=is_anchor,
            causal=causal, request=request, result=result, rows=rows, historical=historical)
    if not (
        birth is not None and birth.get("basis") == "clone"
        and operation["kind"] in {"move", "task_complete"}
    ):
        if any(row["dimensions"]["warehouse_id"] != operation["warehouse_id"] for row in rows):
            raise _OriginUnproven
    current_dimensions = None
    current_qty = None
    root_operation_id = None
    basis = None
    qc_birth_operation_id = None

    if operation["kind"] in {"assembly_output", "assembly_return", "assembly_kit_move", "assembly_consume", "assembly_unused_return", "ship"}:
        return await _origin_assembly_transition(session, organization_id, unit_id, group,
            previous_qty, previous_dimensions, trail, is_anchor=is_anchor,
            causal=causal, request=request, result=result, rows=rows, birth=birth, historical=historical)
    if operation["kind"] == "pick" and is_anchor:
        receipt = await _origin_pick_receipt(session, organization_id, operation["warehouse_id"], result.get("pick_id"), historical=historical)
        if receipt["child_unit_id"] != unit_id or len(rows) != 1 or _origin_qty(rows[0]["qty_delta"]) != receipt["qty"]:
            raise _OriginUnproven
        parent = await _prove_unit_boundary(session, organization_id, receipt["source_unit_id"],
            _origin_frontier_tokens(causal["prior_frontiers"][receipt["source_unit_id"]]), trail=trail,
            historical=prior_historical)
        if parent["dimensions"] != receipt["source_dimensions"] or parent["qty"] < receipt["qty"]:
            raise _OriginUnproven
        return {"qty": receipt["qty"], "dimensions": receipt["child_dimensions"], "basis": "clone",
            "root_operation_id": parent["root_operation_id"], "tail_operation_id": operation["operation_id"],
            **({"qc_birth_operation_id": parent["qc_birth_operation_id"]} if "qc_birth_operation_id" in parent else {})}

    if operation["kind"] == "count_approve":
        if _origin_count_observations(operation, unit_id) is None:
            raise _OriginUnproven
        count_basis = causal.get("count_basis")
        matches = [row for row in count_basis if isinstance(row, dict) and row.get("unit_id") == unit_id] if isinstance(count_basis, list) else []
        if len(matches) != 1 or result.get("status") != "approved" or type(result.get("opening")) is not bool:
            raise _OriginUnproven
        observation = matches[0]
        if (
            result.get("count_id") != observation.get("count_id")
            or request.get("count_id") != observation.get("count_id")
            or request.get("review_digest") != observation.get("review_digest")
            or result.get("opening") != (observation.get("count_kind") == "opening")
        ):
            raise _OriginUnproven
        current_qty = _origin_qty(observation["observed_qty"])
        current_dimensions = _origin_dimensions(observation["observed_dimensions"])
        delta = _origin_qty(observation["qty_delta"])
        if rows:
            if len(rows) != 1 or _origin_qty(rows[0]["qty_delta"]) != delta:
                raise _OriginUnproven
            if rows[0]["dimensions"] != current_dimensions:
                raise _OriginUnproven
        elif delta != 0:
            raise _OriginUnproven
        expected_adjustments = []
        for basis_row in count_basis:
            row_delta = _origin_qty(basis_row["qty_delta"])
            if row_delta:
                expected_adjustments.append({
                    **_origin_dimensions(basis_row["observed_dimensions"]),
                    "qty_delta": format(row_delta, "f"),
                })
        adjustments = result.get("adjustments")
        if (
            not isinstance(adjustments, list)
            or sorted(canonical(row) for row in adjustments) != sorted(canonical(row) for row in expected_adjustments)
        ):
            raise _OriginUnproven
        if not is_anchor:
            if previous_qty is None or previous_dimensions is None:
                raise _OriginUnproven
            if current_qty - previous_qty != delta or previous_dimensions != current_dimensions:
                raise _OriginUnproven
        expected_birth = birth is not None and birth.get("basis") == "approved_count"
        if bool(observation.get("created_unit")) != expected_birth:
            raise _OriginUnproven
        if not observation.get("created_unit") and birth is not None:
            raise _OriginUnproven
        if expected_birth and birth.get("source_unit_id") is not None:
            raise _OriginUnproven
        basis = "approved_count"
        root_operation_id = operation["operation_id"]
    elif operation["kind"] == "receive" and birth and birth.get("basis") == "received":
        unit_result = result.get("unit")
        if not is_anchor or previous_qty is not None or not isinstance(unit_result, dict) or unit_result.get("id") != unit_id:
            raise _OriginUnproven
        if len(rows) != 1:
            raise _OriginUnproven
        delta = _origin_qty(rows[0]["qty_delta"])
        current_qty = _origin_qty(unit_result.get("qty"))
        current_dimensions = _origin_result_dimensions(unit_result)
        if (
            not _origin_uuid_text(unit_result.get("id"))
            or request.get("kind") != "receive"
            or result.get("source_ref") != request.get("source_ref")
            or current_dimensions["warehouse_id"] != operation["warehouse_id"]
            or current_dimensions["location_id"] != request.get("location_id")
            or current_dimensions["sku_code"] != request.get("sku_code")
            or current_dimensions["batch_id"] != request.get("batch_id")
            or current_dimensions["characteristic"] != request.get("characteristic")
            or current_dimensions["purpose"] != request.get("purpose")
            or current_dimensions["packaging_id"] != request.get("packaging_id")
            or current_dimensions["package_state"] != request.get("package_state")
            or current_dimensions["quality"] != "quarantine"
            or current_dimensions["container"] != ""
            or current_dimensions["inspection_ref"] != ""
            or delta <= 0 or current_qty != delta or _origin_qty(request.get("qty")) != delta
            or _origin_qty(unit_result.get("reserved")) != 0
            or _origin_qty(unit_result.get("free")) != current_qty
            or unit_result.get("batch_scan") != f"LOT-{current_dimensions['batch_id']}-{unit_id.upper()}"
        ):
            raise _OriginUnproven
        if rows[0]["dimensions"] != current_dimensions:
            raise _OriginUnproven
        basis = "received"
        root_operation_id = operation["operation_id"]
    elif birth and birth.get("basis") == "clone":
        unit_result = result.get("unit")
        source_id = birth.get("source_unit_id")
        if (
            not isinstance(unit_result, dict) or unit_result.get("id") != unit_id
            or not _origin_uuid_text(source_id) or source_id == unit_id
        ):
            raise _OriginUnproven
        if len(rows) != 1 or _origin_qty(rows[0]["qty_delta"]) <= 0:
            raise _OriginUnproven
        current_qty = _origin_qty(unit_result.get("qty"))
        current_dimensions = _origin_result_dimensions(unit_result)
        if current_qty != _origin_qty(rows[0]["qty_delta"]) or rows[0]["dimensions"] != current_dimensions:
            raise _OriginUnproven
        operation_rows = (await session.execute(
            select(PhysicalLedger.unit_id, PhysicalLedger.qty_delta, PhysicalLedger.dimensions)
            .where(PhysicalLedger.operation_pk == operation["id"])
        )).all()
        if len(operation_rows) != 2:
            raise _OriginUnproven
        pair_by_unit = {row[0]: row for row in operation_rows}
        if len(pair_by_unit) != 2 or set(pair_by_unit) != {source_id, unit_id}:
            raise _OriginUnproven
        _, source_tokens = _origin_group_frontier(group, source_id)
        parent = await _prove_unit_boundary(
            session, organization_id, source_id, source_tokens,
            current_unit=None, trail=trail, historical=prior_historical,
        )
        qc_birth_operation_id = parent.get("qc_birth_operation_id")
        source_row = pair_by_unit[source_id]
        child_row = pair_by_unit[unit_id]
        source_delta = _origin_qty(str(source_row[1]))
        child_delta = _origin_qty(str(child_row[1]))
        source_dimensions = _origin_dimensions(source_row[2])
        child_dimensions = _origin_dimensions(child_row[2])
        move_request = causal.get("delegated_move") if operation["kind"] == "task_complete" else request
        requested_qty = "1" if operation["kind"] == "assembly_qc" else move_request.get("qty") if isinstance(move_request, dict) else None
        if not isinstance(move_request, dict) or _origin_qty(requested_qty) != current_qty:
            raise _OriginUnproven
        destination_warehouse_id = operation["warehouse_id"]
        if operation["kind"] in {"move", "task_complete"}:
            if (
                move_request.get("kind") != "move"
                or type(move_request.get("warehouse_id")) is not int
                or move_request["warehouse_id"] != operation["warehouse_id"]
            ):
                raise _OriginUnproven
            destination_warehouse_id = move_request.get("destination_warehouse_id")
            if destination_warehouse_id is None:
                destination_warehouse_id = operation["warehouse_id"]
            elif type(destination_warehouse_id) is not int or destination_warehouse_id <= 0:
                raise _OriginUnproven
            if destination_warehouse_id != operation["warehouse_id"]:
                destination_organization_id = await session.scalar(
                    select(WarehouseControl.organization_id).where(
                        WarehouseControl.warehouse_id == destination_warehouse_id,
                    ).execution_options(populate_existing=True)
                )
                if destination_organization_id != organization_id:
                    raise _OriginUnproven
        if (
            source_delta >= 0 or child_delta <= 0 or source_delta + child_delta != 0
            or -source_delta != current_qty or child_delta != current_qty
            or source_dimensions != parent["dimensions"] or child_dimensions != current_dimensions
            or source_dimensions["warehouse_id"] != operation["warehouse_id"]
            or child_dimensions["warehouse_id"] != destination_warehouse_id
        ):
            raise _OriginUnproven
        _origin_validate_clone_transition(
            operation["kind"], request, causal, source_id, parent["dimensions"], current_dimensions,
        )
        if operation["kind"] == "assembly_qc":
            await _origin_validate_assembly_qc(session, organization_id, operation, source_id, unit_id,
                parent.get("qc_birth_operation_id"), historical=historical)
        if parent["qty"] + source_delta < 0:
            raise _OriginUnproven
        basis = "clone"
        root_operation_id = parent["root_operation_id"]
    elif operation["kind"] in {"quality", "maintenance", "open_package", "move", "task_complete", "assembly_qc"}:
        if previous_qty is None or previous_dimensions is None or len(rows) != 1:
            raise _OriginUnproven
        target_delta = _origin_qty(rows[0]["qty_delta"])
        if target_delta >= 0 or rows[0]["dimensions"] != previous_dimensions:
            raise _OriginUnproven
        source_id = request.get("unit_id")
        if operation["kind"] == "task_complete":
            delegated = causal.get("delegated_move")
            source_id = delegated.get("unit_id") if isinstance(delegated, dict) else None
        if source_id != unit_id:
            raise _OriginUnproven
        counterpart = (await session.execute(
            select(PhysicalLedger.unit_id, PhysicalLedger.qty_delta, PhysicalLedger.dimensions)
            .where(PhysicalLedger.operation_pk == operation["id"], PhysicalLedger.unit_id != unit_id)
        )).all()
        unit_result = result.get("unit")
        if (
            len(counterpart) != 1
            or _origin_qty(str(counterpart[0][1])) != -target_delta
            or not isinstance(unit_result, dict)
            or unit_result.get("id") != counterpart[0][0]
        ):
            raise _OriginUnproven
        child_dimensions = _origin_result_dimensions(unit_result)
        child_qty = _origin_qty(unit_result.get("qty"))
        if child_qty != -target_delta or counterpart[0][2] != child_dimensions:
            raise _OriginUnproven
        counterpart_birth = _origin_birth(causal, counterpart[0][0])
        if (
            counterpart_birth is None
            or counterpart_birth.get("basis") != "clone"
            or counterpart_birth.get("source_unit_id") != unit_id
        ):
            raise _OriginUnproven
        _origin_validate_clone_transition(
            operation["kind"], request, causal, unit_id, previous_dimensions, child_dimensions,
        )
        if operation["kind"] not in {"task_complete", "assembly_qc"} and _origin_qty(request.get("qty")) != child_qty:
            raise _OriginUnproven
        if operation["kind"] == "assembly_qc":
            await _origin_validate_assembly_qc(session, organization_id, operation, unit_id, counterpart[0][0],
                None, historical=historical)
        if previous_qty + target_delta < 0:
            raise _OriginUnproven
        current_qty = previous_qty + target_delta
        current_dimensions = previous_dimensions
        basis = "clone_suffix"
    elif operation["kind"] in {"pick", "unpick"}:
        if previous_qty is None or previous_dimensions is None or len(rows) != 1:
            raise _OriginUnproven
        target_delta = _origin_qty(rows[0]["qty_delta"])
        if rows[0]["dimensions"] != previous_dimensions:
            raise _OriginUnproven
        counterpart = (await session.execute(
            select(PhysicalLedger.unit_id, PhysicalLedger.qty_delta, PhysicalLedger.dimensions)
            .where(PhysicalLedger.operation_pk == operation["id"], PhysicalLedger.unit_id != unit_id)
        )).all()
        if len(counterpart) != 1 or _origin_qty(str(counterpart[0][1])) != -target_delta:
            raise _OriginUnproven
        counterpart_dimensions = _origin_dimensions(counterpart[0][2])
        if counterpart_dimensions["warehouse_id"] != operation["warehouse_id"]:
            raise _OriginUnproven
        if operation["kind"] == "pick":
            pick_receipt = await _origin_pick_receipt(
                session, organization_id, operation["warehouse_id"], result.get("pick_id"), historical=historical,
            )
            if (
                target_delta >= 0
                or pick_receipt["operation"]["id"] != operation["id"]
                or pick_receipt["source_unit_id"] != unit_id
                or pick_receipt["child_unit_id"] != counterpart[0][0]
                or -target_delta != pick_receipt["qty"]
                or pick_receipt["source_dimensions"] != previous_dimensions
                or pick_receipt["child_dimensions"] != counterpart_dimensions
            ):
                raise _OriginUnproven
        else:
            pick_receipt = await _origin_pick_receipt(
                session, organization_id, operation["warehouse_id"], request.get("pick_id"), historical=prior_historical,
            )
            if (
                target_delta <= 0
                or pick_receipt["source_unit_id"] != unit_id
                or pick_receipt["child_unit_id"] != counterpart[0][0]
                or target_delta != pick_receipt["qty"]
                or _origin_qty(result.get("qty")) != target_delta
                or result.get("returned_to_location") != previous_dimensions["location_id"]
                or not _origin_same_identity(previous_dimensions, pick_receipt["source_dimensions"])
                or not _origin_same_identity(counterpart_dimensions, pick_receipt["child_dimensions"])
            ):
                raise _OriginUnproven
            await _origin_pick_precedes(
                session, organization_id, causal, pick_receipt["operation"]["id"],
                (pick_receipt["source_unit_id"], pick_receipt["child_unit_id"]), historical=prior_historical,
            )
        current_qty = previous_qty + target_delta
        current_dimensions = previous_dimensions
        if current_qty < 0:
            raise _OriginUnproven
        basis = "clone_suffix"
    elif operation["kind"] == "pack_v2":
        if previous_qty is None or previous_dimensions is None or len(rows) != 2:
            raise _OriginUnproven
        negative = [row for row in rows if _origin_qty(row["qty_delta"]) < 0]
        positive = [row for row in rows if _origin_qty(row["qty_delta"]) > 0]
        if len(negative) != 1 or len(positive) != 1:
            raise _OriginUnproven
        before = _origin_qty(negative[0]["qty_delta"])
        after = _origin_qty(positive[0]["qty_delta"])
        old_dimensions = _origin_dimensions(negative[0]["dimensions"])
        new_dimensions = _origin_dimensions(positive[0]["dimensions"])
        if before + after != 0 or before != -previous_qty or old_dimensions != previous_dimensions:
            raise _OriginUnproven
        if (
            request.get("kind") != "pack_v2"
            or request.get("protocol") != 2
            or result.get("packed") is not True
            or result.get("protocol") != 2
            or result.get("container") != request.get("container")
            or new_dimensions["container"] != request.get("container")
            or new_dimensions["location_id"] != request.get("destination_id")
            or not isinstance(request.get("lines"), list)
        ):
            raise _OriginUnproven
        pick_receipt = await _origin_pick_receipt(
            session, organization_id, operation["warehouse_id"], child_unit_id=unit_id, historical=prior_historical,
        )
        matching_lines = [line for line in request["lines"] if line.get("pick_id") == pick_receipt["pick_id"]]
        if len(matching_lines) != 1:
            raise _OriginUnproven
        line = matching_lines[0]
        if (
            pick_receipt["child_unit_id"] != unit_id
            or pick_receipt["child_dimensions"]["container"] != request.get("container")
            or not _origin_same_identity(old_dimensions, pick_receipt["child_dimensions"])
            or _origin_qty(line.get("qty")) != -before
            or line.get("batch_scan") != pick_receipt["request"].get("batch_scan")
        ):
            raise _OriginUnproven
        await _origin_pick_precedes(
            session, organization_id, causal, pick_receipt["operation"]["id"], (unit_id,), historical=prior_historical,
        )
        if not _origin_same_identity(old_dimensions, new_dimensions):
            raise _OriginUnproven
        for key in _ORIGIN_DIMENSION_KEYS:
            if key not in {"location_id"} and old_dimensions[key] != new_dimensions[key]:
                raise _OriginUnproven
        current_qty, current_dimensions = previous_qty, new_dimensions
        basis = "clone_suffix"
    else:
        raise _OriginUnproven

    if not isinstance(current_qty, Decimal) or current_dimensions is None:
        raise _OriginUnproven
    if is_anchor and basis == "clone":
        if anchor_kind != "clone" or anchor_source != birth.get("source_unit_id"):
            raise _OriginUnproven
    return {
        "qty": current_qty,
        "dimensions": current_dimensions,
        "basis": basis,
        "root_operation_id": root_operation_id,
        "tail_operation_id": operation["operation_id"],
        **({"qc_birth_operation_id": qc_birth_operation_id} if qc_birth_operation_id is not None else {}),
    }


def _origin_operation_snapshot(operation):
    return {"id": operation.id, "organization_id": operation.organization_id,
        "operation_id": operation.operation_id, "warehouse_id": operation.warehouse_id,
        "kind": operation.kind, "request_hash": operation.request_hash,
        "result": operation.result, "actor": operation.actor,
        "created_at": operation.created_at.isoformat() if operation.created_at else None}


async def _origin_validate_assembly_qc(session, organization_id, operation, source_id, child_id, qc_birth, *, historical=None):
    from modules.wms.assembly_quality import physical_assembly_identity, physical_assembly_proof, physical_assembly_validate_qc
    from modules.wms.physical_models import AssemblyOutputIdentity, PhysicalOperation
    from modules.wms.physical_schemas import AssemblyQcProof
    try:
        typed = physical_assembly_proof(AssemblyQcProof, operation["result"].get("assembly_qc_v1"))
        if historical is None:
            identity, _output, _birth = await physical_assembly_identity(
                session, organization_id, str(typed.command.identity_id),
            )
            saved = await session.get(PhysicalOperation, operation["id"])
            boundary = await physical_assembly_validate_qc(session, identity, saved, typed)
        else:
            historical.snapshot(operation)
            actual_rows = (await session.scalars(select(PhysicalOperation).where(
                PhysicalOperation.id == operation["id"],
                PhysicalOperation.id > 0,
                PhysicalOperation.id < historical.before_pk,
                PhysicalOperation.organization_id == organization_id,
                PhysicalOperation.warehouse_id == operation["warehouse_id"],
                PhysicalOperation.kind == "assembly_qc",
                PhysicalOperation.operation_id == operation["operation_id"],
            ).execution_options(populate_existing=True))).all()
            if len(actual_rows) != 1:
                raise _OriginUnproven
            saved = historical.operation(actual_rows[0])
            if _origin_operation_snapshot(saved) != operation:
                raise _OriginUnproven
            identity = await session.scalar(select(AssemblyOutputIdentity).where(
                AssemblyOutputIdentity.id == str(typed.command.identity_id),
                AssemblyOutputIdentity.organization_id == organization_id,
            ).execution_options(populate_existing=True))
            if identity is None:
                raise _OriginUnproven
            boundary = await physical_assembly_validate_qc(
                session, identity, saved, typed, historical=historical,
            )
        proof = typed.model_dump(mode="json")
        if (proof["verdict"] != "PASS" or proof["command"]["unit_id"] != source_id
                or proof["released_unit_id"] != child_id
                or (qc_birth is not None and proof["command"]["qc_birth_operation_id"] != qc_birth)):
            raise _OriginUnproven
        if historical is not None:
            root_id = boundary.get("root_operation_id") if isinstance(boundary, dict) else None
            birth_pk = identity.birth_operation_pk
            if (type(birth_pk) is not int or birth_pk <= 0 or not _origin_uuid_text(root_id)):
                raise _OriginUnproven
            births = (await session.scalars(select(PhysicalOperation).where(
                PhysicalOperation.id == birth_pk,
                PhysicalOperation.id > 0,
                PhysicalOperation.id < operation["id"],
                PhysicalOperation.organization_id == organization_id,
                PhysicalOperation.operation_id == root_id,
                PhysicalOperation.kind == "assembly_output",
            ).execution_options(populate_existing=True))).all()
            if len(births) != 1:
                raise _OriginUnproven
            anchor = births[0]
            birth_context = historical.before_warehouse(anchor.warehouse_id, operation["id"])
            checked_identity, _output, checked_birth = await physical_assembly_identity(
                session, organization_id, identity.id, historical=birth_context,
            )
            if (checked_identity.id != identity.id
                    or checked_identity.birth_operation_pk != birth_pk
                    or checked_birth.id != birth_pk
                    or checked_birth.operation_id != root_id):
                raise _OriginUnproven
    except (HTTPException, KeyError, TypeError, ValueError) as exc:
        raise _OriginUnproven from exc


async def _origin_assembly_transition(session, organization_id, unit_id, group, previous_qty,
        previous_dimensions, trail, *, is_anchor, causal, request, result, rows, birth, historical=None):
    from modules.wms.assembly_quality import physical_assembly_identity, physical_assembly_proof
    from modules.wms.physical_models import AssemblyConsumption, AssemblyInputLink, AssemblyOutputIdentity, PhysicalLedger, PhysicalOperation, StockUnit
    from modules.wms.physical_schemas import AssemblyOutputProof, AssemblyReturnProof, AssemblyConsumeProof
    operation = group["operation"]
    kind = operation["kind"]
    prior_historical = None if historical is None else historical.before(operation["id"])
    qc_birth = None
    root = None
    basis = "clone_suffix"
    try:
        if kind in {"assembly_output", "assembly_return"}:
            model = AssemblyOutputProof if kind == "assembly_output" else AssemblyReturnProof
            proof = physical_assembly_proof(model, result.get(f"{kind}_v1")).model_dump(mode="json")
            if kind == "assembly_return" and historical is not None:
                identity = await session.scalar(select(AssemblyOutputIdentity).where(
                    AssemblyOutputIdentity.id == proof["identity_id"],
                    AssemblyOutputIdentity.organization_id == organization_id,
                ).execution_options(populate_existing=True))
                if identity is None or type(identity.birth_operation_pk) is not int or identity.birth_operation_pk <= 0:
                    raise _OriginUnproven
                _output = _birth = None
            else:
                identity, _output, _birth = await physical_assembly_identity(
                    session, organization_id, proof["identity_id"], trail=trail, historical=historical,
                )
            unit_identity = (getattr(await session.get(StockUnit, unit_id), "assembly_output_id", None)
                if historical is None else await historical.identity(session, unit_id))
            if (not is_anchor or birth is None or birth["basis"] != kind or len(rows) != 1
                    or _origin_qty(rows[0]["qty_delta"]) != 1
                    or unit_identity != identity.id or result.get("unit", {}).get("id") != unit_id
                    or result["unit"].get("assembly_output_id") != identity.id):
                raise _OriginUnproven
            current_dimensions = _origin_result_dimensions(result["unit"])
            if (rows[0]["dimensions"] != current_dimensions or current_dimensions["quality"] != "quarantine"
                    or _origin_qty(result["unit"].get("qty")) != 1 or _origin_qty(result["unit"].get("reserved")) != 0
                    or request["location_id"] != current_dimensions["location_id"]):
                raise _OriginUnproven
            all_rows = (await session.scalars(select(PhysicalLedger).where(PhysicalLedger.operation_pk == operation["id"]))).all()
            if len(all_rows) != 1:
                raise _OriginUnproven
            if kind == "assembly_output":
                if identity.birth_operation_pk != operation["id"]:
                    raise _OriginUnproven
                root = operation["operation_id"]
            else:
                if proof["returned_unit_id"] != unit_id or proof["executor"]["original"]["sub"] != operation["actor"]:
                    raise _OriginUnproven
                saved = await _prove_assembly_shipment(session, organization_id, operation["warehouse_id"],
                    identity, proof["ship_operation_id"], proof["pick_id"], trail=trail, historical=prior_historical)
                if {key: proof[key] for key in saved} != saved:
                    raise _OriginUnproven
                # Return stores the exact shipped-zero boundary as a causal dependency.
                ship_frontier = causal["prior_frontiers"].get(proof["shipped_unit_id"])
                if ship_frontier is None:
                    raise _OriginUnproven
                zero = await _prove_unit_boundary(session, organization_id, proof["shipped_unit_id"],
                    _origin_frontier_tokens(ship_frontier), trail=trail, allow_zero=True, historical=prior_historical)
                if zero["qty"] != 0 or zero["tail_operation_id"] != proof["ship_operation_id"]:
                    raise _OriginUnproven
                if historical is not None:
                    root_id = zero.get("root_operation_id")
                    if not _origin_uuid_text(root_id):
                        raise _OriginUnproven
                    birth_rows = (await session.scalars(select(PhysicalOperation).where(
                        PhysicalOperation.id == identity.birth_operation_pk,
                        PhysicalOperation.id > 0,
                        PhysicalOperation.id < operation["id"],
                        PhysicalOperation.organization_id == organization_id,
                        PhysicalOperation.operation_id == root_id,
                        PhysicalOperation.kind == "assembly_output",
                    ).execution_options(populate_existing=True))).all()
                    if len(birth_rows) != 1:
                        raise _OriginUnproven
                    anchor = birth_rows[0]
                    birth_context = historical.before_warehouse(anchor.warehouse_id, operation["id"])
                    identity, _output, original = await physical_assembly_identity(
                        session, organization_id, identity.id, trail=trail, historical=birth_context,
                    )
                    if (identity.birth_operation_pk != anchor.id or original.id != anchor.id
                            or original.operation_id != root_id):
                        raise _OriginUnproven
                else:
                    original = _birth
                    if original is None:
                        raise _OriginUnproven
                root = original.operation_id
            current_qty, basis, qc_birth = Decimal(1), kind, operation["operation_id"]
        elif kind in {"assembly_kit_move", "assembly_unused_return"}:
            if kind == "assembly_kit_move":
                link = await session.get(AssemblyInputLink, result.get("input_link_id"))
                if link is None or link.kit_move_operation_id != operation["operation_id"]:
                    raise _OriginUnproven
                source_id, child_id, amount = link.original_unit_id, link.actual_kit_unit_id, Decimal(link.qty)
            else:
                entries = [entry for entry in result.get("returned", []) if isinstance(entry, dict)
                    and (entry.get("returned_unit_id") == unit_id or (
                        birth is None and entry.get("input_link_id") is not None))]
                candidates = []
                for entry in entries:
                    link = await session.get(AssemblyInputLink, entry.get("input_link_id"))
                    if link is not None and unit_id in {link.actual_kit_unit_id, entry.get("returned_unit_id")}:
                        candidates.append((entry, link))
                if len(candidates) != 1:
                    raise _OriginUnproven
                entry, link = candidates[0]
                source_id, child_id, amount = link.actual_kit_unit_id, entry["returned_unit_id"], _origin_qty(entry["qty"])
            if link.execution_id != request["execution_id"] or len(rows) != 1 or amount <= 0:
                raise _OriginUnproven
            pair = (await session.scalars(select(PhysicalLedger).where(
                PhysicalLedger.operation_pk == operation["id"], PhysicalLedger.unit_id.in_([source_id, child_id])))).all()
            if len(pair) != 2 or {row.unit_id for row in pair} != {source_id, child_id}:
                raise _OriginUnproven
            by_id = {row.unit_id: row for row in pair}
            parent = await _prove_unit_boundary(session, organization_id, source_id,
                _origin_frontier_tokens(causal["prior_frontiers"][source_id]), trail=trail, historical=prior_historical)
            source_dimensions, child_dimensions = _origin_dimensions(by_id[source_id].dimensions), _origin_dimensions(by_id[child_id].dimensions)
            if (Decimal(by_id[source_id].qty_delta) != -amount or Decimal(by_id[child_id].qty_delta) != amount
                    or source_dimensions != parent["dimensions"] or parent["qty"] < amount):
                raise _OriginUnproven
            _origin_validate_clone_transition(kind, request, causal, source_id, source_dimensions, child_dimensions)
            if kind == "assembly_kit_move":
                if (_origin_qty(request["qty"]) != amount or request["hold_id"] != link.hold_id
                        or child_dimensions["location_id"] != request["destination_id"]
                        or source_dimensions != link.source_dimensions or child_dimensions != link.kit_dimensions):
                    raise _OriginUnproven
            else:
                requested = [line for line in request["returns"] if line["input_link_id"] == link.id]
                if (len(requested) != 1 or _origin_qty(requested[0]["qty"]) != amount
                        or child_dimensions["location_id"] != link.source_dimensions["location_id"]):
                    raise _OriginUnproven
            if unit_id == child_id:
                if not is_anchor or birth is None or birth["source_unit_id"] != source_id:
                    raise _OriginUnproven
                current_qty, current_dimensions, basis = amount, child_dimensions, "clone"
                root, qc_birth = parent["root_operation_id"], parent.get("qc_birth_operation_id")
            else:
                if is_anchor or previous_dimensions != source_dimensions or previous_qty != parent["qty"]:
                    raise _OriginUnproven
                current_qty, current_dimensions = previous_qty - amount, previous_dimensions
        elif kind == "assembly_consume":
            proof = physical_assembly_proof(AssemblyConsumeProof, result.get("assembly_consume_proof_v1")).model_dump(mode="json")
            traces = (await session.scalars(select(AssemblyConsumption).join(AssemblyInputLink,
                AssemblyConsumption.input_link_id == AssemblyInputLink.id).where(
                AssemblyConsumption.operation_id == operation["operation_id"], AssemblyInputLink.actual_kit_unit_id == unit_id))).all()
            if (is_anchor or previous_qty is None or previous_dimensions is None or len(rows) != 1 or not traces
                    or proof["execution_id"] != request["execution_id"] or proof["output_key"] != request["output_key"]
                    or proof["executor"]["original"]["sub"] != operation["actor"]):
                raise _OriginUnproven
            amount = sum((Decimal(trace.qty) for trace in traces), Decimal(0))
            for trace in traces:
                if (trace.execution_id != proof["execution_id"] or trace.output_key != proof["output_key"]
                        or _origin_result_dimensions(trace.dimensions) != previous_dimensions
                        or _origin_qty(trace.dimensions.get("qty")) != Decimal(trace.qty)):
                    raise _OriginUnproven
            if _origin_qty(rows[0]["qty_delta"]) != -amount or rows[0]["dimensions"] != previous_dimensions:
                raise _OriginUnproven
            current_qty, current_dimensions = previous_qty - amount, previous_dimensions
        else:  # A shipment is a negative whole-unit suffix, never a birth.
            if is_anchor or previous_qty is None or previous_dimensions is None or len(rows) != 1:
                raise _OriginUnproven
            if historical is not None:
                # Prove the whole immutable ship packet, including every other
                # line; a selected unit must not hide an extra counterpart.
                packet = (await session.scalars(select(PhysicalLedger).where(PhysicalLedger.operation_pk == operation['id']))).all()
                shipped = result.get('shipped')
                requested_lines = request.get('lines')
                if (not isinstance(shipped, list) or not isinstance(requested_lines, list)
                        or len({line.get('pick_id') for line in shipped}) != len(shipped)
                        or len({line.get('pick_id') for line in requested_lines}) != len(requested_lines)
                        or {line.get('pick_id') for line in shipped} != {line.get('pick_id') for line in requested_lines}):
                    raise _OriginUnproven
                expected_packet = []
                for line in shipped:
                    actual_pick = await _origin_pick_receipt(session, organization_id, operation['warehouse_id'],
                        line['pick_id'], historical=prior_historical)
                    amount = _origin_qty(line['qty'])
                    requested_line = next(item for item in requested_lines if item['pick_id'] == line['pick_id'])
                    dims = _origin_result_dimensions(line)
                    if amount <= 0 or amount > actual_pick['qty'] or _origin_qty(requested_line['qty']) != amount:
                        raise _OriginUnproven
                    pre = await _prove_unit_boundary(session, organization_id, actual_pick['child_unit_id'],
                        _origin_frontier_tokens(causal['prior_frontiers'][actual_pick['child_unit_id']]),
                        trail=trail, historical=prior_historical)
                    if pre['qty'] < amount or pre['dimensions'] != dims:
                        raise _OriginUnproven
                    expected_packet.append((actual_pick['child_unit_id'], -amount, dims))
                from modules.wms.physical_stock import canonical
                if (len(packet) != len(expected_packet)
                        or sorted((item.unit_id, Decimal(item.qty_delta), canonical(item.dimensions)) for item in packet)
                        != sorted((uid, quantity, canonical(dims)) for uid, quantity, dims in expected_packet)):
                    raise _OriginUnproven
            matching = [line for line in result.get("shipped", []) if isinstance(line, dict)
                and _origin_result_dimensions(line) == previous_dimensions]
            if len(matching) != 1:
                raise _OriginUnproven
            line = matching[0]
            receipt = await _origin_pick_receipt(session, organization_id, operation["warehouse_id"], line.get("pick_id"), historical=historical)
            requested = [item for item in request["lines"] if item["pick_id"] == line["pick_id"]]
            amount = _origin_qty(line.get("qty"))
            if (receipt["child_unit_id"] != unit_id or len(requested) != 1
                    or _origin_qty(requested[0]["qty"]) != amount or rows[0]["dimensions"] != previous_dimensions
                    or _origin_qty(rows[0]["qty_delta"]) != -amount or amount <= 0):
                raise _OriginUnproven
            current_qty, current_dimensions = previous_qty - amount, previous_dimensions
        if current_qty < 0:
            raise _OriginUnproven
        return {"qty": current_qty, "dimensions": current_dimensions, "basis": basis,
            "root_operation_id": root, "tail_operation_id": operation["operation_id"],
            **({"qc_birth_operation_id": qc_birth} if qc_birth is not None else {})}
    except (HTTPException, KeyError, TypeError, ValueError) as exc:
        raise _OriginUnproven from exc


async def _prove_assembly_shipment(session, organization_id, warehouse_id, identity, ship_operation_id, pick_id, *, trail=frozenset(), historical=None):
    from modules.wms.physical_models import PhysicalLedger, PickedAllocation, StockUnit
    from modules.wms.physical_stock import digest

    ship = await session.scalar(select(PhysicalOperation).where(PhysicalOperation.organization_id == organization_id,
        PhysicalOperation.warehouse_id == warehouse_id, PhysicalOperation.operation_id == ship_operation_id,
        PhysicalOperation.kind == "ship").execution_options(populate_existing=True))
    if ship is None:
        raise _OriginUnproven
    if historical is not None:
        historical.operation(ship)
    prior_historical = None if historical is None else historical.before(ship.id)
    causal, request, result = _origin_validate_operation(_origin_operation_snapshot(ship), organization_id)
    receipt = await _origin_pick_receipt(session, organization_id, warehouse_id, pick_id, historical=historical)
    unit_id = receipt["child_unit_id"]
    if historical is None:
        pick = await session.get(PickedAllocation, pick_id, populate_existing=True)
        unit = await session.get(StockUnit, unit_id, populate_existing=True)
        identity_valid = (pick is not None and unit is not None and unit.assembly_output_id == identity.id
            and pick.unit_id == unit_id and not pick.canceled and Decimal(pick.shipped) == 1 and Decimal(pick.qty) == 1)
    else:
        identity_valid = (receipt["qty"] == 1 and receipt["operation"]["id"] < ship.id
            and await prior_historical.identity(session, unit_id) == identity.id)
    lines = [line for line in result.get("shipped", []) if isinstance(line, dict) and line.get("pick_id") == pick_id]
    requested = [line for line in request["lines"] if line.get("pick_id") == pick_id]
    ledgers = (await session.scalars(select(PhysicalLedger).where(PhysicalLedger.operation_pk == ship.id,
        PhysicalLedger.unit_id == unit_id))).all()
    if (not identity_valid
            or len(lines) != 1 or len(requested) != 1 or len(ledgers) != 1
            or _origin_qty(lines[0].get("qty")) != 1 or _origin_qty(requested[0].get("qty")) != 1
            or Decimal(ledgers[0].qty_delta) != -1):
        raise _OriginUnproven
    prior = causal["prior_frontiers"].get(unit_id)
    if prior is None:
        raise _OriginUnproven
    pre_ship = await _prove_unit_boundary(session, organization_id, unit_id,
        _origin_frontier_tokens(prior), trail=trail, historical=prior_historical)
    if (pre_ship["qty"] != 1 or ledgers[0].dimensions != pre_ship["dimensions"]
            or _origin_result_dimensions(lines[0]) != pre_ship["dimensions"]
            or pre_ship["dimensions"]["quality"] != "released" or pre_ship.get("qc_birth_operation_id") is None):
        raise _OriginUnproven
    return {"protocol": 1, "identity_id": identity.id, "ship_operation_id": ship.operation_id,
        "ship_request_hash": ship.request_hash, "pick_id": pick_id, "shipped_unit_id": unit_id,
        "ship_ledger_id": ledgers[0].id, "pre_ship_frontier_digest": digest(prior)}


async def prove_assembly_shipment_return(session, organization_id, warehouse_id, identity, ship_operation_id, pick_id):
    from modules.wms.physical_models import StockUnit
    from modules.wms.physical_stock import physical_unit_frontier
    try:
        proof = await _prove_assembly_shipment(session, organization_id, warehouse_id, identity, ship_operation_id, pick_id)
        unit = await session.get(StockUnit, proof["shipped_unit_id"], populate_existing=True)
        frontier, _ = await physical_unit_frontier(session, organization_id, unit.id)
        current = await _prove_unit_boundary(session, organization_id, unit.id, _origin_frontier_tokens(frontier),
            current_unit=unit, allow_zero=True)
        if current["qty"] != 0 or Decimal(unit.reserved) != 0 or current["tail_operation_id"] != ship_operation_id:
            raise _OriginUnproven
        return proof
    except _OriginUnproven as exc:
        raise HTTPException(409, "Assembly shipment return origin is unproven") from exc


async def assembly_unit_qc_birth(session, organization_id, unit):
    from modules.wms.physical_stock import physical_unit_frontier
    try:
        frontier, _ = await physical_unit_frontier(session, organization_id, unit.id)
        proof = await _prove_unit_boundary(session, organization_id, unit.id, _origin_frontier_tokens(frontier), current_unit=unit)
        if unit.assembly_output_id is None or proof.get("qc_birth_operation_id") is None:
            raise _OriginUnproven
        return proof["qc_birth_operation_id"]
    except _OriginUnproven as exc:
        raise HTTPException(409, "Assembly unit QC birth is unproven") from exc


async def _origin_pick_receipt(session, organization_id, warehouse_id, pick_id=None, *, child_unit_id=None, historical=None):
    from modules.wms.physical_models import PhysicalLedger, PhysicalOperation

    if pick_id is None and not _origin_uuid_text(child_unit_id):
        raise _OriginUnproven
    if pick_id is not None and (type(pick_id) is not int or pick_id <= 0):
        raise _OriginUnproven
    selector = (
        PhysicalOperation.result["pick_id"].as_integer() == pick_id
        if pick_id is not None
        else PhysicalOperation.result["unit"]["id"].as_string() == child_unit_id
    )
    matching_operations = (await session.scalars(
        select(PhysicalOperation).where(
            PhysicalOperation.organization_id == organization_id,
            PhysicalOperation.warehouse_id == warehouse_id,
            PhysicalOperation.kind == "pick",
            selector,
            *([] if historical is None else [PhysicalOperation.id < historical.before_pk]),
        ).execution_options(populate_existing=True)
    )).all()
    if len(matching_operations) != 1:
        raise _OriginUnproven
    if historical is not None:
        matching_operations = [row for row in matching_operations if row.id < historical.before_pk]
        if len(matching_operations) != 1:
            raise _OriginUnproven
    saved = matching_operations[0]
    if historical is not None:
        historical.operation(saved)
    operation = {
        "id": saved.id,
        "organization_id": saved.organization_id,
        "operation_id": saved.operation_id,
        "warehouse_id": saved.warehouse_id,
        "kind": saved.kind,
        "request_hash": saved.request_hash,
        "result": saved.result,
        "actor": saved.actor,
        "created_at": saved.created_at.isoformat() if saved.created_at else None,
    }
    causal, request, result = _origin_validate_operation(operation, organization_id)
    if pick_id is None:
        pick_id = result.get("pick_id")
    if (
        operation["kind"] != "pick"
        or request.get("kind") != "pick"
        or type(request.get("hold_id")) is not int or request["hold_id"] <= 0
        or type(result.get("pick_id")) is not int or result["pick_id"] != pick_id
    ):
        raise _OriginUnproven
    unit_result = result.get("unit")
    if not isinstance(unit_result, dict) or not _origin_uuid_text(unit_result.get("id")):
        raise _OriginUnproven
    ledger_rows = (await session.execute(
        select(PhysicalLedger.unit_id, PhysicalLedger.qty_delta, PhysicalLedger.dimensions)
        .where(PhysicalLedger.operation_pk == operation["id"])
    )).all()
    if len(ledger_rows) != 2:
        raise _OriginUnproven
    deltas = [(row, _origin_qty(str(row[1]))) for row in ledger_rows]
    negative = [(row, delta) for row, delta in deltas if delta < 0]
    positive = [(row, delta) for row, delta in deltas if delta > 0]
    if len(negative) != 1 or len(positive) != 1 or negative[0][0][0] == positive[0][0][0]:
        raise _OriginUnproven
    source_row, source_delta = negative[0]
    child_row, child_qty = positive[0]
    source_id, child_id = source_row[0], child_row[0]
    source_dimensions = _origin_dimensions(source_row[2])
    child_dimensions = _origin_dimensions(child_row[2])
    qty = -source_delta
    result_dimensions = _origin_result_dimensions(unit_result)
    if (
        child_id != unit_result.get("id")
        or qty != child_qty
        or _origin_qty(request.get("qty")) != qty
        or _origin_qty(unit_result.get("qty")) != qty
        or _origin_qty(unit_result.get("reserved")) != qty
        or _origin_qty(unit_result.get("free")) != 0
        or result_dimensions != child_dimensions
        or not _origin_same_identity(source_dimensions, child_dimensions)
        or source_dimensions["warehouse_id"] != warehouse_id
        or child_dimensions["warehouse_id"] != warehouse_id
        or request.get("destination_id") != child_dimensions["location_id"]
        or request.get("location_scan") != f"LOC-{source_dimensions['location_id']}"
        or request.get("batch_scan") != f"LOT-{source_dimensions['batch_id']}-{source_id.upper()}"
        or request.get("container_scan") != f"BOX-{child_dimensions['container']}"
        or unit_result.get("batch_scan") != f"LOT-{child_dimensions['batch_id']}-{child_id.upper()}"
        or not isinstance(result.get("lot"), str)
    ):
        raise _OriginUnproven
    return {
        "operation": operation,
        "causal": causal,
        "request": request,
        "pick_id": pick_id,
        "source_unit_id": source_id,
        "child_unit_id": child_id,
        "source_dimensions": source_dimensions,
        "child_dimensions": child_dimensions,
        "qty": qty,
    }


async def _origin_pick_precedes(session, organization_id, causal, pick_operation_pk, unit_ids, *, historical=None):
    from modules.wms.physical_stock import canonical, physical_unit_frontier

    saved_frontiers = causal.get("prior_frontiers")
    if not isinstance(saved_frontiers, dict):
        raise _OriginUnproven
    for unit_id in unit_ids:
        prior = saved_frontiers.get(unit_id)
        if prior is None:
            raise _OriginUnproven
        prior_tokens = _origin_frontier_tokens(prior)
        _, records = await physical_unit_frontier(session, organization_id, unit_id, historical=historical)
        pick_rows = [row for row in records["ledgers"] if row["operation_pk"] == pick_operation_pk]
        if len(pick_rows) != 1 or canonical(pick_rows[0]["token"]) not in prior_tokens:
            raise _OriginUnproven


async def resolve_current_unit_origin(session, organization_id, warehouse_id, unit_id):
    """Return a deterministic origin proof for a currently positive scoped unit."""
    from modules.wms.models import WarehouseDirectory
    from modules.wms.physical_models import StockUnit
    from modules.wms.physical_stock import _require_physical_causal_snapshot, control, digest

    if not session.in_transaction():
        raise RuntimeError("physical_origin_transaction_required")
    if (
        type(organization_id) is not int or type(warehouse_id) is not int
        or not isinstance(unit_id, str) or not unit_id
    ):
        raise HTTPException(422, "Некорректная область доказательства остатка")
    await _require_physical_causal_snapshot(session)
    with session.no_autoflush:
        directory = await session.scalar(select(WarehouseDirectory).where(
            WarehouseDirectory.id == warehouse_id,
            WarehouseDirectory.organization_id == organization_id,
            WarehouseDirectory.is_active.is_(True),
        ).execution_options(populate_existing=True))
        if directory is None:
            raise HTTPException(404, "Активный склад организации не найден")
        await control(session, warehouse_id, organization_id, lock=True)
        unit = await session.scalar(select(StockUnit).where(
            StockUnit.id == unit_id,
            StockUnit.warehouse_id == warehouse_id,
        ).with_for_update().execution_options(populate_existing=True))
        if unit is None:
            raise HTTPException(404, "Остаток склада не найден")
        try:
            frontier, current_tokens, _ = await _origin_history(session, organization_id, unit_id)
            if Decimal(unit.qty) <= 0:
                raise _OriginUnproven
            proof = await _prove_unit_boundary(
                session, organization_id, unit_id, current_tokens, current_unit=unit,
            )
        except _OriginUnproven:
            unresolved = {
                "status": "origin_unproven",
                "organization_id": organization_id,
                "warehouse_id": warehouse_id,
                "unit_id": unit_id,
                "frontier_digest": digest(frontier) if "frontier" in locals() else None,
            }
            return {**unresolved, "proof_digest": digest(unresolved)}

    current_unit = {
        "unit_id": unit.id,
        "sku_code": unit.sku_code,
        "batch_id": unit.batch_id,
        "packaging_id": unit.packaging_id,
    }
    evidence = {
        "status": "proven",
        "organization_id": organization_id,
        "warehouse_id": warehouse_id,
        "current_unit": current_unit,
        "basis": proof["basis"],
        "root_operation_id": proof["root_operation_id"],
        "current_operation_id": proof["tail_operation_id"],
        "proof_digest": proof["proof_digest"],
    }
    evidence["proof_digest"] = digest({
        "evidence": {key: value for key, value in evidence.items() if key != "proof_digest"},
        "internal_proof_digest": proof["proof_digest"],
    })
    return evidence
