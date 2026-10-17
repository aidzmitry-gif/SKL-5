"""Company-scoped ERP stock and TSD API. A read never confirms unknown stock."""
from datetime import timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from core.domain.models import Sku, User
from core.runtime.deps import get_core, get_session
from core.services.auth import CurrentUser, get_current_user, has_permission, require_permission
from modules.wms.assembly_quality import (
    confirm_process_run,
    prepare_process_run,
    read_process_receipt,
    read_process_run,
)
from modules.wms.models import Location, WarehouseDirectory
from modules.wms.physical_models import (
    CountLocationLock,
    ExchangeJob,
    Packaging,
    PhysicalCount,
    PhysicalCountLine,
    PhysicalHold,
    PhysicalLocation,
    PhysicalOperation,
    PhysicalTask,
    PickedAllocation,
    PickRule,
    ReconciliationSnapshot,
    StockUnit,
    StoragePolicy,
    WarehouseControl,
)
from modules.wms.physical_schemas import (
    AssemblyRunConfirm,
    AssemblyRunPrepare,
    AssemblyStationRegister,
    BatchSetup,
    CapacityCommand,
    ItemIdentityEnsureEnvelope,
    ItemIdentityEnsureRequest,
    ItemLabelPrepareEnvelope,
    ItemLabelPrepareRequest,
    ItemLabelVerifyEnvelope,
    ItemLabelVerifyRequest,
    LocationSetup,
    PackagingSetup,
    PickRuleSetup,
    PolicySetup,
    ReconciliationInput,
    ResponsibleOperationId,
    TransferCommand,
    WarehouseCommand,
    WarehouseResponsibleSet,
    WarehouseSetup,
)
from modules.wms.physical_stock import (
    PhysicalStock,
    _on_hand_units,
    capture_native_erp_selector_authority,
    control,
    decimal_text,
    eligible,
    location,
    native_receiving_source_warehouse,
    native_selector_i32,
    picked_output,
    policy,
    protect_blind_projection,
    protect_blind_receipt,
    public_operation_result,
    responsible_operation_indicator,
    revalidate_native_erp_selector_authority,
    review_count,
    today,
    unit_output,
)
from modules.wms.placement_schemas import PlacementCommand, PlacementProposalQuery
from modules.wms.transfer_loss_protocol import TransferLossCommand, loss_operation_indicator
from modules.wms.warehouse_catalog import scoped

router = APIRouter(prefix="/physical/organizations/{org_id}", tags=["ERP WMS и ТСД"])
directory_router = APIRouter(prefix="/physical", tags=["ERP WMS и ТСД"])








@directory_router.get("/organizations")
async def organizations(session=Depends(get_session), core=Depends(get_core), user=Depends(require_permission("wms.read"))):
    return await core.services.accounting.source_organizations(session, user)


@directory_router.get("/context")
async def context(session=Depends(get_session), core=Depends(get_core), user=Depends(require_permission("wms.read"))):
    from dataclasses import replace

    from core.services.auth import EffectiveIdentityLookupError, resolve_effective_oidc_user

    actor_user_id = None
    try:
        fresh = await resolve_effective_oidc_user(
            replace(user, local_status=None), session,
        )
    except EffectiveIdentityLookupError:
        fresh = None
    if (
        fresh is not None
        and isinstance(fresh.keycloak_user_id, str)
        and fresh.keycloak_user_id == user.keycloak_user_id
        and fresh.local_status == "active"
    ):
        managed = await session.scalar(select(User).where(
            User.keycloak_user_id == fresh.keycloak_user_id,
        ).execution_options(populate_existing=True))
        if (
            managed is not None
            and managed.status == "active"
            and managed.keycloak_user_id == fresh.keycloak_user_id
            and managed.role
            and managed.role in fresh.roles
        ):
            actor_user_id = managed.id
    return {"actor": user.keycloak_user_id or user.username,
        "actor_user_id": actor_user_id,
        "can_operate": has_permission(core, user, "wms.operate"),
        "can_approve": has_permission(core, user, "wms.approve"),
        "can_configure": has_permission(core, user, "wms.configure")}


@router.post("/warehouses/{warehouse_id}/reconciliation")
async def reconciliation(org_id: int, warehouse_id: int, data: ReconciliationInput,
    session=Depends(get_session), core=Depends(get_core), user=Depends(require_permission("wms.approve"))):
    from modules.wms.physical_reconciliation import record_snapshot
    try:
        await scoped(org_id, session, core, user)
        warehouse = await control(session, warehouse_id, org_id)
        if not data.complete:
            raise HTTPException(422, "Частичный снимок не подтверждает полную сверку")
        result = await record_snapshot(session, warehouse, data.snapshot_ref, data.warehouse_ref,
            [row.model_dump(mode="json") for row in data.rows], scope=data.scope)
        await session.commit()
        return result
    except Exception:
        await session.rollback()
        raise


async def transaction(session=Depends(get_session)):
    try:
        yield session
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "Конкурентная операция изменила данные; повторите исходную команду") from exc
    except Exception:
        await session.rollback()
        raise


@directory_router.get("/packing-context")
async def packing_context(session=Depends(get_session), core=Depends(get_core), user=Depends(require_permission("wms.packing.read"))):
    return {"organizations": await core.services.accounting.source_organizations(session, user)}


@router.get("/packing-warehouses")
async def packing_warehouses(org_id: int, session=Depends(get_session), core=Depends(get_core), user=Depends(require_permission("wms.packing.read"))):
    await scoped(org_id, session, core, user)
    rows = (await session.scalars(select(WarehouseDirectory).where(
        WarehouseDirectory.organization_id == org_id, WarehouseDirectory.is_active.is_(True),
    ).order_by(WarehouseDirectory.name, WarehouseDirectory.id))).all()
    return [{"id": row.id, "name": row.name, "title": row.title} for row in rows]


@router.get("/warehouses/{warehouse_id}/packing")
async def packing_for_office(org_id: int, warehouse_id: int, session=Depends(transaction, scope="function"), core=Depends(get_core), user=Depends(require_permission("wms.packing.read"))):
    from modules.wms.physical_packing import office_packing

    await scoped(org_id, session, core, user)
    stock = PhysicalStock(core.services.stock, core.event_bus, services=core.services, user=user, core=core)
    return await office_packing(session, stock, org_id, warehouse_id)


@router.get("/warehouses/{warehouse_id}/delivery-options")
async def delivery_binding_options(org_id: int, warehouse_id: int, document_id: int = Query(gt=0), session=Depends(transaction, scope="function"), core=Depends(get_core), user=Depends(require_permission("wms.operate"))):
    from modules.wms.physical_packing import delivery_options

    await scoped(org_id, session, core, user)
    stock = PhysicalStock(core.services.stock, core.event_bus, services=core.services, user=user, core=core)
    return await delivery_options(session, stock, org_id, warehouse_id, document_id)


@router.get("/receiving-sources")
async def native_receiving_sources(
    org_id: str,
    warehouse_id: str = Query(...),
    before_receipt_id: str | None = Query(None),
    session=Depends(transaction, scope="function"),
    core=Depends(get_core),
    user: CurrentUser = Depends(require_permission("wms.operate")),
):
    # Freeze the caller before the first awaited authorization/source read.
    frozen = capture_native_erp_selector_authority(user)
    organization_id = native_selector_i32(org_id, "organization_id")
    selected_warehouse_id = native_selector_i32(warehouse_id, "warehouse_id")
    before_id = native_selector_i32(before_receipt_id, "before_receipt_id") if before_receipt_id is not None else None
    try:
        await revalidate_native_erp_selector_authority(session, core, frozen, organization_id)
        await native_receiving_source_warehouse(
            session, organization_id, selected_warehouse_id, lock=False,
        )
        await revalidate_native_erp_selector_authority(session, core, frozen, organization_id)
        page = await core.services.procurement_source.native_receipt_selector_page(
            session, organization_id, selected_warehouse_id, before_id,
        )
        await revalidate_native_erp_selector_authority(session, core, frozen, organization_id)
        await protect_blind_projection(session, [selected_warehouse_id])
        await revalidate_native_erp_selector_authority(session, core, frozen, organization_id)
        return {
            "protocol": 1, "organization_id": organization_id,
            "warehouse_id": selected_warehouse_id,
            "owner": {"user_id": frozen[0], "sub": frozen[1]},
            "items": page["items"],
            "next_before_receipt_id": page["next_before_receipt_id"],
        }
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(409, "Источник поступления требует повторной проверки") from exc


@router.get("/receiving-sources/{receipt_id}")
async def native_receiving_source_detail(
    org_id: str,
    receipt_id: str,
    warehouse_id: str = Query(...),
    expected_version: str = Query(...),
    session=Depends(transaction, scope="function"),
    core=Depends(get_core),
    user: CurrentUser = Depends(require_permission("wms.operate")),
):
    frozen = capture_native_erp_selector_authority(user)
    organization_id = native_selector_i32(org_id, "organization_id")
    selected_warehouse_id = native_selector_i32(warehouse_id, "warehouse_id")
    selected_receipt_id = native_selector_i32(receipt_id, "receipt_id")
    version = native_selector_i32(expected_version, "expected_version")
    stock = PhysicalStock(
        core.services.stock, core.event_bus, services=core.services, user=user, core=core,
    )
    try:
        await revalidate_native_erp_selector_authority(session, core, frozen, organization_id)
        await native_receiving_source_warehouse(
            session, organization_id, selected_warehouse_id, lock=False,
        )
        await revalidate_native_erp_selector_authority(session, core, frozen, organization_id)
        source = await core.services.procurement_source.native_receipt_selector_detail(
            session, organization_id, selected_warehouse_id, selected_receipt_id, version,
        )
        await revalidate_native_erp_selector_authority(session, core, frozen, organization_id)
        if source is None:
            raise HTTPException(404, "Источник поступления не найден")
        history = await stock._native_receipt_history(session, organization_id, source["snapshot"])
        await revalidate_native_erp_selector_authority(session, core, frozen, organization_id)
        await native_receiving_source_warehouse(
            session, organization_id, selected_warehouse_id, lock=True,
        )
        await revalidate_native_erp_selector_authority(session, core, frozen, organization_id)
        await protect_blind_projection(session, [selected_warehouse_id])
        await stock._native_receipt_history(
            session, organization_id, source["snapshot"], owners=history["owners"],
        )
        await revalidate_native_erp_selector_authority(session, core, frozen, organization_id)
        return {
            "protocol": 1, "organization_id": organization_id,
            "warehouse_id": selected_warehouse_id,
            "owner": {"user_id": frozen[0], "sub": frozen[1]},
            "receipt_id": selected_receipt_id, "version": version,
            "lines": source["lines"],
        }
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(409, "История источника поступления требует проверки") from exc


@router.get("/warehouses")
async def warehouses(org_id: int, session=Depends(transaction, scope="function"), core=Depends(get_core), user=Depends(require_permission("wms.read"))):
    await scoped(org_id, session, core, user)
    rows = (await session.execute(select(WarehouseDirectory, WarehouseControl).outerjoin(WarehouseControl,
        WarehouseControl.warehouse_id == WarehouseDirectory.id).where(WarehouseDirectory.organization_id == org_id,
        WarehouseDirectory.is_active.is_(True)).order_by(WarehouseDirectory.id))).all()
    return [{"id": row.id, "name": row.name, "title": row.title, "mode": cfg.mode if cfg else "legacy",
        "opening_count_id": cfg.opening_count_id if cfg else None, "onec_ref": cfg.onec_ref if cfg else None} for row, cfg in rows]


@router.put("/warehouses/{warehouse_id}/setup")
async def setup(org_id: int, warehouse_id: int, data: WarehouseSetup, session=Depends(transaction, scope="function"), core=Depends(get_core), user=Depends(require_permission("wms.configure"))):
    await scoped(org_id, session, core, user)
    directory = await session.get(WarehouseDirectory, warehouse_id)
    if directory is None or directory.organization_id != org_id or not directory.is_active:
        raise HTTPException(404, "Склад организации не найден")
    row = await session.get(WarehouseControl, warehouse_id)
    if row:
        if row.onec_ref != data.onec_ref:
            raise HTTPException(409, "Идентификатор учётного склада уже закреплён")
    else:
        row = WarehouseControl(warehouse_id=warehouse_id, organization_id=org_id, onec_ref=data.onec_ref, mode="preparing")
        session.add(row)
    return {"warehouse_id": warehouse_id, "mode": row.mode, "source_of_stock": "erp" if row.mode == "erp" else "not_counted"}


@router.post("/warehouses/{warehouse_id}/locations", status_code=201)
async def setup_location(org_id: int, warehouse_id: int, data: LocationSetup, session=Depends(transaction, scope="function"), core=Depends(get_core), user=Depends(require_permission("wms.configure"))):
    await scoped(org_id, session, core, user)
    cfg = await control(session, warehouse_id, org_id)
    previous = await session.scalar(select(PhysicalLocation).where(PhysicalLocation.warehouse_id == warehouse_id,
        PhysicalLocation.code == data.code))
    if previous:
        if any(getattr(previous, key) != value for key, value in data.model_dump(exclude={"title"}).items()):
            raise HTTPException(409, "Адрес уже зарегистрирован с другими правилами")
        return {"location_id": previous.location_id, **data.model_dump()}
    if cfg.opening_count_id:
        raise HTTPException(409, "Новые адреса после начального пересчёта требуют отдельного согласования")
    if await session.scalar(select(PhysicalCount.id).where(PhysicalCount.warehouse_id == warehouse_id, PhysicalCount.status == "open")):
        raise HTTPException(409, "Состав адресов заморожен на время пересчёта")
    directory = await session.get(WarehouseDirectory, warehouse_id)
    original = Location(warehouse=directory.name, zone=data.kind, code=data.code, title=data.title, is_active=True)
    session.add(original)
    await session.flush()
    row = PhysicalLocation(location_id=original.id, warehouse_id=warehouse_id, **data.model_dump(exclude={"title"}))
    session.add(row)
    return {"location_id": original.id, **data.model_dump()}


@router.put("/warehouses/{warehouse_id}/policies/{sku_code}")
async def setup_policy(org_id: int, warehouse_id: int, sku_code: str, data: PolicySetup, session=Depends(transaction, scope="function"), core=Depends(get_core), user=Depends(require_permission("wms.configure"))):
    await scoped(org_id, session, core, user)
    await control(session, warehouse_id, org_id)
    if sku_code != data.sku_code:
        raise HTTPException(422, "Код товара не совпадает с адресом запроса")
    sku = await session.scalar(select(Sku).where(Sku.code == sku_code))
    if sku is None:
        raise HTTPException(422, "Сначала подтвердите номенклатуру в справочнике ERP")
    if sku.unit != data.base_unit:
        raise HTTPException(422, "Базовая единица должна совпадать с подтверждённым справочником ERP")
    units = (await session.scalars(select(StockUnit).where(StockUnit.warehouse_id == warehouse_id, StockUnit.sku_code == sku_code))).all()
    quantum = Decimal(1).scaleb(-data.precision)
    if any(unit.qty != unit.qty.quantize(quantum) or unit.reserved != unit.reserved.quantize(quantum) for unit in units):
        raise HTTPException(409, "Новая точность не соответствует существующим остаткам")
    row = await session.get(StoragePolicy, (warehouse_id, sku_code))
    if row is None:
        row = StoragePolicy(warehouse_id=warehouse_id, **data.model_dump())
        session.add(row)
    else:
        for key, value in data.model_dump().items():
            setattr(row, key, value)
    return data.model_dump()


@router.post("/batches", status_code=201)
async def setup_batch(org_id: int, data: BatchSetup, session=Depends(transaction, scope="function"), core=Depends(get_core), user=Depends(require_permission("wms.configure"))):
    stock = PhysicalStock(
        core.services.stock, core.event_bus, services=core.services, user=user, core=core,
    )
    seed = stock._caller_seed
    if (
        seed is None
        or type(seed[7]) is not int
        or seed[7] <= 0
        or not isinstance(seed[2], str)
        or not seed[2].strip()
    ):
        raise HTTPException(403, "Требуется управляемая OIDC-учётная запись")

    actor = await scoped(org_id, session, core, user)
    await stock._fresh_capacity_authority(session, org_id, actor, ("wms.configure",))
    sku = await session.scalar(
        select(Sku).where(Sku.code == data.sku_code).with_for_update()
    )
    if sku is None:
        raise HTTPException(404, "Номенклатура ERP не найдена")
    try:
        result = await core.services.stock.register_batch(
            session, org_id, data.model_dump(),
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    await session.flush()
    await stock._fresh_capacity_authority(session, org_id, actor, ("wms.configure",))
    return result


@router.get("/batches")
async def batches(org_id: int, sku_code: str | None = None, session=Depends(transaction, scope="function"), core=Depends(get_core), user=Depends(require_permission("wms.read"))):
    await scoped(org_id, session, core, user)
    return await core.services.stock.list_batches(session, org_id, sku_code)


@router.post("/packaging", status_code=201)
async def setup_packaging(org_id: int, data: PackagingSetup, session=Depends(transaction, scope="function"), core=Depends(get_core), user=Depends(require_permission("wms.configure"))):
    await scoped(org_id, session, core, user)
    from modules.wms.item_identity import reject_reserved_packaging_barcode

    reject_reserved_packaging_barcode(data.barcode)
    sku = await session.scalar(select(Sku).where(Sku.code == data.sku_code, Sku.is_active.is_(True)))
    if sku is None:
        raise HTTPException(422, "Упаковка требует подтверждённый активный товар ERP")
    previous = await session.scalar(select(Packaging).where(Packaging.organization_id == org_id, Packaging.barcode == data.barcode))
    if previous:
        if any(getattr(previous, key) != value for key, value in data.model_dump().items()):
            raise HTTPException(409, "Штрихкод уже обозначает другую упаковку")
        return {"id": previous.id, **data.model_dump()}
    row = Packaging(organization_id=org_id, **data.model_dump())
    session.add(row)
    await session.flush()
    return {"id": row.id, **data.model_dump()}


@router.put("/warehouses/{warehouse_id}/pick-rules/{location_id}")
async def setup_pick_rule(org_id: int, warehouse_id: int, location_id: int, data: PickRuleSetup, session=Depends(transaction, scope="function"), core=Depends(get_core), user=Depends(require_permission("wms.configure"))):
    await scoped(org_id, session, core, user)
    await control(session, warehouse_id, org_id)
    place = await location(session, warehouse_id, location_id)
    await policy(session, warehouse_id, data.sku_code, data.max_qty)
    if location_id != data.location_id or place.kind != "pick" or place.capacity_sku != data.sku_code or place.capacity_qty is None or data.max_qty > place.capacity_qty:
        raise HTTPException(422, "Min/Max должен соответствовать вместимости места отбора")
    row = await session.scalar(select(PickRule).where(PickRule.warehouse_id == warehouse_id,
        PickRule.location_id == location_id, PickRule.sku_code == data.sku_code))
    if row is None:
        row = PickRule(warehouse_id=warehouse_id, **data.model_dump())
        session.add(row)
    else:
        row.min_qty, row.max_qty = data.min_qty, data.max_qty
    return data.model_dump()


@router.post("/commands")
async def command(org_id: int, data: WarehouseCommand | TransferCommand, session=Depends(transaction, scope="function"), core=Depends(get_core), user=Depends(require_permission("wms.operate"))):
    approvals = {"count_approve", "count_cancel", "cutover", "quality", "maintenance", "adopt_reservation", "assembly_qc"}
    if (data.kind in approvals or getattr(data, "correction_of", None)) and not has_permission(core, user, "wms.approve"):
        raise HTTPException(403, "Для допуска товара и корректировок требуется ответственный склада")
    stock = PhysicalStock(core.services.stock, core.event_bus, services=core.services,
        user=user, core=core)
    actor = await scoped(org_id, session, core, user)
    return await stock.execute(session, org_id, data, actor)


@router.get("/operations/{operation_id}")
async def operation(org_id: int, operation_id: UUID, session=Depends(transaction, scope="function"), core=Depends(get_core), user=Depends(require_permission("wms.read"))):
    stock = PhysicalStock(core.services.stock, core.event_bus, services=core.services, user=user, core=core)
    actor = await scoped(org_id, session, core, user)
    row = await session.scalar(select(PhysicalOperation).where(PhysicalOperation.organization_id == org_id,
        PhysicalOperation.operation_id == str(operation_id)))
    if row is None:
        raise HTTPException(404, "Операция не зарегистрирована")
    from modules.wms.item_identity import ITEM_IDENTITY_METADATA_KINDS
    from modules.wms.physical_capacity import (
        equipment_operation_indicator,
        measurement_operation_indicator,
    )
    from modules.wms.placement_models import work_operation_indicator, placement_operation_indicator

    if placement_operation_indicator(row.kind, row.result) or loss_operation_indicator(row.kind, row.result) or work_operation_indicator(row.kind, row.result) or measurement_operation_indicator(row.kind, row.result) or equipment_operation_indicator(row.kind, row.result) or row.kind == "assembly_station_register" or row.kind in ITEM_IDENTITY_METADATA_KINDS or responsible_operation_indicator(row.kind, row.result):
        raise HTTPException(404, "Операция не зарегистрирована")
    await stock._native_replay_authority(session, org_id, actor, row)
    await protect_blind_receipt(session, row)
    await stock._native_replay_authority(session, org_id, actor, row)
    return public_operation_result(row.result, operation_kind=row.kind)


@router.get("/warehouses/{warehouse_id}/state")
async def state(org_id: int, warehouse_id: int, session=Depends(transaction, scope="function"), core=Depends(get_core), user=Depends(require_permission("wms.read"))):
    await scoped(org_id, session, core, user)
    from modules.wms.physical_packing import task_deliveries
    stock = PhysicalStock(core.services.stock, core.event_bus, services=core.services, user=user, core=core)
    await control(session, warehouse_id, org_id, lock=False)
    initial_tasks = (await session.scalars(select(PhysicalTask).where(PhysicalTask.warehouse_id == warehouse_id,
        PhysicalTask.status.in_(["open", "claimed"]), PhysicalTask.kind == "pick"))).all()
    deliveries = await task_deliveries(session, stock, org_id, warehouse_id, initial_tasks)
    cfg = await control(session, warehouse_id, org_id)
    places = (await session.scalars(select(PhysicalLocation).where(PhysicalLocation.warehouse_id == warehouse_id).order_by(PhysicalLocation.code))).all()
    units = (await session.scalars(select(StockUnit).where(StockUnit.warehouse_id == warehouse_id, StockUnit.qty > 0).order_by(StockUnit.id))).all()
    units = _on_hand_units(units)
    locked = set((await session.scalars(select(CountLocationLock.location_id)
        .join(PhysicalLocation, CountLocationLock.location_id == PhysicalLocation.location_id)
        .where(PhysicalLocation.warehouse_id == warehouse_id))).all())
    # Blind counts also hide stock through the normal terminal, tasks and alerts.
    units = [unit for unit in units if unit.location_id not in locked]
    result, alerts = [], []
    for unit in units:
        reason, lot = await eligible(session, core.services.stock, cfg, unit)
        sku = await session.scalar(select(Sku).where(Sku.code == unit.sku_code))
        packaging = await session.get(Packaging, unit.packaging_id) if unit.packaging_id else None
        result.append({**unit_output(unit, reason), "lot": lot, "location_scan": f"LOC-{unit.location_id}",
            "item_scan": f"ERP-P-{packaging.id}" if packaging else f"SKU-{sku.id}",
            "package_factor": decimal_text(packaging.factor) if packaging else "1"})
        if reason:
            alerts.append({"kind": reason, "unit_id": unit.id, "sku_code": unit.sku_code, "qty": decimal_text(unit.qty)})
    tasks = (await session.scalars(select(PhysicalTask).where(PhysicalTask.warehouse_id == warehouse_id,
        PhysicalTask.status.in_(["open", "claimed"]), PhysicalTask.kind != "cap_place").order_by(PhysicalTask.due_at.asc().nullslast(), PhysicalTask.created_at, PhysicalTask.id))).all()
    visible_ids = {unit.id for unit in units}
    tasks = [task for task in tasks if not task.unit_id or task.unit_id in visible_ids]
    # Stable sort retains original SQL due_at, created_at, id within each group.
    tasks.sort(key=lambda task: not deliveries.get(task.id, {}).get("priority", False))
    counts = (await session.scalars(select(PhysicalCount).where(PhysicalCount.warehouse_id == warehouse_id).order_by(PhysicalCount.created_at.desc()))).all()
    jobs = (await session.scalars(select(ExchangeJob).join(PhysicalOperation).where(
        PhysicalOperation.warehouse_id == warehouse_id).order_by(ExchangeJob.id))).all()
    for job in jobs:
        if job.status != "done":
            alerts.append({"kind": "exchange_" + job.status, "operation_id": job.operation_id, "reason": job.error})
    clone_type, original_type = aliased(StockUnit), aliased(StockUnit)
    picked_rows = (await session.execute(
        select(PickedAllocation, PhysicalHold, clone_type, original_type)
        .join(PhysicalHold, PickedAllocation.hold_id == PhysicalHold.id)
        .outerjoin(clone_type, PickedAllocation.unit_id == clone_type.id)
        .outerjoin(original_type, PhysicalHold.unit_id == original_type.id)
        .where(PhysicalHold.warehouse_id == warehouse_id,
            PickedAllocation.canceled.is_(False), PickedAllocation.shipped < PickedAllocation.qty)
        .order_by(PickedAllocation.id)
        .execution_options(populate_existing=True)
    )).all()
    picks = []
    for pick, hold, clone, original in picked_rows:
        if clone is None or original is None:
            raise HTTPException(409, "Подобранная единица или исходная партия не найдена")
        if clone.location_id in locked or (original.location_id in locked):
            continue
        picked = await picked_output(session, core.services.stock, cfg, pick, hold, clone, original)
        picked["binding_revision_id"] = pick.binding_revision_id
        picks.append(picked)
    if cfg.mode != "erp":
        alerts.append({"kind": "opening_balance_unknown" if not cfg.opening_count_id else "cutover_pending"})
    policies = (await session.execute(
        select(StoragePolicy, Sku).outerjoin(
            Sku,
            (Sku.code == StoragePolicy.sku_code) & Sku.is_active.is_(True),
        ).where(StoragePolicy.warehouse_id == warehouse_id)
    )).all()
    packages = (await session.scalars(select(Packaging).where(Packaging.organization_id == org_id))).all()
    for rule, _sku in policies:
        cadence = {"A": 7, "B": 30, "C": 90}[rule.count_class]
        relevant = [u for u in units if u.sku_code == rule.sku_code]
        completed = [c for c in counts if c.status == "approved" and all(u.location_id in c.locations for u in relevant)]
        last = completed[0].approved_at if completed else None
        if relevant and (last is None or last.date() + timedelta(days=cadence) <= today()):
            alerts.append({"kind": "cycle_count_due", "sku_code": rule.sku_code, "cadence_days": cadence,
                "location_ids": sorted({u.location_id for u in relevant})})
    snapshots = (await session.scalars(select(ReconciliationSnapshot).where(ReconciliationSnapshot.warehouse_id == warehouse_id)
        .order_by(ReconciliationSnapshot.id.desc()).limit(1))).all()
    return {"warehouse_id": warehouse_id, "mode": cfg.mode, "source": "erp" if cfg.mode == "erp" else "unknown",
        "opening_count_id": cfg.opening_count_id,
        "locations": [{"id": p.location_id, "code": p.code, "kind": p.kind, "count_locked": p.location_id in locked,
            "capacity_qty": decimal_text(p.capacity_qty) if p.capacity_qty else None} for p in places],
        "units": result, "alerts": alerts,
        "policies": [{"sku_code": p.sku_code, "sku_id": sku.id if sku else None,
            "sku_title": sku.title if sku else None, "sku_unit": sku.unit if sku else None,
            "base_unit": p.base_unit, "precision": p.precision,
            "expiry_required": p.expiry_required, "manufacture_required": p.manufacture_required,
            "min_shelf_days": p.min_shelf_days, "max_age_days": p.max_age_days, "maintenance_days": p.maintenance_days,
            "manufacturer_source": p.manufacturer_source, "storage_rule": p.storage_rule,
            "warranty_rule": p.warranty_rule, "count_class": p.count_class} for p, sku in policies],
        "packaging": [{"id": p.id, "sku_code": p.sku_code, "barcode": p.barcode, "factor": decimal_text(p.factor),
            "title": p.title, "sealed_allowed": p.sealed_allowed} for p in packages],
        "picks": picks,
        "tasks": [{"delivery": deliveries.get(t.id), "id": t.id, "kind": t.kind, "status": t.status, "source": t.source,
            "unit_id": t.unit_id, "hold_id": t.hold_id, "qty": decimal_text(t.qty), "destination_id": t.destination_id,
            "assignee": t.assignee, "due_at": (deliveries[t.id]["cutoff_at"] if t.id in deliveries else (t.due_at.isoformat() if t.due_at else None)),
            "container": (await session.get(PhysicalHold, t.hold_id)).container if t.hold_id else None} for t in tasks],
        "counts": [{"id": c.id, "kind": c.kind, "status": c.status, "locations": c.locations,
            "completed_locations": c.completed_locations} for c in counts],
        "exchange": [{"operation_id": j.operation_id, "status": j.status, "document_ref": j.document_ref,
            "document_state": j.document_state, "error": j.error, "attempts": j.attempts} for j in jobs],
        "reconciliation": None if locked or not snapshots else {"snapshot_ref": snapshots[0].snapshot_ref,
            "differences": snapshots[0].differences}}


@router.get("/counts/{count_id}")
async def count_view(org_id: int, count_id: UUID, session=Depends(transaction, scope="function"), core=Depends(get_core), user=Depends(require_permission("wms.operate"))):
    await scoped(org_id, session, core, user)
    count = await session.get(PhysicalCount, str(count_id))
    if count is None:
        raise HTTPException(404, "Пересчёт не найден")
    await control(session, count.warehouse_id, org_id)
    await protect_blind_projection(session, [count.warehouse_id],
        own_count_id=count.id if count.status == "open" else None)
    lines = (await session.scalars(select(PhysicalCountLine).where(PhysicalCountLine.count_id == count.id))).all()
    # Neither the expected snapshot nor a difference is returned to the counter.
    return {"id": count.id, "status": count.status, "locations": count.locations,
        "identities": [{"unit_id": key, **entry["dimensions"]} for key, entry in count.expected.items()],
        "completed_locations": count.completed_locations, "lines": [{"id": line.id, "unit_id": line.unit_id,
            **line.dimensions, "qty": decimal_text(line.qty) if line.qty is not None else None} for line in lines]}


@router.get("/counts/{count_id}/review")
async def count_review(org_id: int, count_id: UUID, session=Depends(transaction, scope="function"), core=Depends(get_core), user=Depends(require_permission("wms.approve"))):
    await scoped(org_id, session, core, user)
    count = await session.get(PhysicalCount, str(count_id))
    if count is None:
        raise HTTPException(404, "Пересчёт не найден")
    await control(session, count.warehouse_id, org_id)
    return await review_count(session, count)


@router.get("/scan")
async def scan(org_id: int, value: str = Query(min_length=1, max_length=256),
    warehouse_id: int | None = Query(default=None, gt=0),
    session=Depends(transaction, scope="function"), core=Depends(get_core), user=Depends(require_permission("wms.operate"))):
    await scoped(org_id, session, core, user)
    from modules.wms.item_identity import resolve_item_scan

    return await resolve_item_scan(session, org_id, value, warehouse_id=warehouse_id)


async def capacity_transaction(session=Depends(get_session)):
    # get_session yields a new AsyncSession with no begin/commit policy.
    # The new metadata route owns explicit root begin and completion.
    try:
        async with session.begin():
            yield session
            from modules.wms.physical_models import cap_finish_equipment_root
            await cap_finish_equipment_root(session)
            from modules.wms.placement_models import cap_finish_work_root
            await cap_finish_work_root(session)
            from modules.wms.placement_models import cap_finish_placement_root
            await cap_finish_placement_root(session)
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            409, "Конкурентная операция изменила данные; повторите исходную команду",
        ) from exc
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(409, str(exc)) from exc
    except Exception:
        await session.rollback()
        raise


@router.get("/warehouses/{warehouse_id}/responsible")
async def warehouse_responsibility_route(
    org_id: str, warehouse_id: str,
    session=Depends(capacity_transaction, scope="function"), core=Depends(get_core),
    user=Depends(require_permission("wms.configure")),
):
    stock = PhysicalStock(core.services.stock, core.event_bus, services=core.services, user=user, core=core)
    organization_id = native_selector_i32(org_id, "organization_id")
    selected_warehouse = native_selector_i32(warehouse_id, "warehouse_id")
    actor = await scoped(organization_id, session, core, user)
    return await stock.warehouse_responsibility(session, organization_id, selected_warehouse, actor)


@router.put("/warehouses/{warehouse_id}/responsible")
async def set_warehouse_responsibility_route(
    org_id: str, warehouse_id: str, data: WarehouseResponsibleSet,
    session=Depends(capacity_transaction, scope="function"), core=Depends(get_core),
    user=Depends(require_permission("wms.configure")),
):
    data = WarehouseResponsibleSet.model_validate(data.model_dump(mode="json"))
    stock = PhysicalStock(core.services.stock, core.event_bus, services=core.services, user=user, core=core)
    organization_id = native_selector_i32(org_id, "organization_id")
    selected_warehouse = native_selector_i32(warehouse_id, "warehouse_id")
    actor = await scoped(organization_id, session, core, user)
    return await stock.set_warehouse_responsibility(session, organization_id, selected_warehouse, data, actor)


@router.post("/warehouses/{warehouse_id}/assembly-stations", status_code=201)
async def register_assembly_station_route(
    org_id: int,
    warehouse_id: int,
    data: AssemblyStationRegister,
    session=Depends(capacity_transaction, scope="function"),
    core=Depends(get_core),
    user=Depends(require_permission("wms.configure")),
):
    stock = PhysicalStock(
        core.services.stock, core.event_bus, services=core.services,
        user=user, core=core,
    )
    if (
        type(user.local_user_id) is not int or user.local_user_id <= 0
        or not isinstance(user.keycloak_user_id, str)
        or not user.keycloak_user_id.strip()
    ):
        raise HTTPException(403, "Требуется управляемая OIDC-учётная запись")
    actor = await scoped(org_id, session, core, user)
    return await stock.register_assembly_station(
        session, org_id, warehouse_id, data, actor,
    )


@router.post(
    "/item-identities/ensure",
    status_code=201,
    response_model=ItemIdentityEnsureEnvelope,
)
async def ensure_item_identity_route(
    org_id: int,
    data: ItemIdentityEnsureRequest,
    session=Depends(capacity_transaction, scope="function"),
    core=Depends(get_core),
    user=Depends(require_permission("wms.operate")),
):
    from modules.wms.item_identity import ensure_item_identity

    return await ensure_item_identity(session, core, org_id, data, user)


@router.post(
    "/item-labels/prepare",
    status_code=201,
    response_model=ItemLabelPrepareEnvelope,
)
async def prepare_item_label_route(
    org_id: int,
    data: ItemLabelPrepareRequest,
    session=Depends(capacity_transaction, scope="function"),
    core=Depends(get_core),
    user=Depends(require_permission("wms.operate")),
):
    from modules.wms.item_identity import prepare_item_label

    return await prepare_item_label(session, core, org_id, data, user)


@router.post(
    "/item-labels/verify",
    status_code=201,
    response_model=ItemLabelVerifyEnvelope,
)
async def verify_item_label_route(
    org_id: int,
    data: ItemLabelVerifyRequest,
    session=Depends(capacity_transaction, scope="function"),
    core=Depends(get_core),
    user=Depends(require_permission("wms.operate")),
):
    from modules.wms.item_identity import verify_item_label

    return await verify_item_label(session, core, org_id, data, user)


@router.post("/capacity/commands")
async def capacity_command(
    org_id: int,
    data: CapacityCommand,
    session=Depends(capacity_transaction, scope="function"),
    core=Depends(get_core),
    user=Depends(require_permission("wms.operate")),
):
    stock = PhysicalStock(
        core.services.stock,
        core.event_bus,
        services=core.services,
        user=user,
        core=core,
    )
    actor = await scoped(org_id, session, core, user)
    return await stock.execute_capacity(session, org_id, data, actor)


@router.post("/placement/commands")
async def placement_command(
    org_id: int, data: PlacementCommand,
    session=Depends(capacity_transaction, scope="function"), core=Depends(get_core),
    user=Depends(require_permission("wms.operate")),
):
    stock = PhysicalStock(core.services.stock, core.event_bus, services=core.services, user=user, core=core)
    actor = await scoped(org_id, session, core, user)
    try:
        return await stock.execute_placement(session, org_id, data, actor)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/placement/proposals")
async def placement_proposal(
    org_id: int, data: PlacementProposalQuery,
    session=Depends(capacity_transaction, scope="function"), core=Depends(get_core),
    user=Depends(require_permission("wms.operate")),
):
    stock = PhysicalStock(core.services.stock, core.event_bus, services=core.services, user=user, core=core)
    actor = await scoped(org_id, session, core, user)
    try:
        return await stock.propose_placement(session, org_id, data, actor)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/orders/{order_id}/assembly-process-run")
async def assembly_process_run(
    org_id: int = Path(gt=0, le=2147483647),
    order_id: int = Path(gt=0, le=2147483647),
    warehouse_id: int | None = Query(default=None, gt=0, le=2147483647),
    view: Literal["h11"] | None = Query(default=None),
    session=Depends(transaction, scope="function"),
    core=Depends(get_core),
    user=Depends(get_current_user),
    response: Response = None,
):
    if view == "h11":
        response.headers["Cache-Control"] = "no-store"
    return await read_process_run(
        session, core, org_id, order_id, user, warehouse_id=warehouse_id, view=view,
    )


@router.get("/orders/{order_id}/assembly-outputs")
async def assembly_outputs(
    org_id: int = Path(gt=0, le=2147483647),
    order_id: int = Path(gt=0, le=2147483647),
    warehouse_id: int = Query(gt=0, le=2147483647),
    session=Depends(transaction, scope="function"), core=Depends(get_core),
    user=Depends(get_current_user), response: Response = None,
):
    from modules.wms.assembly_workplace import read_assembly_outputs

    response.headers["Cache-Control"] = "no-store"
    return await read_assembly_outputs(session, core, org_id, order_id, warehouse_id, user)


@router.post("/orders/{order_id}/assembly-process-run/prepare", status_code=201)
async def prepare_assembly_process_run(
    org_id: int,
    order_id: int,
    data: AssemblyRunPrepare,
    session=Depends(transaction, scope="function"),
    core=Depends(get_core),
    user=Depends(get_current_user),
):
    return await prepare_process_run(session, core, org_id, order_id, user, data)


@router.post("/orders/{order_id}/assembly-process-run/confirm", status_code=201)
async def confirm_assembly_process_run(
    org_id: int,
    order_id: int,
    data: AssemblyRunConfirm,
    session=Depends(transaction, scope="function"),
    core=Depends(get_core),
    user=Depends(get_current_user),
):
    return await confirm_process_run(session, core, org_id, order_id, user, data)


@router.get("/orders/{order_id}/assembly-process-run/requests/{request_id}")
async def assembly_process_request_receipt(
    org_id: int,
    order_id: int,
    request_id: UUID,
    session=Depends(transaction, scope="function"),
    core=Depends(get_core),
    user=Depends(get_current_user),
):
    return await read_process_receipt(
        session, core, org_id, order_id, request_id, user,
    )



@router.post("/warehouses/{warehouse_id}/transfer-discrepancies/commands")
async def transfer_loss_command_route(
    org_id: str, warehouse_id: str, data: TransferLossCommand,
    session=Depends(capacity_transaction, scope="function"), core=Depends(get_core),
    user=Depends(require_permission("wms.operate")),
):
    from pydantic import TypeAdapter

    from modules.wms.transfer_loss_service import execute_loss

    # Freeze the original validated body before membership awaits.
    data = TypeAdapter(TransferLossCommand).validate_python(data.model_dump(mode="json"))
    organization_id = native_selector_i32(org_id, "organization_id")
    selected_warehouse = native_selector_i32(warehouse_id, "warehouse_id")
    if data.warehouse_id != selected_warehouse:
        raise HTTPException(422, "Склад команды не совпадает с выбранным складом")
    stock = PhysicalStock(core.services.stock, core.event_bus, services=core.services,
        user=user, core=core)
    actor = await scoped(organization_id, session, core, user)
    return await execute_loss(stock, session, organization_id, data, actor)


@router.get("/warehouses/{warehouse_id}/transfer-discrepancies/operations/{operation_id}")
async def transfer_loss_receipt_route(
    org_id: str, warehouse_id: str, operation_id: ResponsibleOperationId,
    session=Depends(capacity_transaction, scope="function"), core=Depends(get_core),
    user=Depends(require_permission("wms.read")),
):
    from modules.wms.transfer_loss_service import read_loss

    organization_id = native_selector_i32(org_id, "organization_id")
    selected_warehouse = native_selector_i32(warehouse_id, "warehouse_id")
    stock = PhysicalStock(core.services.stock, core.event_bus, services=core.services,
        user=user, core=core)
    actor = await scoped(organization_id, session, core, user)
    return await read_loss(stock, session, organization_id, selected_warehouse, str(operation_id), actor)


@router.get("/warehouses/{warehouse_id}/transfer-discrepancies")
async def transfer_loss_status_route(
    org_id: str, warehouse_id: str,
    session=Depends(capacity_transaction, scope="function"), core=Depends(get_core),
    user=Depends(require_permission("wms.read")),
):
    from modules.wms.transfer_loss_service import list_loss

    organization_id = native_selector_i32(org_id, "organization_id")
    selected_warehouse = native_selector_i32(warehouse_id, "warehouse_id")
    stock = PhysicalStock(core.services.stock, core.event_bus, services=core.services,
        user=user, core=core)
    actor = await scoped(organization_id, session, core, user)
    return await list_loss(stock, session, organization_id, selected_warehouse, actor)
