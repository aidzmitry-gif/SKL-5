import json
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Numeric, select
from sqlalchemy.orm import Session

from modules.wms.physical_models import (
    STOCK_UNIT_MEASUREMENT_V1_COLUMNS,
    CarrierPosition,
    PhysicalCarrier,
    PhysicalCount,
    PhysicalCountLine,
    PhysicalLedger,
    PhysicalOperation,
    PlacementProfile,
    StockUnit,
    _cap_current_binding,
    _cap_fixed,
    _cap_timestamp_text,
    require_current_in_transit,
    require_current_on_hand,
)


def profile_payload(profile, slots):
    from modules.wms.physical_models import (
        _CAP_PROFILE_OMIT,
        PlacementProfile,
        PlacementSlot,
        _cap_profile_json,
        _cap_slot_json,
    )

    values = {
        column.name: getattr(profile, column.name)
        for column in PlacementProfile.__table__.columns
        if column.name not in _CAP_PROFILE_OMIT
    }
    children = [
        _cap_slot_json({
            column.name: getattr(slot, column.name)
            for column in PlacementSlot.__table__.columns
        })
        for slot in slots
    ]
    return {
        "profile": _cap_profile_json(values),
        "slots": sorted(children, key=lambda row: row["slot_key"]),
    }


def check_profile_snapshot(profile, slots):
    from modules.wms.physical_stock import canonical, digest

    expected = profile_payload(profile, slots)
    supplied = json.loads(profile.canonical_payload)
    if (
        profile.canonical_payload != canonical(expected)
        or supplied != expected
        or profile.digest != digest(expected)
    ):
        raise ValueError("capacity_profile_snapshot_mismatch")
def require_capacity_read_committed(connection):
    dialect = connection.dialect.name
    if dialect == "sqlite":
        # Explicit local ORM/FK harness adapter; not PostgreSQL isolation proof.
        return
    if dialect != "postgresql":
        raise ValueError("capacity_database_dialect_unsupported")

    if not connection.in_transaction():
        raise ValueError("capacity_transaction_required")

    dbapi = connection.connection.dbapi_connection
    if dbapi is None:
        raise ValueError("capacity_dbapi_connection_unavailable")
    try:
        autocommit = dbapi.autocommit
    except AttributeError as exc:
        raise ValueError("capacity_autocommit_state_unavailable") from exc
    if type(autocommit) is not bool:
        raise ValueError("capacity_autocommit_state_unavailable")
    if autocommit:
        raise ValueError("capacity_autocommit_unsupported")

    # Live query on this exact Connection; not engine defaults or cached options.
    if connection.get_isolation_level() != "READ COMMITTED":
        raise ValueError("capacity_read_committed_required")


async def prelock_count_carriers(session, warehouse_id, lines):
    from sqlalchemy import select

    from modules.wms.physical_models import (
        CarrierPosition,
        LoadResource,
        PhysicalCarrier,
        PhysicalLocation,
        PlacementProfile,
        PlacementSlot,
        StockUnit,
    )
    from modules.wms.physical_stock import reject

    observed = {line.unit_id: line.qty for line in lines if line.unit_id}
    if not observed:
        return {}, {}

    with session.no_autoflush:
        rows = (await session.scalars(
            select(StockUnit).where(
                StockUnit.warehouse_id == warehouse_id,
                StockUnit.id.in_(sorted(observed)),
            ).order_by(StockUnit.warehouse_id, StockUnit.id)
            .execution_options(populate_existing=True)
        )).all()
        if {row.id for row in rows} != set(observed):
            reject("Пересчитываемый остаток отсутствует в выбранном складе")
        candidates = {
            row.carrier_id for row in rows
            if row.carrier_id is not None and observed[row.id] != row.qty
        }
        carriers = {}
        if candidates:
            connection = await session.connection()
            await connection.run_sync(require_capacity_read_committed)
            # WarehouseControl is already held by execute. Lock the complete
            # warehouse capacity footprint before any counted StockUnit lock.
            for model, ordering in (
                (LoadResource, (LoadResource.id,)),
                (PhysicalLocation, (PhysicalLocation.location_id,)),
                (PlacementProfile, (PlacementProfile.id,)),
                (PlacementSlot, (
                    PlacementSlot.location_id, PlacementSlot.profile_id,
                    PlacementSlot.slot_key,
                )),
            ):
                (await session.scalars(
                    select(model).where(model.warehouse_id == warehouse_id)
                    .order_by(*ordering).with_for_update()
                    .execution_options(populate_existing=True)
                )).all()
            carrier_rows = (await session.scalars(
                select(PhysicalCarrier).where(
                    PhysicalCarrier.warehouse_id == warehouse_id,
                ).order_by(PhysicalCarrier.id).with_for_update()
                .execution_options(populate_existing=True)
            )).all()
            carriers = {row.id: row for row in carrier_rows}
            (await session.scalars(
                select(CarrierPosition).where(
                    CarrierPosition.warehouse_id == warehouse_id,
                ).order_by(CarrierPosition.carrier_id, CarrierPosition.ordinal)
                .with_for_update().execution_options(populate_existing=True)
            )).all()
            if not candidates <= set(carriers):
                reject("Связь пересчитываемого остатка с носителем не подтверждена")

        rows = (await session.scalars(
            select(StockUnit).where(
                StockUnit.warehouse_id == warehouse_id,
                StockUnit.id.in_(sorted(observed)),
            ).order_by(StockUnit.warehouse_id, StockUnit.id).with_for_update()
            .execution_options(populate_existing=True)
        )).all()
        if {row.id for row in rows} != set(observed):
            reject("Пересчитываемый остаток отсутствует в выбранном складе")
        changed = {
            row.carrier_id for row in rows
            if row.carrier_id is not None and observed[row.id] != row.qty
        }
        if changed != candidates:
            reject("Состав носителей изменился; повторите проверку пересчёта")
        if any(carriers[key].content_revision >= 2147483647 for key in changed):
            reject("Версия состава носителя исчерпана")
        return (
            {row.id: row for row in rows},
            {key: carriers[key] for key in sorted(changed)},
        )


_CAPACITY_MEASUREMENT_FIELDS = frozenset((
    "length_mm", "width_mm", "height_mm",
    "opening_width_mm", "opening_height_mm",
    "clearance_mm", "gross_limit_kg", "tare_kg", "top_load_limit_kg",
    "gross_kg", "x_mm", "y_mm", "z_mm", "depth_mm",
))


def capacity_request_snapshot(data):
    request = data.model_dump(mode="json")
    for name in _CAPACITY_MEASUREMENT_FIELDS:
        if name in request and request[name] is not None:
            request[name] = _cap_fixed(request[name])
    if data.kind == "capacity_profile_approve":
        for slot in request["slots"]:
            for name in (
                "x_mm", "y_mm", "z_mm", "width_mm", "height_mm", "depth_mm",
            ):
                slot[name] = _cap_fixed(slot[name])
    return request


async def capacity_metadata_rows(session, warehouse_id):
    from modules.wms.physical_models import (
        LoadResource,
        PhysicalLocation,
        PlacementProfile,
        PlacementSlot,
    )
    connection = await session.connection()
    await connection.run_sync(require_capacity_read_committed)
    resources = (await session.scalars(
        select(LoadResource).where(LoadResource.warehouse_id == warehouse_id)
        .order_by(LoadResource.id).with_for_update()
        .execution_options(populate_existing=True)
    )).all()
    places = (await session.scalars(
        select(PhysicalLocation).where(PhysicalLocation.warehouse_id == warehouse_id)
        .order_by(PhysicalLocation.location_id).with_for_update()
        .execution_options(populate_existing=True)
    )).all()
    profiles = (await session.scalars(
        select(PlacementProfile).where(PlacementProfile.warehouse_id == warehouse_id)
        .order_by(PlacementProfile.id).with_for_update()
        .execution_options(populate_existing=True)
    )).all()
    slots = (await session.scalars(
        select(PlacementSlot).where(PlacementSlot.warehouse_id == warehouse_id)
        .order_by(PlacementSlot.location_id, PlacementSlot.profile_id, PlacementSlot.slot_key)
        .with_for_update().execution_options(populate_existing=True)
    )).all()
    return resources, places, profiles, slots


async def confirm_resource(session, warehouse, data, actor):
    from modules.wms.physical_models import LoadResource, _cap_chain
    from modules.wms.physical_stock import location, reject

    resources, places, _, _ = await capacity_metadata_rows(session, warehouse.warehouse_id)
    by_id = {row.id: row for row in resources}
    by_location = {row.location_id: row for row in places}
    resource_id = str(data.resource_id)
    parent_id = str(data.parent_id) if data.parent_id else None
    row = by_id.get(resource_id)
    global_row = await session.get(LoadResource, resource_id)
    if global_row is not None and global_row.warehouse_id != warehouse.warehouse_id:
        reject("Идентификатор ресурса принадлежит другому складу")
    revision = row.topology_revision if row is not None else 0
    if revision != data.expected_revision or revision >= 2147483647:
        reject("Версия ресурса изменилась или исчерпана")
    if row is not None and row.kind != data.resource_kind:
        reject("Тип существующего ресурса неизменяем")
    if parent_id is not None:
        if parent_id not in by_id:
            reject("Предок не найден в этом складе")
        connection = await session.connection()
        chain = await connection.run_sync(lambda conn: _cap_chain(conn, warehouse.warehouse_id, parent_id))
        if any(node["resource_id"] == resource_id for node in chain):
            reject("Изменение создаёт цикл ресурсов")
    mappings = []
    for item in data.location_mappings:
        place = by_location.get(item.location_id)
        if place is None:
            reject("Ячейка не найдена в этом складе")
        await location(session, warehouse.warehouse_id, item.location_id)
        if place.resource_mapping_revision != item.expected_mapping_revision:
            reject("Mapping revision ячейки изменилась")
        changed = place.resource_id != resource_id
        if changed and place.resource_mapping_revision >= 2147483647:
            reject("Mapping revision исчерпана")
        mappings.append((place, changed))
    evidence = {
        "source_ref": data.source_ref, "source_revision": data.source_revision,
        "evidence": data.evidence, "approved_by": actor,
        "approved_at": datetime.now(timezone.utc),
    }
    if row is None:
        row = LoadResource(id=resource_id, warehouse_id=warehouse.warehouse_id,
            kind=data.resource_kind, parent_id=parent_id, topology_revision=1, **evidence)
        session.add(row)
    else:
        row.parent_id = parent_id
        row.topology_revision = revision + 1
        for name, value in evidence.items():
            setattr(row, name, value)
    await session.flush()
    for place, changed in mappings:
        if changed:
            place.resource_id = resource_id
            place.resource_mapping_revision += 1
    await session.flush()
    return {
        "resource_id": row.id, "topology_revision": row.topology_revision,
        "location_mappings": [{"location_id": place.location_id,
            "resource_mapping_revision": place.resource_mapping_revision} for place, _ in mappings],
    }


async def approve_profile(session, warehouse, data, actor):
    from modules.wms.physical_models import Packaging, PlacementProfile, PlacementSlot, _cap_chain
    from modules.wms.physical_stock import canonical, digest, location, reject

    resources, places, profiles, _ = await capacity_metadata_rows(session, warehouse.warehouse_id)
    by_resource = {row.id: row for row in resources}
    by_location = {row.location_id: row for row in places}
    profile_id = str(data.profile_id)
    if await session.get(PlacementProfile, profile_id) is not None:
        reject("Идентификатор immutable profile уже использован")
    target = (data.packaging_id, data.location_id, str(data.resource_id) if data.resource_id else None)
    previous = [row.revision for row in profiles
        if (row.packaging_id, row.location_id, row.resource_id) == target]
    revision = max(previous, default=0)
    if revision != data.expected_revision or revision >= 2147483647:
        reject("Версия профиля изменилась или исчерпана")
    bound = None
    chain = None
    if data.packaging_id is not None:
        packaging = await session.get(Packaging, data.packaging_id)
        if packaging is None or packaging.organization_id != warehouse.organization_id:
            reject("Упаковка принадлежит другой организации")
    else:
        if data.location_id is not None:
            place = by_location.get(data.location_id)
            if place is None:
                reject("Ячейка не найдена в этом складе")
            await location(session, warehouse.warehouse_id, data.location_id)
            resource_id = place.resource_id
            bound = place.resource_mapping_revision
        else:
            resource_id = target[2]
            resource = by_resource.get(resource_id)
            if resource is None:
                reject("Ресурс не найден в этом складе")
            bound = resource.topology_revision
        if resource_id is None or not bound or bound != data.expected_mapping_revision:
            reject("Mapping/topology revision отсутствует или изменилась")
        connection = await session.connection()
        chain = await connection.run_sync(lambda conn: _cap_chain(conn, warehouse.warehouse_id, resource_id))
    values = data.model_dump(mode="python", exclude={
        "protocol", "operation_id", "kind", "warehouse_id", "profile_id",
        "expected_revision", "expected_mapping_revision", "slots",
    })
    values["resource_id"] = target[2]
    profile = PlacementProfile(id=profile_id, organization_id=warehouse.organization_id,
        warehouse_id=warehouse.warehouse_id, revision=revision + 1,
        approved_by=actor, approved_at=datetime.now(timezone.utc),
        bound_mapping_revision=bound, ancestor_binding=chain,
        digest="", canonical_payload="", sealed=False, **values)
    slots = [PlacementSlot(warehouse_id=warehouse.warehouse_id, location_id=data.location_id,
        profile_id=profile_id, **item.model_dump(mode="python")) for item in data.slots]
    snapshot = profile_payload(profile, slots)
    profile.canonical_payload = canonical(snapshot)
    profile.digest = digest(snapshot)
    session.add(profile)
    await session.flush()
    session.add_all(slots)
    await session.flush()
    profile.sealed = True
    await session.flush()
    check_profile_snapshot(profile, slots)
    return {"profile_id": profile.id, "revision": profile.revision,
        "digest": profile.digest, "snapshot": snapshot}


def _next_carrier_revision(value):
    from modules.wms.physical_stock import reject
    if type(value) is not int or not 1 <= value <= 2147483647:
        raise ValueError("capacity_carrier_revision_corruption")
    if value == 2147483647:
        reject("capacity_carrier_revision_exhausted", 409)
    return value + 1


async def _capacity_transit_origin(session, organization_id, unit):
    from modules.wms.item_identity import (
        _origin_frontier_tokens,
        _OriginUnproven,
        _prove_unit_boundary,
    )
    from modules.wms.physical_stock import physical_unit_frontier

    require_current_in_transit(unit)
    try:
        frontier, records = await physical_unit_frontier(session, organization_id, unit.id)
        origin = await _prove_unit_boundary(
            session, organization_id, unit.id, _origin_frontier_tokens(frontier),
            current_unit=unit, expected_state="in_transit", allow_zero=True,
        )
        if unit.assembly_output_id is not None:
            from modules.wms.assembly_quality import physical_assembly_identity

            identity, _output, birth = await physical_assembly_identity(
                session, organization_id, unit.assembly_output_id,
            )
            if (identity.sku_code != unit.sku_code or identity.batch_id != unit.batch_id
                    or origin.get("root_operation_id") != birth.operation_id
                    or origin.get("qc_birth_operation_id") is None):
                raise _OriginUnproven
    except _OriginUnproven as exc:
        raise ValueError("capacity_transit_origin_unproven") from exc
    return origin, frontier, records


async def _addressable_capacity_units(session, organization_id, units):
    present = []
    for unit in units:
        if unit.stock_state == "in_transit":
            await _capacity_transit_origin(session, organization_id, unit)
        else:
            require_current_on_hand(unit)
            present.append(unit)
    return present


class _ImmediateCapacityReads:
    """Only canonical origin reads may borrow the caller's synchronous transaction."""

    def __init__(self, session):
        self.session = session

    @property
    def no_autoflush(self):
        return self.session.no_autoflush

    def in_transaction(self):
        return self.session.in_transaction()

    async def get(self, *args, **kwargs):
        return self.session.get(*args, **kwargs)

    async def scalar(self, statement, *args, **kwargs):
        if getattr(statement, "is_select", False) is not True:
            raise ValueError("capacity_origin_read_only")
        return self.session.scalar(statement, *args, **kwargs)

    async def scalars(self, statement, *args, **kwargs):
        if getattr(statement, "is_select", False) is not True:
            raise ValueError("capacity_origin_read_only")
        return self.session.scalars(statement, *args, **kwargs)

    async def execute(self, statement, *args, **kwargs):
        if getattr(statement, "is_select", False) is not True:
            raise ValueError("capacity_origin_read_only")
        return self.session.execute(statement, *args, **kwargs)

    async def connection(self):
        return self

    async def run_sync(self, function):
        return function(self.session.connection())


def _immediate_capacity_origin(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as done:
        return done.value
    finally:
        coroutine.close()
    raise ValueError("capacity_origin_unexpected_suspension")


def _addressable_capacity_db_rows(connection, organization_id, rows):
    present, transit = [], []
    for row in rows:
        if row["stock_state"] == "in_transit":
            transit.append(row)
        else:
            require_current_on_hand(row)
            present.append(row)
    if not transit:
        return present
    if not connection.in_transaction():
        raise ValueError("capacity_origin_transaction_required")
    # rollback_only makes close release this Session without ending the caller's
    # transaction/savepoint. No writes or transaction ownership cross the facade.
    with Session(bind=connection, autoflush=False, join_transaction_mode="rollback_only",
                 info={"wms_native_historical_reader_v1": True}) as borrowed:
        if borrowed.connection() is not connection:
            raise ValueError("capacity_origin_connection_mismatch")
        reader = _ImmediateCapacityReads(borrowed)
        for row in transit:
            unit = borrowed.get(StockUnit, row["id"], populate_existing=True)
            if unit is None or any(
                getattr(unit, column.name) != row[column.name]
                for column in StockUnit.__table__.columns
            ):
                raise ValueError("capacity_origin_current_row_changed")
            _immediate_capacity_origin(_capacity_transit_origin(reader, organization_id, unit))
    return present


def _observation_row(row):
    from modules.wms.physical_stock import canonical, timestamp_text
    if isinstance(row, StockUnit):
        require_current_on_hand(row)
    answer = {}
    columns = (
        (row.__table__.columns[name] for name in STOCK_UNIT_MEASUREMENT_V1_COLUMNS)
        if isinstance(row, StockUnit) else row.__table__.columns
    )
    for column in columns:
        value = getattr(row, column.name)
        if value is not None and isinstance(column.type, Numeric):
            value = _cap_fixed(value)
        elif isinstance(value, datetime):
            value = (timestamp_text(value) if isinstance(row, StockUnit)
                     else _cap_timestamp_text(value))
        elif isinstance(value, date):
            value = value.isoformat()
        elif isinstance(value, UUID):
            value = str(value)
        answer[column.name] = value
    # Copy JSON children by value; validate finite, canonical JSON now. Legacy
    # JSON/scalar identity is preserved, not reinterpreted as new quantity data.
    return json.loads(canonical(answer))


def _observation_frame_values(carrier_row, position_rows, unit_rows, location_id):
    carrier_id = None if carrier_row is None else carrier_row["id"]
    return {
        "carrier": carrier_row,
        "members": sorted([
            row for row in unit_rows
            if carrier_id is not None and row["carrier_id"] == carrier_id
        ], key=lambda row: row["id"]),
        "positions": sorted([
            row for row in position_rows
            if carrier_id is not None and row["carrier_id"] == carrier_id
        ], key=lambda row: row["ordinal"]),
        "location_units": sorted([
            row for row in unit_rows if row["location_id"] == location_id
        ], key=lambda row: row["id"]),
    }


def _observation_frame(carrier, positions, units, location_id):
    return _observation_frame_values(
        None if carrier is None else _observation_row(carrier),
        [_observation_row(row) for row in positions],
        [_observation_row(row) for row in units], location_id,
    )


async def _locked_observation_rows(session, warehouse_id):
    # Shared helper owns exact RC check and resource -> location -> profile ->
    # child-slot locks. It is not duplicated by this Carrier producer.
    resources, places, profiles, slots = await capacity_metadata_rows(
        session, warehouse_id
    )
    carriers = list((await session.scalars(
        select(PhysicalCarrier).where(
            PhysicalCarrier.warehouse_id == warehouse_id
        ).order_by(PhysicalCarrier.id).with_for_update()
        .execution_options(populate_existing=True)
    )).all())
    positions = list((await session.scalars(
        select(CarrierPosition).where(
            CarrierPosition.warehouse_id == warehouse_id
        ).order_by(CarrierPosition.carrier_id, CarrierPosition.ordinal)
        .with_for_update().execution_options(populate_existing=True)
    )).all())
    # Count mutable state is read under the already-held WH mutex, not locked
    # late after units. Actual immutable approved receipts are checked below.
    counts = list((await session.scalars(
        select(PhysicalCount).where(
            PhysicalCount.warehouse_id == warehouse_id
        ).order_by(PhysicalCount.id)
        .execution_options(populate_existing=True)
    )).all())
    lines = list((await session.scalars(
        select(PhysicalCountLine).join(
            PhysicalCount, PhysicalCount.id == PhysicalCountLine.count_id
        ).where(PhysicalCount.warehouse_id == warehouse_id)
        .order_by(PhysicalCountLine.count_id, PhysicalCountLine.id)
        .execution_options(populate_existing=True)
    )).all())
    # ALL units, including zero members and unbound arrivals, are locked.
    units = list((await session.scalars(
        select(StockUnit).where(StockUnit.warehouse_id == warehouse_id)
        .order_by(StockUnit.id).with_for_update()
        .execution_options(populate_existing=True)
    )).all())
    return places, profiles, slots, carriers, positions, counts, lines, units


def _slot_intersects(a, b):
    return (
        a.x_mm < b.x_mm + b.width_mm
        and b.x_mm < a.x_mm + a.width_mm
        and a.y_mm < b.y_mm + b.height_mm
        and b.y_mm < a.y_mm + a.height_mm
        and a.z_mm < b.z_mm + b.depth_mm
        and b.z_mm < a.z_mm + a.depth_mm
    )


async def observe_carrier(session, warehouse, data, actor):
    with session.no_autoflush:
        # New-policy check is deliberately after the executor's two replay
        # checks, before any handler reload can overwrite pending ORM edits.
        from modules.wms.physical_stock import reject
        if session.new or session.dirty or session.deleted:
            reject("capacity_pending_changes_before_observation", 409)
        return await _observe_carrier_no_autoflush(
            session, warehouse, data, actor
        )


async def _observe_carrier_no_autoflush(session, warehouse, data, actor):
    from modules.wms.physical_stock import canonical, digest, dimensions, location, reject

    warehouse_id = warehouse.warehouse_id
    organization_id = warehouse.organization_id
    location_id = data.location_id
    carrier_id = str(data.carrier_id)
    await location(session, warehouse_id, location_id)
    (
        places, profiles, slots, carriers, positions, counts, lines, units,
    ) = await _locked_observation_rows(session, warehouse_id)
    units = await _addressable_capacity_units(session, organization_id, units)
    connection = await session.connection()
    by_carrier = {row.id: row for row in carriers}
    by_unit = {row.id: row for row in units}
    by_profile = {row.id: row for row in profiles}
    by_count = {row.id: row for row in counts}
    by_location = {row.location_id: row for row in places}
    by_slot = {
        (row.location_id, row.profile_id, row.slot_key): row for row in slots
    }
    requested_ids = {str(value) for value in data.member_unit_ids}
    selected_ids = {str(value) for value in data.source_operation_ids}

    if location_id not in by_location:
        reject("capacity_location_scope", 409)
    foreign = await session.scalar(select(PhysicalCarrier.id).where(
        PhysicalCarrier.id == carrier_id,
        PhysicalCarrier.warehouse_id != warehouse_id,
    ))
    if foreign is not None:
        reject("capacity_carrier_scope", 409)
    carrier = by_carrier.get(carrier_id)
    if carrier is None:
        if data.expected_revision != 0:
            reject("capacity_carrier_revision", 409)
    else:
        if carrier.location_id != location_id or carrier.state != "present":
            reject("capacity_carrier_transfer_requires_physical_command", 409)
        if carrier.content_revision != data.expected_revision:
            reject("capacity_carrier_revision", 409)
    foreign_member = await session.scalar(select(StockUnit.id).where(
        StockUnit.carrier_id == carrier_id,
        ((StockUnit.warehouse_id != warehouse_id)
         | (StockUnit.location_id != location_id)),
    ))
    if foreign_member is not None:
        raise ValueError("capacity_carrier_member_scope_corruption")
    before = _observation_frame(carrier, positions, units, location_id)
    # Validate even a new carrier's full frame before effects.
    canonical(before)
    if carrier is not None and data.expected_content_digest != digest(before):
        reject("capacity_carrier_content_changed", 409)
    members = [row for row in units if row.carrier_id == carrier_id]
    positive = {row.id for row in members if row.qty > 0}
    if data.observed_empty and positive:
        reject("capacity_observed_empty_conflicts_with_stock", 409)
    if positive - requested_ids:
        reject("capacity_member_removal_requires_physical_command", 409)
    requested_units = []
    for unit_id in sorted(requested_ids):
        unit = by_unit.get(unit_id)
        if unit is None or unit.location_id != location_id:
            reject("capacity_member_scope", 409)
        if unit.carrier_id not in (None, carrier_id):
            reject("capacity_member_owned_by_other_carrier", 409)
        if unit.qty == 0 and unit.carrier_id is None:
            reject("capacity_zero_unbound_is_not_carrier_proof", 409)
        requested_units.append(unit)

    # These reads are immutable provenance, no late FOR UPDATE locks.
    operations = list((await session.scalars(select(PhysicalOperation).where(
        PhysicalOperation.organization_id == organization_id,
        PhysicalOperation.warehouse_id == warehouse_id,
    ).order_by(PhysicalOperation.id))).all())
    by_operation = {row.operation_id: row for row in operations}
    by_operation_pk = {row.id: row for row in operations}
    if selected_ids - set(by_operation):
        reject("capacity_source_operation_scope", 409)
    from modules.wms.placement_models import placement_operation_indicator
    from modules.wms.placement_service import (
        _native_dimensions, placement_ledger_dimensions, validate_placement_receipt,
    )
    placement_sources = {}
    placement_rows = {}
    for operation_id in sorted(selected_ids):
        operation = by_operation[operation_id]
        if placement_operation_indicator(operation.kind, operation.result):
            if operation.kind != "capacity_placement_complete":
                raise ValueError("measurement_observation_placement_source_kind")
            source = {column.name: getattr(operation, column.name)
                      for column in PhysicalOperation.__table__.columns}
            placement_rows[operation_id] = source
            placement_sources[operation_id] = await connection.run_sync(
                lambda sync: validate_placement_receipt(sync, source)
            )
    ledgers = list((await session.scalars(select(PhysicalLedger).join(
        PhysicalOperation, PhysicalOperation.id == PhysicalLedger.operation_pk
    ).where(
        PhysicalOperation.organization_id == organization_id,
        PhysicalOperation.warehouse_id == warehouse_id,
    ).order_by(PhysicalLedger.id))).all())
    count_id = (None if data.approved_count_id is None
                else str(data.approved_count_id))
    declared_count = None if count_id is None else by_count.get(count_id)
    if count_id is not None and (
        declared_count is None or declared_count.status != "approved"
        or not declared_count.approved_by or declared_count.approved_at is None
        or location_id not in declared_count.completed_locations
    ):
        reject("capacity_count_not_approved_for_location", 409)
    count_receipts = {}
    for operation in operations:
        if operation.kind != "count_approve":
            continue
        reference = operation.result.get("count_id")
        count = by_count.get(reference)
        if (count is not None and count.status == "approved"
                and count.approved_by and count.approved_at is not None
                and location_id in count.completed_locations):
            count_receipts.setdefault(reference, []).append(operation)
    if count_id is not None and count_id not in count_receipts:
        reject("capacity_count_receipt_missing", 409)
    allowed_pks = {by_operation[value].id for value in selected_ids}
    if count_id is not None:
        allowed_pks.update(row.id for row in count_receipts[count_id])
    proof = {}
    used_ids = set()
    for unit in requested_units:
        current_dimensions = dimensions(unit)
        references = []
        for ledger in ledgers:
            if (ledger.operation_pk not in allowed_pks or ledger.unit_id != unit.id
                    or not (ledger.qty_delta > 0 or unit.qty == 0)):
                continue
            operation = by_operation_pk[ledger.operation_pk]
            ledger_dimensions = ledger.dimensions
            if operation.operation_id in placement_sources:
                ledger_row = {column.name: getattr(ledger, column.name)
                              for column in PhysicalLedger.__table__.columns}
                source = placement_rows[operation.operation_id]
                ledger_dimensions = await connection.run_sync(
                    lambda sync: placement_ledger_dimensions(sync, ledger_row, source)
                )
            if canonical(ledger_dimensions) == canonical(current_dimensions):
                used_ids.add(operation.operation_id)
                references.append({
                    "operation_id": operation.operation_id,
                    "ledger_id": ledger.id, "dimensions": ledger_dimensions,
                })
        if declared_count is not None:
            for line in lines:
                if (line.count_id == count_id and line.unit_id == unit.id
                        and line.location_id == location_id and line.qty == unit.qty
                        and canonical(line.dimensions)
                        == canonical(current_dimensions)):
                    receipts = count_receipts[count_id]
                    used_ids.update(row.operation_id for row in receipts)
                    references.append({
                        "count_id": count_id, "count_line_id": line.id,
                        "operation_ids": [row.operation_id for row in receipts],
                        "dimensions": line.dimensions,
                    })
        if unit.qty > 0 and not references:
            reject("capacity_member_current_provenance_missing", 409)
        if references:
            proof[unit.id] = references
    for operation_id, placement in placement_sources.items():
        destination = placement["destination_after"]
        if destination["carrier"]["id"] != carrier_id:
            continue
        for member in destination["members"]:
            if Decimal(member["qty"]) != 0:
                continue
            unit = by_unit.get(member["id"])
            if (unit is None or unit.location_id != location_id
                    or unit.carrier_id != carrier_id):
                continue
            if (unit.qty != 0 or canonical(_native_dimensions(member))
                    != canonical(dimensions(unit))):
                raise ValueError("measurement_observation_empty_placement_members")
            used_ids.add(operation_id)
        if not destination["members"] and not positive and not requested_ids:
            used_ids.add(operation_id)
    if not positive and not requested_ids:
        if declared_count is None:
            member_ids = {row.id for row in members}
            for ledger in ledgers:
                if (ledger.operation_pk in allowed_pks
                        and ledger.unit_id in member_ids
                        and canonical(ledger.dimensions)
                        == canonical(dimensions(by_unit[ledger.unit_id]))):
                    used_ids.add(by_operation_pk[ledger.operation_pk].operation_id)
            if not used_ids:
                reject("capacity_empty_observation_provenance_missing", 409)
        else:
            used_ids.update(row.operation_id for row in count_receipts[count_id])
    if selected_ids - used_ids:
        reject("capacity_unrelated_source_operation", 409)

    issues = []
    def issue(code, *refs):
        issues.append({"code": code, "refs": sorted({str(x) for x in refs})})

    newly_found = set()
    for unit in units:
        if (unit.location_id != location_id or unit.carrier_id is not None
                or unit.qty <= 0):
            continue
        for ledger in ledgers:
            operation = by_operation_pk[ledger.operation_pk]
            reference = operation.result.get("count_id")
            if (operation.kind != "count_approve" or reference not in count_receipts
                    or ledger.unit_id != unit.id or ledger.qty_delta <= 0
                    or canonical(ledger.dimensions) != canonical(dimensions(unit))):
                continue
            if any(
                line.count_id == reference and line.unit_id is None
                and line.location_id == location_id and line.qty == ledger.qty_delta
                and canonical(line.dimensions) == canonical(ledger.dimensions)
                for line in lines
            ):
                newly_found.add(unit.id)
    outside_claim = {
        row.id for row in units if row.location_id == location_id
        and row.carrier_id is None and row.qty > 0 and row.id not in requested_ids
    }
    unresolved = newly_found | outside_claim
    unapplied = requested_ids & newly_found
    if unresolved:
        issue("carrier_membership_unknown", *unresolved)

    async def checked_profile(profile_id, packaging):
        profile = by_profile.get(profile_id)
        if (profile is None or profile.organization_id != organization_id
                or not profile.sealed or not profile.approved_by
                or profile.approved_at is None):
            reject("capacity_profile_not_approved_in_scope", 409)
        if packaging:
            if (profile.packaging_id is None or profile.location_id is not None
                    or profile.resource_id is not None):
                reject("capacity_packaging_profile_target", 409)
        elif (profile.location_id != location_id or profile.packaging_id is not None
              or profile.resource_id is not None):
            reject("capacity_position_profile_target", 409)
        children = [row for row in slots if row.profile_id == profile.id]
        check_profile_snapshot(profile, children)
        values = {column.name: getattr(profile, column.name)
                  for column in PlacementProfile.__table__.columns}
        try:
            await connection.run_sync(
                lambda sync_connection: _cap_current_binding(sync_connection, values)
            )
        except ValueError as exc:
            if exc.args != ("capacity_binding_stale_or_fabricated",):
                raise
            issue("profile_binding_stale", profile.id)
        return profile

    packaging_profile = None
    if data.packaging_profile_id is None:
        issue("packaging_profile_unknown", carrier_id)
    else:
        packaging_profile = await checked_profile(str(data.packaging_profile_id), True)
        if (carrier is not None and carrier.packaging_id is not None
                and carrier.packaging_id != packaging_profile.packaging_id):
            reject("capacity_repack_requires_physical_command", 409)
        if any(getattr(packaging_profile, name) is None
               for name in ("length_mm", "width_mm", "height_mm")):
            issue("packaging_geometry_unknown", packaging_profile.id)
        if (data.gross_kg is not None and packaging_profile.gross_limit_kg is not None
                and data.gross_kg > packaging_profile.gross_limit_kg):
            issue("gross_limit_exceeded", packaging_profile.id)
    desired = []
    for item in data.positions:
        profile = await checked_profile(str(item.location_profile_id), False)
        slot = by_slot.get((location_id, profile.id, item.slot_key))
        if slot is None:
            reject("capacity_position_slot_missing", 409)
        desired.append((profile.id, item.slot_key, slot))
    desired.sort(key=lambda item: (item[0], item[1]))
    if not desired:
        issue("position_not_observed", carrier_id)
    else:
        for index, first in enumerate(desired):
            for second in desired[index + 1:]:
                if _slot_intersects(first[2], second[2]):
                    issue("observed_position_overlap", carrier_id,
                          first[0], first[1], second[0], second[1])
            for position in positions:
                if position.location_id != location_id or position.carrier_id == carrier_id:
                    continue
                other = by_slot.get((location_id, position.location_profile_id,
                                     position.slot_key))
                if other is None:
                    raise ValueError("capacity_position_reference_corruption")
                if _slot_intersects(first[2], other):
                    issue("observed_position_overlap", carrier_id, position.carrier_id)
    if data.gross_kg is None or data.tare_kg is None:
        issue("mass_measurement_unknown", carrier_id)

    # Complete immutable scalar/snapshot plan BEFORE the first ORM mutation.
    target_revision = 1 if carrier is None else _next_carrier_revision(carrier.content_revision)
    invalidation_plan = []
    if unresolved:
        for other in carriers:
            if (other.location_id == location_id and other.id != carrier_id
                    and other.confirmed_content_revision is not None):
                invalidation_plan.append((other,
                    _next_carrier_revision(other.content_revision),
                    _observation_row(other)))
    measurement_confirmed = (
        data.confirm_measurements and not unresolved and not unapplied
        and data.gross_kg is not None and data.tare_kg is not None
    )
    now = datetime.now(timezone.utc)
    old_packaging_id = None if carrier is None else carrier.packaging_id
    old_packaging_profile_id = None if carrier is None else carrier.packaging_profile_id
    carrier_values = {
        "id": carrier_id, "warehouse_id": warehouse_id, "location_id": location_id,
        "packaging_id": (old_packaging_id if packaging_profile is None
                         else packaging_profile.packaging_id),
        "packaging_profile_id": (old_packaging_profile_id if packaging_profile is None
                                 else packaging_profile.id),
        "content_revision": target_revision,
        "confirmed_content_revision": (target_revision if measurement_confirmed else None),
        "gross_kg": data.gross_kg, "tare_kg": data.tare_kg,
        "measurement_evidence": data.evidence, "observed_by": actor,
        "observed_at": now, "state": "present",
    }
    if set(carrier_values) != {column.name for column in PhysicalCarrier.__table__.columns}:
        raise ValueError("capacity_observation_carrier_shape")
    carrier_row = dict(carrier_values)
    for name in ("gross_kg", "tare_kg"):
        carrier_row[name] = (None if carrier_row[name] is None
                             else _cap_fixed(carrier_row[name]))
    carrier_row["observed_at"] = _cap_timestamp_text(now)
    apply_ids = requested_ids - unapplied
    planned_unit_rows = [_observation_row(row) for row in units]
    for row in planned_unit_rows:
        if row["id"] in apply_ids:
            row["carrier_id"] = carrier_id
    position_values = [{
        "carrier_id": carrier_id, "ordinal": ordinal,
        "warehouse_id": warehouse_id, "location_id": location_id,
        "location_profile_id": profile_id, "slot_key": slot_key,
    } for ordinal, (profile_id, slot_key, _) in enumerate(desired)]
    planned_position_rows = [_observation_row(row) for row in positions]
    if desired:
        planned_position_rows = [row for row in planned_position_rows
                                 if row["carrier_id"] != carrier_id] + position_values
    after = _observation_frame_values(carrier_row, planned_position_rows,
                                      planned_unit_rows, location_id)
    issue("placement_evaluator_unavailable", carrier_id)
    issues.sort(key=lambda value: (value["code"], tuple(value["refs"])))
    result = {
        "carrier_id": carrier_id, "content_revision": target_revision,
        "content_digest": digest(after),
        "confirmed_content_revision": carrier_values["confirmed_content_revision"],
        "observed_empty": data.observed_empty,
        "reported": capacity_request_snapshot(data), "before": before, "after": after,
        "source_proofs": proof, "unapplied_member_ids": sorted(unapplied),
        "invalidated_carriers": [{
            "before": snapshot, "reason": "carrier_membership_unknown",
            "unit_ids": sorted(unresolved),
        } for _, _, snapshot in invalidation_plan],
        "measurement_confirmation": {
            "requested": data.confirm_measurements, "accepted": measurement_confirmed,
            "content_revision": target_revision if measurement_confirmed else None,
        },
        "planning": {"status": "cannot_plan", "placement_authorized": False,
                     "violations": issues},
    }
    canonical(result)  # Entire response JSON plan validated before effects.

    # Effects begin here. No qty/reserved/hold/counter mutation anywhere.
    for other, next_revision, _ in invalidation_plan:
        other.content_revision = next_revision
        other.confirmed_content_revision = None
    if carrier is None:
        carrier = PhysicalCarrier(**carrier_values)
        session.add(carrier)
    else:
        for name, value in carrier_values.items():
            setattr(carrier, name, value)
    for unit in requested_units:
        if unit.id in apply_ids:
            unit.carrier_id = carrier_id
    if desired:
        for position in positions:
            if position.carrier_id == carrier_id:
                await session.delete(position)
        await session.flush()  # Release old ordinal PKs, never an intermediate commit.
        new_positions = [CarrierPosition(**values) for values in position_values]
        session.add_all(new_positions)
        final_positions = [row for row in positions if row.carrier_id != carrier_id] + new_positions
    else:
        final_positions = positions  # Unknown positions do NOT erase footprint.
    await session.flush()
    actual_after = _observation_frame(carrier, final_positions, units, location_id)
    if canonical(actual_after) != canonical(after):
        raise ValueError("capacity_observation_plan_state_mismatch")
    return result


# One append-only approved observation; this is not an equipment executor.
def equipment_operation_indicator(kind, result):
    from modules.wms.physical_schemas import EQUIPMENT_STATE_KIND
    return kind == EQUIPMENT_STATE_KIND or (type(result) is dict and result.get("kind") == EQUIPMENT_STATE_KIND)


def equipment_operation_receipt(row, connection):
    from modules.wms.physical_models import _cap_full_snapshot
    from modules.wms.physical_schemas import equipment_receipt

    value = equipment_receipt(row["result"])
    if any(row[key] != value[key] for key in ("organization_id", "warehouse_id", "operation_id", "kind", "request_hash")) or row["actor"] != value["actor_sub"]:
        raise ValueError("equipment_receipt_row_mismatch")
    binding = value["profile_binding"]
    table = PlacementProfile.__table__
    historical = connection.execute(select(table).where(table.c.id == binding["profile_id"])).mappings().one_or_none()
    if (historical is None or historical["sealed"] is not True
        or historical["organization_id"] != value["organization_id"] or historical["warehouse_id"] != value["warehouse_id"]
        or historical["resource_id"] != value["equipment_resource_id"]
        or historical["packaging_id"] is not None or historical["location_id"] is not None):
        raise ValueError("equipment_historical_profile_scope_or_seal")
    historical = dict(historical)
    actual = {"profile_id": historical["id"], "revision": historical["revision"], "digest": historical["digest"],
        "canonical_payload": historical["canonical_payload"], "bound_mapping_revision": historical["bound_mapping_revision"],
        "ancestor_binding": historical["ancestor_binding"]}
    if actual != binding:
        raise ValueError("equipment_historical_profile_binding_mismatch")
    # Specific historical ID and complete slots, without current topology validation.
    _cap_full_snapshot(connection, historical)
    return value


def measurement_operation_indicator(kind, result):
    from modules.wms.physical_schemas import MEASUREMENTS_KIND, MEASUREMENTS_PRIVATE_KEY
    return kind == MEASUREMENTS_KIND or (type(result) is dict and (
        MEASUREMENTS_PRIVATE_KEY in result or result.get("kind") == MEASUREMENTS_KIND))


def measurement_ack(proof):
    from modules.wms.physical_schemas import MeasuredContextAck
    request = proof["request"]
    violations = []
    def unknown(value, code, refs):
        if value["state"] == "unknown":
            violations.append({"code": code, "refs": refs})
    for row in request["profile_frames"]:
        unknown(row["measured_slots"], "measured_slots_unknown", [row["profile_id"]])
    unknown(request["routes"], "routes_unknown", [])
    if request["routes"]["state"] == "confirmed":
        for row in request["routes"]["value"]:
            refs = [str(row["source_location_id"]), str(row["destination_location_id"])]
            unknown(row["openings"], "openings_unknown", refs)
            unknown(row["corridors"], "corridors_unknown", refs)
    unknown(request["supports"], "supports_unknown", [])
    if request["supports"]["state"] == "confirmed":
        for row in request["supports"]["value"]:
            if row["top_load_limit_kg"] is None:
                violations.append({"code": "support_top_load_unknown", "refs": [row["profile_id"], str(row["location_id"])]})
    unknown(request["carrier_support_relations"], "carrier_relations_unknown", [])
    value = {"protocol": 2, "operation_id": proof["operation_id"], "kind": proof["kind"], "warehouse_id": proof["warehouse_id"],
        "revision": proof["revision"], "digest": proof["digest"], "status": "incomplete" if violations else "confirmed",
        "violations": violations, "frames": [{"frame_key": row["frame_key"], "frame_id": row["frame_id"]} for row in proof["resolved_frames"]]}
    MeasuredContextAck.model_validate(value)
    return value


def _measurement_result_parts(value, operation_kind):
    from modules.wms.placement_policy_codec import measured_context_proof
    from modules.wms.physical_schemas import MEASUREMENTS_KIND, MEASUREMENTS_PRIVATE_KEY
    from modules.wms.physical_stock import canonical
    if operation_kind != MEASUREMENTS_KIND or type(value) is not dict or MEASUREMENTS_PRIVATE_KEY not in value:
        raise ValueError("measurement_public_kind_private_mismatch")
    proof = measured_context_proof(value[MEASUREMENTS_PRIVATE_KEY])
    ack = measurement_ack(proof)
    if set(value) != set(ack) | {MEASUREMENTS_PRIVATE_KEY} or canonical({k:v for k,v in value.items() if k != MEASUREMENTS_PRIVATE_KEY}) != canonical(ack):
        raise ValueError("measurement_public_ack_mismatch")
    return ack, proof


def measurement_public_result(value, operation_kind):
    return _measurement_result_parts(value, operation_kind)[0]


def _measurement_geometry(proof):
    """Check declared geometry against exact immutable native slots; no defaults."""
    from types import SimpleNamespace
    from modules.wms.physical_placement import Slot, _box
    from modules.wms.physical_stock import canonical
    request = proof["request"]
    declarations = {row["profile_id"]: row for row in request["profile_frames"]}
    bindings = {row["profile_id"]: row for row in proof["profile_bindings"]}
    locations = {row["location_id"]: row for row in proof["topology"]["locations"]}
    observations = {row["carrier_id"]: row for row in proof["carrier_observations"]}
    def known(value):
        return value["value"] if value["state"] == "confirmed" else []
    def profile(profile_id, location_id, profile_digest, frame_key):
        binding = bindings.get(profile_id)
        if binding is None or binding["digest"] != profile_digest or binding["frame_key"] != frame_key or binding["snapshot"]["profile"]["location_id"] != location_id:
            raise ValueError("measurement_declared_profile_relation")
        return binding, declarations[profile_id]
    def inside(row, native):
        x,y,z,w,h,d = (Decimal(row[key]) for key in ("x_mm", "y_mm", "z_mm", "length_mm", "height_mm", "width_mm"))
        if any(native[key] is None for key in ("length_mm", "height_mm", "width_mm")) or x+w > Decimal(native["length_mm"]) or y+h > Decimal(native["height_mm"]) or z+d > Decimal(native["width_mm"]):
            raise ValueError("measurement_box_outside_envelope")
    def cuboid(binding, keys):
        slots = tuple(Slot(row["warehouse_id"], row["location_id"], row["profile_id"], row["slot_key"], *(Decimal(row[key]) for key in ("x_mm", "y_mm", "z_mm", "width_mm", "height_mm", "depth_mm"))) for row in binding["snapshot"]["slots"])
        return _box(SimpleNamespace(slots=slots), tuple(keys))
    for profile_id, declaration in declarations.items():
        binding = bindings[profile_id]
        if declaration["measured_slots"]["state"] == "confirmed":
            actual = [{"slot_key": row["slot_key"], "box": {"x_mm": row["x_mm"], "y_mm": row["y_mm"], "z_mm": row["z_mm"],
                "length_mm": row["width_mm"], "height_mm": row["height_mm"], "width_mm": row["depth_mm"]}} for row in binding["snapshot"]["slots"]]
            measured = declaration["measured_slots"]["value"]
            if len({row["slot_key"] for row in measured}) != len(measured) or canonical(sorted(measured, key=lambda row: row["slot_key"])) != canonical(actual):
                raise ValueError("measurement_complete_slots_mismatch")
    for route in known(request["routes"]):
        src, dst = bindings.get(route["source_profile_id"]), bindings.get(route["destination_profile_id"])
        if src is None or dst is None:
            raise ValueError("measurement_route_profile_missing")
        profile(route["source_profile_id"], route["source_location_id"], route["source_profile_digest"], src["frame_key"])
        profile(route["destination_profile_id"], route["destination_location_id"], route["destination_profile_digest"], dst["frame_key"])
        for phase in route["phase_location_ids"]:
            phase_profiles = [row for row in bindings.values() if row["snapshot"]["profile"]["location_id"] == phase]
            if phase not in locations or len(phase_profiles) != 1:
                raise ValueError("measurement_route_phase_profile_missing")
        for group in route["slot_groups"]:
            cuboid(dst, group)
        for opening in known(route["openings"]):
            matching = [row for row in bindings.values() if row["snapshot"]["profile"]["location_id"] == opening["phase_location_id"]]
            if opening["phase_location_id"] not in route["phase_location_ids"] or len(matching) != 1 or matching[0]["frame_key"] != opening["frame_key"]:
                raise ValueError("measurement_opening_phase_frame")
        for corridor in known(route["corridors"]):
            matching = [row for row in bindings.values() if row["snapshot"]["profile"]["location_id"] == corridor["location_id"]]
            if corridor["location_id"] not in route["phase_location_ids"] or len(matching) != 1 or matching[0]["frame_key"] != corridor["frame_key"]:
                raise ValueError("measurement_corridor_phase_frame")
            inside(corridor["box"], matching[0]["snapshot"]["profile"])
    supports = known(request["supports"])
    for row in supports:
        binding, declaration = profile(row["profile_id"], row["location_id"], row["profile_digest"], row["frame_key"])
        native = binding["snapshot"]["profile"]
        if native["max_layers"] is None or row["layer"] >= native["max_layers"] or (native["support_rule"] == "full_base" and row["layer"] != 0):
            raise ValueError("measurement_support_layers")
        if any(native[key] is None for key in ("length_mm", "height_mm", "width_mm")) or Decimal(row["x_max_mm"]) > Decimal(native["length_mm"]) or Decimal(row["z_max_mm"]) > Decimal(native["width_mm"]) or Decimal(row["plane_y_mm"]) > Decimal(native["height_mm"]):
            raise ValueError("measurement_support_outside_envelope")
        if not any(Decimal(slot["y_mm"]) == Decimal(row["plane_y_mm"]) and Decimal(row["x_min_mm"]) <= Decimal(slot["x_mm"]) and Decimal(row["x_max_mm"]) >= Decimal(slot["x_mm"])+Decimal(slot["width_mm"]) and Decimal(row["z_min_mm"]) <= Decimal(slot["z_mm"]) and Decimal(row["z_max_mm"]) >= Decimal(slot["z_mm"])+Decimal(slot["depth_mm"]) for slot in binding["snapshot"]["slots"]):
            raise ValueError("measurement_support_full_footprint_missing")
        bearing = row["bearing"]
        if bearing["kind"] == "resource":
            if bearing["resource_id"] not in {item["resource_id"] for item in locations[row["location_id"]]["ancestor_binding"]}:
                raise ValueError("measurement_support_resource_not_ancestor")
        else:
            observation = observations.get(bearing["carrier_id"])
            if observation is None or any(observation[key] != bearing[other] for key,other in (("observation_operation_id","observation_operation_id"),("content_revision","expected_content_revision"),("content_digest","expected_content_digest"))) or observation["after"]["carrier"]["location_id"] != row["location_id"]:
                raise ValueError("measurement_support_carrier_binding")
            positions = observation["after"]["positions"]
            if not positions or {p["location_profile_id"] for p in positions} != {row["profile_id"]}:
                raise ValueError("measurement_support_lower_frame_or_positions")
            lower_box = cuboid(binding, [p["slot_key"] for p in positions])
            if (lower_box.y + lower_box.h != Decimal(row["plane_y_mm"])
                or Decimal(row["x_min_mm"]) < lower_box.x or Decimal(row["x_max_mm"]) > lower_box.x + lower_box.w
                or Decimal(row["z_min_mm"]) < lower_box.z or Decimal(row["z_max_mm"]) > lower_box.z + lower_box.d):
                raise ValueError("measurement_support_lower_surface_or_footprint")
    relations = known(request["carrier_support_relations"])
    if len({row["carrier_id"] for row in relations}) != len(relations):
        raise ValueError("measurement_duplicate_carrier_relation")
    for row in relations:
        observation = observations.get(row["carrier_id"])
        if observation is None or observation["observation_operation_id"] != row["observation_operation_id"] or observation["content_revision"] != row["expected_content_revision"] or observation["content_digest"] != row["expected_content_digest"]:
            raise ValueError("measurement_relation_observation_binding")
        carrier = observation["after"]["carrier"]
        binding = bindings.get(row["location_profile_id"])
        if binding is None:
            raise ValueError("measurement_relation_profile_missing")
        profile(row["location_profile_id"], carrier["location_id"], binding["digest"], row["frame_key"])
        positions = observation["after"]["positions"]
        if {p["location_profile_id"] for p in positions} != {row["location_profile_id"]} or sorted(row["slot_keys"]) != sorted(p["slot_key"] for p in positions):
            raise ValueError("measurement_relation_complete_positions")
        upper_box = cuboid(binding, row["slot_keys"])
        lower = row["support_carrier_id"]
        if lower is not None:
            candidates = [support for support in supports if support["location_id"] == carrier["location_id"] and support["profile_id"] == row["location_profile_id"] and support["frame_key"] == row["frame_key"] and support["bearing"]["kind"] == "carrier" and support["bearing"]["carrier_id"] == lower]
            if len(candidates) != 1:
                raise ValueError("measurement_relation_lower_support_missing")
            support = candidates[0]
            if (upper_box.y != Decimal(support["plane_y_mm"])
                or upper_box.x < Decimal(support["x_min_mm"]) or upper_box.x + upper_box.w > Decimal(support["x_max_mm"])
                or upper_box.z < Decimal(support["z_min_mm"]) or upper_box.z + upper_box.d > Decimal(support["z_max_mm"])):
                raise ValueError("measurement_relation_supported_group_footprint")
    required_locations = {row["anchor_location_id"] for row in proof["resolved_frames"]} | {row["snapshot"]["profile"]["location_id"] for row in bindings.values()}
    for row in known(request["routes"]):
        required_locations.update(row["phase_location_ids"])
    required_locations.update(row["location_id"] for row in supports)
    if set(locations) != required_locations:
        raise ValueError("measurement_topology_location_footprint")
    required_resources = {item["resource_id"] for row in locations.values() for item in row["ancestor_binding"]}
    if {row["id"] for row in proof["topology"]["resources"]} != required_resources:
        raise ValueError("measurement_topology_resource_footprint")
    required_carriers = {row["bearing"]["carrier_id"] for row in supports if row["bearing"]["kind"] == "carrier"} | {row["carrier_id"] for row in relations}
    if set(observations) != required_carriers:
        raise ValueError("measurement_observation_declared_footprint")


def _measurement_support_packaging(connection, proof, *, current):
    from modules.wms.physical_models import _cap_full_snapshot
    request = proof["request"]
    if request["supports"]["state"] != "confirmed":
        return
    observations = {row["carrier_id"]:row for row in proof["carrier_observations"]}
    for support in request["supports"]["value"]:
        if support["bearing"]["kind"] != "carrier":
            continue
        carrier = observations[support["bearing"]["carrier_id"]]["after"]["carrier"]
        table = PlacementProfile.__table__
        profile = connection.execute(select(table).where(table.c.id == carrier["packaging_profile_id"],
            table.c.organization_id == proof["organization_id"],table.c.warehouse_id == proof["warehouse_id"])).mappings().one_or_none()
        if profile is None or profile["sealed"] is not True or profile["packaging_id"] != carrier["packaging_id"] or profile["location_id"] is not None or profile["resource_id"] is not None:
            raise ValueError("measurement_lower_packaging_profile_missing")
        _cap_full_snapshot(connection,dict(profile))
        if current:
            latest = connection.execute(select(table).where(table.c.organization_id == proof["organization_id"],
                table.c.warehouse_id == proof["warehouse_id"],table.c.packaging_id == carrier["packaging_id"]).order_by(table.c.revision.desc(),table.c.id)).mappings().first()
            if latest is None or latest["id"] != profile["id"]:
                raise ValueError("measurement_lower_packaging_profile_stale")
        limit = support["top_load_limit_kg"]
        if limit is not None and (profile["top_load_limit_kg"] is None or Decimal(limit) > profile["top_load_limit_kg"]):
            raise ValueError("measurement_lower_packaging_top_load_unproved")


def measurement_receipt(value, row, connection):
    """Historical receipt validation; never require current metadata here."""
    from modules.wms.physical_models import _cap_full_snapshot
    from modules.wms.physical_stock import canonical
    _, proof = _measurement_result_parts(value, row["kind"])
    if any(row[key] != proof[key] for key in ("organization_id", "warehouse_id", "operation_id", "kind", "request_hash")) or row["actor"] != proof["approver"]["sub"]:
        raise ValueError("measurement_receipt_row_correspondence")
    _measurement_geometry(proof)

    _measurement_support_packaging(connection,proof,current=False)

    for binding in proof["profile_bindings"]:
        table = PlacementProfile.__table__
        historical = connection.execute(select(table).where(table.c.id == binding["profile_id"])).mappings().one_or_none()
        if historical is None or historical["sealed"] is not True:
            raise ValueError("measurement_historical_profile_missing")
        _cap_full_snapshot(connection, dict(historical))
        if canonical(json.loads(historical["canonical_payload"])) != canonical(binding["snapshot"]) or historical["digest"] != binding["digest"]:
            raise ValueError("measurement_historical_profile_mismatch")
    for observation in proof["carrier_observations"]:
        _measurement_observation_receipt(connection, proof["organization_id"], proof["warehouse_id"], observation, measurement_pk=row["id"])
    table = PhysicalOperation.__table__
    for frame in proof["resolved_frames"]:
        if frame["resolution"]["kind"] == "reuse":
            prior = connection.execute(select(table).where(table.c.organization_id == proof["organization_id"], table.c.warehouse_id == proof["warehouse_id"], table.c.operation_id == frame["resolution"]["approval_operation_id"], table.c.id < row["id"])).mappings().one_or_none()
            if prior is None:
                raise ValueError("measurement_reuse_historical_approval_missing")
            _, prior_proof = _measurement_result_parts(prior["result"], prior["kind"])
            if prior["kind"] != proof["kind"] or prior_proof is None or prior_proof["digest"] != frame["resolution"]["approval_digest"]:
                raise ValueError("measurement_reuse_historical_approval_corrupt")
            if any(prior[key] != prior_proof[key] for key in ("organization_id","warehouse_id","operation_id","kind","request_hash")) or prior["actor"] != prior_proof["approver"]["sub"]:
                raise ValueError("measurement_reuse_historical_row_mismatch")
            matches = [item for item in prior_proof["resolved_frames"] if item["frame_id"] == frame["frame_id"] and all(item[key] == frame[key] for key in ("anchor_location_id", "datum_mark", "axes"))]
            if not matches:
                raise ValueError("measurement_reused_frame_mismatch")
    return value


def measurement_state_head(connection, organization_id, warehouse_id):
    """Decode all actual scoped rows, including malformed private indicators."""
    require_capacity_read_committed(connection)
    table = PhysicalOperation.__table__
    rows = connection.execute(select(table).where(table.c.organization_id == organization_id, table.c.warehouse_id == warehouse_id).order_by(table.c.id)).mappings().all()
    head = None
    for row in rows:
        if not measurement_operation_indicator(row["kind"], row["result"]):
            continue
        measurement_receipt(row["result"], row, connection)
        proof = row["result"]["_placement_measured_context_v1"]
        if proof["revision"] != (1 if head is None else head["revision"]+1) or proof["previous_operation_id"] != (None if head is None else head["operation_id"]):
            raise ValueError("measurement_history_branch_or_gap")
        head = proof
    return head


def _measurement_observation_transition(connection, row, request, result):
    """Prove the existing CarrierObserve plan from immutable historical inputs."""
    from modules.wms.placement_policy_codec import _measured_native_frame
    from modules.wms.physical_models import _cap_full_snapshot
    from modules.wms.physical_stock import canonical, digest, dimensions
    from types import SimpleNamespace

    wh, org = row["warehouse_id"], row["organization_id"]
    carrier_id, location_id = str(request.carrier_id), request.location_id
    before, after = result["before"], result["after"]
    _measured_native_frame(after, wh)
    if type(before) is not dict or set(before) != {"carrier", "members", "positions", "location_units"}:
        raise ValueError("measurement_observation_before_shape")
    old, new = before["carrier"], after["carrier"]
    if new["id"] != carrier_id or new["location_id"] != location_id:
        raise ValueError("measurement_observation_location")
    if old is None:
        if request.expected_revision != 0 or request.expected_content_digest is not None or before["members"] != [] or before["positions"] != []:
            raise ValueError("measurement_observation_new_carrier_before")
    else:
        _measured_native_frame(before, wh)
        if (old["id"] != carrier_id or old["location_id"] != location_id or old["content_revision"] != request.expected_revision
            or digest(before) != request.expected_content_digest):
            raise ValueError("measurement_observation_expected_before")
    revision = 1 if old is None else _next_carrier_revision(old["content_revision"])
    if new["content_revision"] != revision or result["content_revision"] != revision or digest(after) != result["content_digest"]:
        raise ValueError("measurement_observation_revision_transition")

    # Every native unit, including unselected zeros, is preserved except the
    # producer's explicit carrier assignment. No current stock rows are read.
    if type(before["location_units"]) is not list:
        raise ValueError("measurement_observation_before_units")
    before_units = before["location_units"]
    after_units = after["location_units"]
    if len(before_units) != len(after_units):
        raise ValueError("measurement_observation_unit_vector_changed")
    by_unit = {}
    for first, last in zip(before_units, after_units):
        if type(first) is not dict or set(first) != set(last) or first["id"] != last["id"]:
            raise ValueError("measurement_observation_unit_vector_changed")
        if canonical({key:value for key,value in first.items() if key != "carrier_id"}) != canonical({key:value for key,value in last.items() if key != "carrier_id"}):
            raise ValueError("measurement_observation_unit_dimensions_changed")
        if first["carrier_id"] is not None and str(UUID(first["carrier_id"])) != first["carrier_id"]:
            raise ValueError("measurement_observation_before_unit_carrier")
        if old is None and first["carrier_id"] == carrier_id:
            raise ValueError("measurement_observation_new_carrier_prior_member")
        by_unit[first["id"]] = first
    requested = {str(value) for value in request.member_unit_ids}
    positive = {unit_id for unit_id, unit in by_unit.items() if unit["carrier_id"] == carrier_id and Decimal(unit["qty"]) > 0}
    if (request.observed_empty and positive) or positive - requested:
        raise ValueError("measurement_observation_existing_member_removal")
    for unit_id in requested:
        unit = by_unit.get(unit_id)
        if unit is None or unit["carrier_id"] not in (None, carrier_id) or (Decimal(unit["qty"]) == 0 and unit["carrier_id"] is None):
            raise ValueError("measurement_observation_requested_member_scope")
    if set(result["source_proofs"]) - requested:
        raise ValueError("measurement_observation_unrequested_source_proof")

    # Reproduce the native count-adjustment exception from earlier immutable
    # operations/ledgers and approved terminal count lines, not current stock.
    op = PhysicalOperation.__table__
    prior_counts = connection.execute(select(op).where(op.c.organization_id == org, op.c.warehouse_id == wh,
        op.c.kind == "count_approve", op.c.id < row["id"])).mappings().all()
    count_by_pk = {}
    for item in prior_counts:
        count_id = item["result"].get("count_id") if type(item["result"]) is dict else None
        count = connection.execute(select(PhysicalCount.__table__).where(PhysicalCount.__table__.c.id == count_id,
            PhysicalCount.__table__.c.warehouse_id == wh)).mappings().one_or_none()
        if count is not None and count["status"] == "approved" and count["approved_by"] and count["approved_at"] is not None and location_id in count["completed_locations"]:
            count_by_pk[item["id"]] = count_id
    ledgers = connection.execute(select(PhysicalLedger.__table__).where(
        PhysicalLedger.__table__.c.operation_pk.in_(sorted(count_by_pk)), PhysicalLedger.__table__.c.qty_delta > 0)).mappings().all()
    lines = connection.execute(select(PhysicalCountLine.__table__).where(
        PhysicalCountLine.__table__.c.count_id.in_(sorted(set(count_by_pk.values()))),
        PhysicalCountLine.__table__.c.location_id == location_id, PhysicalCountLine.__table__.c.unit_id.is_(None))).mappings().all()
    newly_found = set()
    for ledger in ledgers:
        unit = by_unit.get(ledger["unit_id"])
        if unit is None or unit["carrier_id"] is not None or Decimal(unit["qty"]) <= 0:
            continue
        historical_unit = {**unit, "last_checked":None if unit["last_checked"] is None else date.fromisoformat(unit["last_checked"]),
            "next_service":None if unit["next_service"] is None else date.fromisoformat(unit["next_service"]),
            "arrived_at":datetime.fromisoformat(unit["arrived_at"])}
        if canonical(ledger["dimensions"]) != canonical(dimensions(SimpleNamespace(**historical_unit))):
            continue
        if any(line["count_id"] == count_by_pk[ledger["operation_pk"]] and line["qty"] == ledger["qty_delta"]
               and canonical(line["dimensions"]) == canonical(ledger["dimensions"]) for line in lines):
            newly_found.add(unit["id"])
    unresolved = newly_found | {unit_id for unit_id, unit in by_unit.items()
        if unit["carrier_id"] is None and Decimal(unit["qty"]) > 0 and unit_id not in requested}
    unapplied = requested & newly_found
    if result["unapplied_member_ids"] != sorted(unapplied):
        raise ValueError("measurement_observation_unapplied_transition")
    for first, last in zip(before_units, after_units):
        expected = carrier_id if first["id"] in requested - unapplied else first["carrier_id"]
        if last["carrier_id"] != expected:
            raise ValueError("measurement_observation_member_transition")

    def historical_profile(profile_id, packaging):
        profile = connection.execute(select(PlacementProfile.__table__).where(PlacementProfile.__table__.c.id == profile_id)).mappings().one_or_none()
        if profile is None or profile["organization_id"] != org or profile["warehouse_id"] != wh or profile["sealed"] is not True or not profile["approved_by"] or profile["approved_at"] is None:
            raise ValueError("measurement_observation_historical_profile")
        if (profile["resource_id"] is not None or (packaging and (profile["packaging_id"] is None or profile["location_id"] is not None))
            or (not packaging and (profile["packaging_id"] is not None or profile["location_id"] != location_id))):
            raise ValueError("measurement_observation_historical_profile_target")
        _cap_full_snapshot(connection, dict(profile))
        return profile

    old_packaging_id = None if old is None else old["packaging_id"]
    old_profile_id = None if old is None else old["packaging_profile_id"]
    if request.packaging_profile_id is None:
        packaging_id, packaging_profile_id = old_packaging_id, old_profile_id
    else:
        profile = historical_profile(str(request.packaging_profile_id), True)
        packaging_id, packaging_profile_id = profile["packaging_id"], profile["id"]
        if old_packaging_id is not None and old_packaging_id != packaging_id:
            raise ValueError("measurement_observation_repack_transition")
    desired = sorted((str(item.location_profile_id), item.slot_key) for item in request.positions)
    for profile_id, slot_key in desired:
        profile = historical_profile(profile_id, False)
        if slot_key not in {item["slot_key"] for item in json.loads(profile["canonical_payload"])["slots"]}:
            raise ValueError("measurement_observation_historical_position_slot")
    expected_positions = [{"carrier_id":carrier_id, "ordinal":index, "warehouse_id":wh, "location_id":location_id,
        "location_profile_id":profile_id, "slot_key":slot_key} for index,(profile_id,slot_key) in enumerate(desired)] if desired else before["positions"]
    if canonical(after["positions"]) != canonical(expected_positions):
        raise ValueError("measurement_observation_position_transition")

    accepted = request.confirm_measurements and not unresolved and not unapplied and request.gross_kg is not None and request.tare_kg is not None
    confirmed_revision = revision if accepted else None
    expected_carrier = {"id":carrier_id, "warehouse_id":wh, "location_id":location_id, "packaging_id":packaging_id,
        "packaging_profile_id":packaging_profile_id, "content_revision":revision, "confirmed_content_revision":confirmed_revision,
        "gross_kg":None if request.gross_kg is None else _cap_fixed(request.gross_kg),
        "tare_kg":None if request.tare_kg is None else _cap_fixed(request.tare_kg), "measurement_evidence":request.evidence,
        "observed_by":row["actor"], "observed_at":new["observed_at"], "state":"present"}
    if canonical(new) != canonical(expected_carrier) or result["confirmed_content_revision"] != confirmed_revision or result["observed_empty"] is not request.observed_empty:
        raise ValueError("measurement_observation_carrier_transition")
    confirmation = {"requested":request.confirm_measurements, "accepted":accepted, "content_revision":confirmed_revision}
    if canonical(result["measurement_confirmation"]) != canonical(confirmation):
        raise ValueError("measurement_observation_confirmation_transition")
    invalidated = []
    for item in result["invalidated_carriers"]:
        snapshot = item["before"]
        _measured_native_frame({"carrier":snapshot, "members":[], "positions":[], "location_units":[]}, wh)
        if (not unresolved or item["unit_ids"] != sorted(unresolved) or snapshot["id"] == carrier_id
            or snapshot["location_id"] != location_id or snapshot["confirmed_content_revision"] is None):
            raise ValueError("measurement_observation_invalidation_transition")
        invalidated.append(snapshot["id"])
    if invalidated != sorted(set(invalidated)):
        raise ValueError("measurement_observation_invalidation_order")


def _historical_observed_origin(connection, organization_id, warehouse_id, operation_pk, unit, *, work_capture=None, full_evidence=False):
    """Drive the existing async native prover with bounded synchronous SELECTs."""
    from contextlib import nullcontext
    from types import SimpleNamespace
    from sqlalchemy.orm import Session
    from sqlalchemy.sql import visitors
    from sqlalchemy.sql.selectable import Select
    from modules.production.assembly_specifications import AssemblySpecification
    from modules.wms.physical_models import (AssemblyConsumption, AssemblyExecution, AssemblyInputLink,
        AssemblyOutputIdentity, AssemblyProcessRun, PickedAllocation, WarehouseControl)
    from modules.wms.item_identity import (_HistoricalOriginContext, _OriginUnproven,
        _origin_frontier_tokens, _prove_unit_boundary)
    from modules.wms.physical_stock import canonical, dimensions, physical_unit_frontier
    if not connection.in_transaction():
        raise ValueError("physical_origin_historical_transaction_required")
    require_capacity_read_committed(connection)
    saved = connection.execute(select(PhysicalOperation.__table__).where(
        PhysicalOperation.__table__.c.id == operation_pk,
        PhysicalOperation.__table__.c.organization_id == organization_id,
        PhysicalOperation.__table__.c.warehouse_id == warehouse_id,
        PhysicalOperation.__table__.c.kind == ('capacity_carrier_observe' if work_capture is None else 'capacity_work_approve'))).mappings().one_or_none()
    if saved is None or not isinstance(saved['result'], dict):
        raise ValueError('physical_origin_historical_actual_observe_required')
    if work_capture is None:
        if full_evidence:
            raise ValueError('physical_origin_historical_evidence_mode')
        frozen_units = saved['result'].get('after', {}).get('location_units', [])
    else:
        from modules.wms.placement_service import WORK_PRIVATE, validate_work_envelope
        private = saved['result'].get(WORK_PRIVATE)
        if (type(private) is not dict or private.get('protocol') != 2
                or canonical(private.get('location_capture')) != canonical(work_capture)):
            raise ValueError('physical_origin_historical_work_capture_binding')
        validate_work_envelope({key: saved[key] for key in ('organization_id', 'warehouse_id', 'operation_id',
            'kind', 'request_hash', 'actor', 'result')}, private['policy_row'])
        frozen_units = [item for frame in work_capture['location_frames'] for item in frame['units']]
    frozen = [item for item in frozen_units if item.get('id') == unit['id']]
    if len(frozen) != 1 or canonical(frozen[0]) != canonical(unit):
        raise ValueError('physical_origin_historical_observe_unit_binding')
    root, nested = connection.get_transaction(), connection.get_nested_transaction()
    owner_ref = connection.info.get("wms_native_historical_owner_v1")
    owner = None if owner_ref is None else owner_ref()
    if owner is None or not owner.in_transaction():
        raise ValueError("physical_origin_historical_caller_untracked")
    def frozen_guard(value):
        # Freeze container values while keeping transaction/table/callback
        # identities. Comparing a reference to the same mutable dict would not
        # detect a caller obligation being edited by a nested read hook.
        if type(value) is dict:
            return ('dict', tuple((frozen_guard(key), frozen_guard(item)) for key, item in value.items()))
        if type(value) in (tuple, list):
            return (type(value), tuple(frozen_guard(item) for item in value))
        if type(value) in (set, frozenset):
            return (type(value), frozenset(frozen_guard(item) for item in value))
        if value is None or type(value) in (str, int, float, bool, bytes):
            return (type(value), value)
        return ('identity', id(value))
    owner_state = None if owner is None else (owner.get_transaction(), owner.get_nested_transaction(),
        frozenset(owner.new), frozenset(owner.dirty), frozenset(owner.deleted),
        frozen_guard(owner.info.get("wms.g06.profile_guard.v1")))
    context = _HistoricalOriginContext(organization_id, warehouse_id, operation_pk,
        tuple((item['id'], canonical(item)) for item in frozen_units))
    allowed_models = (PhysicalOperation, PhysicalLedger, WarehouseControl, AssemblyConsumption,
        AssemblyInputLink, AssemblyOutputIdentity, AssemblyExecution, AssemblyProcessRun, AssemblySpecification)
    allowed = {model.__table__ for model in allowed_models}

    class Reads:
        no_autoflush = nullcontext()

        def in_transaction(self):
            return connection.in_transaction()

        def statement(self, statement):
            if type(statement) is not Select or statement._for_update_arg is not None:
                raise ValueError("physical_origin_historical_read_only")
            found = set()
            for node in visitors.iterate(statement):
                table = getattr(node, 'table', None)
                if table is not None:
                    table = table._deannotate()
                    if table not in allowed:
                        raise ValueError("physical_origin_historical_live_table_forbidden")
                    found.add(table)
            if not found:
                raise ValueError("physical_origin_historical_unbounded_read")
            if PhysicalOperation.__table__ in found and PhysicalLedger.__table__ not in found:
                statement = statement.where(PhysicalOperation.id < context.before_pk)
            return statement

        async def scalar(self, statement):
            return native.scalar(self.statement(statement))

        async def scalars(self, statement):
            return native.scalars(self.statement(statement))

        async def execute(self, statement):
            return native.execute(self.statement(statement))

        async def get(self, model, key, **options):
            if model not in allowed_models or model in (StockUnit, PickedAllocation):
                raise ValueError("physical_origin_historical_live_identity_forbidden")
            row = native.get(model, key, **options)
            if model is PhysicalOperation and row is not None:
                context.operation(row)
            return row

        async def connection(self):
            return self

        async def run_sync(self, function):
            return function(connection)

    async def prove():
        facade = Reads()
        frontier, records = await physical_unit_frontier(facade, organization_id, unit['id'], historical=context)
        value = dict(unit)
        value['qty'], value['reserved'] = Decimal(value['qty']), Decimal(value['reserved'])
        for name in ('last_checked', 'next_service'):
            value[name] = None if value[name] is None else date.fromisoformat(value[name])
        value['arrived_at'] = None if value['arrived_at'] is None else datetime.fromisoformat(value['arrived_at'])
        try:
            origin = await _prove_unit_boundary(facade, organization_id, unit['id'], _origin_frontier_tokens(frontier),
                current_unit=None, allow_zero=True, historical=context)
            if (origin['qty'] != Decimal(unit['qty'])
                    or origin['dimensions'] != dimensions(SimpleNamespace(**value))
                    or await context.identity(facade, unit['id']) != unit.get('assembly_output_id')):
                raise _OriginUnproven
        except _OriginUnproven:
            if not full_evidence:
                raise
            origin = None  # Unknown remains explicit evidence; never free/zero.
        if not full_evidence:
            return origin
        from modules.wms.physical_stock import decimal_text
        projected = None if origin is None else {**origin, 'qty': decimal_text(origin['qty']),
            'anchor_prior': sorted(origin['anchor_prior'])}
        return {'origin': projected, 'frontier': frontier, 'history': records}

    native = Session(bind=connection, autoflush=False, expire_on_commit=False,
        join_transaction_mode='rollback_only', info={'wms_native_historical_reader_v1': True})
    coroutine = prove()
    try:
        try:
            coroutine.send(None)
        except StopIteration as done:
            answer = done.value
        else:
            raise ValueError("physical_origin_historical_suspension_forbidden")
    except _OriginUnproven as exc:
        raise ValueError("physical_origin_historical_unproven") from exc
    finally:
        coroutine.close()
        native.close()  # rollback_only never owns or settles the caller transaction.
        if (connection.get_transaction() is not root or connection.get_nested_transaction() is not nested
                or not connection.in_transaction()):
            raise ValueError("physical_origin_historical_transaction_changed")
        if owner is not None and (owner.get_transaction(), owner.get_nested_transaction(),
                frozenset(owner.new), frozenset(owner.dirty), frozenset(owner.deleted),
                frozen_guard(owner.info.get("wms.g06.profile_guard.v1"))) != owner_state:
            raise ValueError("physical_origin_historical_caller_changed")
    return answer


def _measurement_observation_receipt(connection, organization_id, warehouse_id, observation, *, measurement_pk=None):
    # Before insertion, only the persisted source PK exists. Historical/root
    # validation supplies the real assigned measurement PK after the final flush.
    from modules.wms.physical_schemas import CarrierObserve, CarrierMetadataResult
    from modules.wms.physical_stock import canonical, digest
    table = PhysicalOperation.__table__
    row = connection.execute(select(table).where(table.c.organization_id == organization_id, table.c.warehouse_id == warehouse_id, table.c.operation_id == observation["observation_operation_id"])).mappings().one_or_none()
    if row is None or row["kind"] != "capacity_carrier_observe" or type(row["result"]) is not dict:
        raise ValueError("measurement_observation_receipt_missing_or_kind")
    if type(row["id"]) is not int or row["id"] <= 0 or (measurement_pk is not None and
        (type(measurement_pk) is not int or measurement_pk <= 0 or row["id"] >= measurement_pk)):
        raise ValueError("measurement_observation_causal_order")
    result = row["result"]
    required = {"protocol", "operation_id", "kind", "warehouse_id"} | set(CarrierMetadataResult.model_fields)
    if set(result) != required or result["kind"] != row["kind"] or result["operation_id"] != row["operation_id"] or result["warehouse_id"] != warehouse_id or type(result["protocol"]) is not int or result["protocol"] != 2:
        raise ValueError("measurement_observation_receipt_shape")
    CarrierMetadataResult.model_validate({key:result[key] for key in CarrierMetadataResult.model_fields})
    request = CarrierObserve.model_validate(result["reported"])
    if (canonical(capacity_request_snapshot(request)) != canonical(result["reported"]) or digest(result["reported"]) != row["request_hash"]
        or row["request_hash"] != observation["observation_request_hash"] or result["carrier_id"] != observation["carrier_id"]
        or result["content_revision"] != observation["content_revision"] or result["content_digest"] != observation["content_digest"]
        or canonical(result["after"]) != canonical(observation["after"]) or request.carrier_id != UUID(observation["carrier_id"])
        or str(request.operation_id) != row["operation_id"] or request.warehouse_id != warehouse_id):
        raise ValueError("measurement_observation_receipt_correspondence")
    _measurement_observation_transition(connection, row, request, result)
    # Real historical producer provenance, not a digest-only after-frame assertion.
    from modules.wms.physical_stock import dimensions
    from types import SimpleNamespace
    reported_count = None if request.approved_count_id is None else str(request.approved_count_id)
    count_receipts = {}
    if reported_count is not None:
        count_table = PhysicalCount.__table__
        count = connection.execute(select(count_table).where(count_table.c.id == reported_count,
            count_table.c.warehouse_id == warehouse_id)).mappings().one_or_none()
        if count is None or count["status"] != "approved" or not count["approved_by"] or count["approved_at"] is None or request.location_id not in count["completed_locations"]:
            raise ValueError("measurement_observation_approved_count_missing")
        candidates = connection.execute(select(table).where(table.c.organization_id == organization_id,
            table.c.warehouse_id == warehouse_id, table.c.kind == "count_approve", table.c.id < row["id"])).mappings().all()
        count_receipts = {item["operation_id"]: item for item in candidates if type(item["result"]) is dict
            and item["result"].get("count_id") == reported_count and item["result"].get("status") == "approved"}
        if not count_receipts:
            raise ValueError("measurement_observation_count_receipt_missing")
    source_ids = {str(item) for item in request.source_operation_ids}
    selected_rows = connection.execute(select(table).where(table.c.organization_id == organization_id,
        table.c.warehouse_id == warehouse_id, table.c.operation_id.in_(sorted(source_ids)), table.c.id < row["id"])).mappings().all()
    from modules.wms.placement_models import placement_operation_indicator
    from modules.wms.placement_service import validate_placement_receipt, placement_ledger_dimensions
    placement_sources = {}
    for item in selected_rows:
        if placement_operation_indicator(item['kind'], item['result']):
            if item['kind'] != 'capacity_placement_complete':
                raise ValueError('measurement_observation_placement_source_kind')
            placement_sources[item['operation_id']] = validate_placement_receipt(connection, dict(item))
    by_operation = {item["operation_id"]: item for item in selected_rows} | count_receipts
    if source_ids - set(by_operation):
        raise ValueError("measurement_observation_source_scope")
    after = observation["after"]
    members = {item["id"]: item for item in after["members"]}
    location_units = {item["id"]:item for item in after["location_units"]}
    if type(result["source_proofs"]) is not dict or set(result["source_proofs"]) - set(location_units):
        raise ValueError("measurement_observation_source_proof_members")
    used = set()
    for unit_id in set(members) | set(result["source_proofs"]):
        unit = location_units[unit_id]
        refs = result["source_proofs"].get(unit_id, [])
        if type(refs) is not list or (unit_id in members and Decimal(unit["qty"]) > 0 and not refs):
            raise ValueError("measurement_observation_positive_provenance_missing")
        dimension_row = {**unit, "last_checked":None if unit["last_checked"] is None else date.fromisoformat(unit["last_checked"]),
            "next_service":None if unit["next_service"] is None else date.fromisoformat(unit["next_service"]),
            "arrived_at":None if unit["arrived_at"] is None else datetime.fromisoformat(unit["arrived_at"])}
        expected_dimensions = dimensions(SimpleNamespace(**dimension_row))
        for ref in refs:
            if type(ref) is not dict or canonical(ref.get("dimensions")) != canonical(expected_dimensions):
                raise ValueError("measurement_observation_provenance_dimensions")
            if set(ref) == {"operation_id", "ledger_id", "dimensions"}:
                operation = by_operation.get(ref["operation_id"])
                ledger = connection.execute(select(PhysicalLedger.__table__).where(PhysicalLedger.__table__.c.id == ref["ledger_id"])).mappings().one_or_none()
                if (type(ref["ledger_id"]) is not int or operation is None or ledger is None or ledger["operation_pk"] != operation["id"]
                    or ledger["unit_id"] != unit_id or (ledger["qty_delta"] <= 0 and Decimal(unit["qty"]) > 0)
                    or canonical(placement_ledger_dimensions(connection, ledger, dict(operation), before_pk=row['id'])
                        if ref['operation_id'] in placement_sources else ledger["dimensions"]) != canonical(expected_dimensions)):
                    raise ValueError("measurement_observation_ledger_provenance")
                if ref['operation_id'] in placement_sources:
                    _historical_observed_origin(connection, organization_id, warehouse_id, row['id'], unit)
                used.add(ref["operation_id"])
            elif set(ref) == {"count_id", "count_line_id", "operation_ids", "dimensions"}:
                line = connection.execute(select(PhysicalCountLine.__table__).where(PhysicalCountLine.__table__.c.id == ref["count_line_id"])).mappings().one_or_none()
                if (type(ref["count_line_id"]) is not int or ref["count_id"] != reported_count or line is None
                    or line["count_id"] != reported_count or line["unit_id"] != unit_id or line["location_id"] != request.location_id
                    or line["qty"] != Decimal(unit["qty"]) or canonical(line["dimensions"]) != canonical(expected_dimensions)
                    or type(ref["operation_ids"]) is not list or not ref["operation_ids"] or set(ref["operation_ids"]) != set(count_receipts)):
                    raise ValueError("measurement_observation_count_line_provenance")
                used.update(ref["operation_ids"])
            else:
                raise ValueError("measurement_observation_provenance_shape")
    if not any(Decimal(item["qty"]) > 0 for item in members.values()) and not request.member_unit_ids:
        if reported_count is not None:
            used.update(count_receipts)
        else:
            ledgers = connection.execute(select(PhysicalLedger.__table__).where(
                PhysicalLedger.__table__.c.operation_pk.in_([item["id"] for item in selected_rows]),
                PhysicalLedger.__table__.c.unit_id.in_(sorted(members)))).mappings().all()
            for ledger in ledgers:
                member = members[ledger["unit_id"]]
                if canonical(ledger["dimensions"]) == canonical({key:member[key] for key in ledger["dimensions"]}):
                    used.update(item["operation_id"] for item in selected_rows if item["id"] == ledger["operation_pk"])
            for operation_id, proof in placement_sources.items():
                if proof['destination_after']['carrier']['id'] == observation['carrier_id']:
                    for member in proof['destination_after']['members']:
                        if Decimal(member['qty']) != 0 or member['id'] not in location_units:
                            raise ValueError('measurement_observation_empty_placement_members')
                        _historical_observed_origin(connection, organization_id, warehouse_id, row['id'], location_units[member['id']])
                    used.add(operation_id)
            if not used:
                raise ValueError("measurement_observation_empty_provenance_missing")
    for operation_id, proof in placement_sources.items():
        zeros = [member for member in proof['destination_after']['members'] if Decimal(member['qty']) == 0]
        for member in zeros:
            if member['id'] in location_units:
                _historical_observed_origin(connection, organization_id, warehouse_id, row['id'], location_units[member['id']])
                used.add(operation_id)
        if not proof['destination_after']['members'] and not request.member_unit_ids:
            if proof['destination_after']['carrier']['id'] != observation['carrier_id']:
                raise ValueError('measurement_observation_empty_placement_carrier')
            used.add(operation_id)
    if source_ids - used:
        raise ValueError("measurement_observation_unrelated_source")
    return result


def _measurement_db_row(model, row):
    """Existing native serializers preserve every column and timestamp convention."""
    from modules.wms.physical_stock import canonical, timestamp_text
    if model is StockUnit:
        require_current_on_hand(row)
    answer = (
        {name: row[name] for name in STOCK_UNIT_MEASUREMENT_V1_COLUMNS}
        if model is StockUnit else dict(row)
    )
    columns = (
        (model.__table__.columns[name] for name in STOCK_UNIT_MEASUREMENT_V1_COLUMNS)
        if model is StockUnit else model.__table__.columns
    )
    for column in columns:
        value = answer[column.name]
        if value is not None and isinstance(column.type, Numeric):
            answer[column.name] = _cap_fixed(value)
        elif isinstance(value, datetime):
            answer[column.name] = timestamp_text(value) if model is StockUnit else _cap_timestamp_text(value)
        elif isinstance(value, date):
            answer[column.name] = value.isoformat()
    return json.loads(canonical(answer))


def measurement_current_binding(connection, proof, *, require_head=True):
    _measurement_current_binding(connection, proof, require_head=require_head)


def _measurement_current_binding(connection, proof, *, require_head=True, verified_frames=None):
    from modules.wms.physical_models import LoadResource, PhysicalLocation, _cap_chain, _cap_full_snapshot
    from modules.wms.physical_stock import canonical, digest
    from modules.wms.placement_policy_codec import measured_context_proof
    measured_context_proof(proof)
    org, wh = proof["organization_id"], proof["warehouse_id"]
    if require_head:
        head = measurement_state_head(connection, org, wh)
        if head is None or head["operation_id"] != proof["operation_id"] or head["digest"] != proof["digest"]:
            raise ValueError("measurement_current_head_stale")
    for binding in proof["profile_bindings"]:
        table = PlacementProfile.__table__
        location_id = binding["snapshot"]["profile"]["location_id"]
        current = connection.execute(select(table).where(table.c.organization_id == org, table.c.warehouse_id == wh, table.c.location_id == location_id).order_by(table.c.revision.desc(),table.c.id)).mappings().first()
        if current is None or current["id"] != binding["profile_id"] or current["sealed"] is not True or current["digest"] != binding["digest"] or current["canonical_payload"] != binding["canonical_payload"]:
            raise ValueError("measurement_current_profile_stale")
        _cap_full_snapshot(connection, dict(current))
        _cap_current_binding(connection, dict(current))
    for expected in proof["topology"]["locations"]:
        table = PhysicalLocation.__table__
        row = connection.execute(select(table).where(table.c.warehouse_id == wh, table.c.location_id == expected["location_id"])).mappings().one_or_none()
        if row is None or row["resource_id"] != expected["resource_id"] or row["resource_mapping_revision"] != expected["resource_mapping_revision"] or _cap_chain(connection,wh,row["resource_id"]) != expected["ancestor_binding"]:
            raise ValueError("measurement_current_location_mapping_stale")
    for expected in proof["topology"]["resources"]:
        table = LoadResource.__table__
        row = connection.execute(select(table).where(table.c.warehouse_id == wh, table.c.id == expected["id"])).mappings().one_or_none()
        if row is None or canonical(_measurement_db_row(LoadResource,row)) != canonical(expected):
            raise ValueError("measurement_current_topology_stale")
    carriers = [_measurement_db_row(PhysicalCarrier,row) for row in connection.execute(select(PhysicalCarrier.__table__).where(PhysicalCarrier.__table__.c.warehouse_id == wh).order_by(PhysicalCarrier.__table__.c.id)).mappings().all()]
    positions = [_measurement_db_row(CarrierPosition,row) for row in connection.execute(select(CarrierPosition.__table__).where(CarrierPosition.__table__.c.warehouse_id == wh).order_by(CarrierPosition.__table__.c.carrier_id,CarrierPosition.__table__.c.ordinal)).mappings().all()]
    raw_units = connection.execute(select(StockUnit.__table__).where(StockUnit.__table__.c.warehouse_id == wh).order_by(StockUnit.__table__.c.id).with_for_update()).mappings().all()
    units = [_measurement_db_row(StockUnit,row) for row in _addressable_capacity_db_rows(connection, org, raw_units)]
    for expected in proof["carrier_observations"]:
        carrier = next((row for row in carriers if row["id"] == expected["carrier_id"]),None)
        if carrier is None:
            raise ValueError("measurement_current_carrier_missing")
        table = StockUnit.__table__
        foreign = connection.execute(select(table.c.id).where(table.c.carrier_id == carrier["id"],
            (table.c.warehouse_id != wh) | (table.c.location_id != carrier["location_id"]))).first()
        if foreign is not None:
            raise ValueError("measurement_current_carrier_member_scope_corrupt")
        actual = _observation_frame_values(carrier,positions,units,carrier["location_id"])
        expected_frame = expected["after"] if verified_frames is None else verified_frames[expected["carrier_id"]]
        expected_digest = expected["content_digest"] if verified_frames is None else digest(expected_frame)
        if canonical(actual) != canonical(expected_frame) or digest(actual) != expected_digest:
            raise ValueError("measurement_current_full_observation_stale")
    request = proof["request"]
    if request["routes"]["state"] == "confirmed":
        by_unit = {row["id"]:row for row in units}
        for route in request["routes"]["value"]:
            if route["corridors"]["state"] == "confirmed":
                for corridor in route["corridors"]["value"]:
                    if any(unit_id not in by_unit or by_unit[unit_id]["location_id"] != corridor["location_id"] for unit_id in corridor["protected_unit_ids"]):
                        raise ValueError("measurement_protected_unit_scope_missing")
    _measurement_geometry(proof)

    _measurement_support_packaging(connection,proof,current=True)




def equipment_state_heads(connection, organization_id, warehouse_id):
    """Decode every scoped candidate before selecting equipment or validity."""
    from sqlalchemy import or_

    from modules.wms.physical_schemas import EQUIPMENT_STATE_KIND
    require_capacity_read_committed(connection)
    table = PhysicalOperation.__table__
    rows = connection.execute(select(table).where(
        table.c.organization_id == organization_id, table.c.warehouse_id == warehouse_id,
        or_(table.c.kind == EQUIPMENT_STATE_KIND, table.c.result["kind"].as_string() == EQUIPMENT_STATE_KIND),
    ).order_by(table.c.id)).mappings().all()
    heads = {}
    for row in rows:
        value = equipment_operation_receipt(row, connection)
        key = value["equipment_resource_id"]
        previous = heads.get(key)
        revision = 0 if previous is None else previous["state_revision"]
        previous_id = None if previous is None else previous["operation_id"]
        if value["state_revision"] != revision + 1 or value["previous_operation_id"] != previous_id:
            raise ValueError("equipment_state_history_branch_or_gap")
        heads[key] = value
    return heads


def equipment_current_binding(connection, value):
    """Caller owns WH mutex. Reuse existing full sealed profile serialization."""
    from modules.wms.physical_models import (
        LoadResource,
        _cap_chain,
        _cap_full_snapshot,
        _cap_profile_shape,
    )
    org, wh, resource_id = value["organization_id"], value["warehouse_id"], value["equipment_resource_id"]
    resource = connection.execute(select(LoadResource.__table__).where(
        LoadResource.__table__.c.warehouse_id == wh, LoadResource.__table__.c.id == resource_id,
    )).mappings().one_or_none()
    if resource is None or resource["kind"] != "equipment":
        raise ValueError("equipment_resource_scope_or_kind")
    actual_resource = {"resource_id": resource["id"], "warehouse_id": wh, "kind": resource["kind"],
        "parent_id": resource["parent_id"], "topology_revision": resource["topology_revision"],
        "ancestor_binding": _cap_chain(connection, wh, resource_id)}
    if actual_resource != value["resource_binding"]:
        raise ValueError("equipment_resource_binding_stale")
    table = PlacementProfile.__table__
    profile = connection.execute(select(table).where(
        table.c.organization_id == org, table.c.warehouse_id == wh, table.c.resource_id == resource_id,
        table.c.packaging_id.is_(None), table.c.location_id.is_(None),
    ).order_by(table.c.revision.desc(), table.c.id)).mappings().first()
    if profile is None or profile["sealed"] is not True:
        raise ValueError("equipment_current_profile_missing_or_unsealed")
    profile = dict(profile)
    _cap_profile_shape(connection, profile, database_value=True)
    _cap_full_snapshot(connection, profile)
    _cap_current_binding(connection, profile)
    actual_profile = {"profile_id": profile["id"], "revision": profile["revision"], "digest": profile["digest"],
        "canonical_payload": profile["canonical_payload"], "bound_mapping_revision": profile["bound_mapping_revision"],
        "ancestor_binding": profile["ancestor_binding"]}
    if actual_profile != value["profile_binding"]:
        raise ValueError("equipment_profile_binding_stale")


async def observe_equipment_state(session, warehouse, data, actor_uid, actor_sub):
    from modules.wms.physical_models import _cap_chain
    from modules.wms.physical_schemas import equipment_receipt, normalize_equipment_request
    from modules.wms.physical_stock import canonical, digest, reject
    resources, _, profiles, slots = await capacity_metadata_rows(session, warehouse.warehouse_id)
    resource_id = str(data.equipment_resource_id)
    resource = next((row for row in resources if row.id == resource_id), None)
    if resource is None or resource.kind != "equipment" or resource.topology_revision != data.expected_topology_revision:
        reject("equipment_resource_scope_kind_or_revision")
    candidates = [row for row in profiles if row.resource_id == resource_id and row.packaging_id is None and row.location_id is None]
    profile = max(candidates, key=lambda row: row.revision, default=None)
    if (profile is None or not profile.sealed or profile.organization_id != warehouse.organization_id
        or profile.id != str(data.equipment_profile_id) or profile.revision != data.expected_profile_revision
        or profile.digest != data.expected_profile_digest):
        reject("equipment_current_profile_missing_or_stale")
    check_profile_snapshot(profile, [row for row in slots if row.profile_id == profile.id])
    connection = await session.connection()
    chain = await connection.run_sync(lambda conn: _cap_chain(conn, warehouse.warehouse_id, resource_id))
    heads = await connection.run_sync(lambda conn: equipment_state_heads(conn, warehouse.organization_id, warehouse.warehouse_id))
    previous = heads.get(resource_id)
    revision = 0 if previous is None else previous["state_revision"]
    if revision != data.expected_state_revision or revision >= 2147483647:
        reject("equipment_state_revision_stale_or_exhausted")
    approved_at = datetime.now(timezone.utc)
    if not data.valid_from <= approved_at < data.valid_until:
        reject("equipment_observation_outside_validity")
    request = normalize_equipment_request(data)
    value = {"protocol": 2, "operation_id": str(data.operation_id), "kind": data.kind,
        "organization_id": warehouse.organization_id, "warehouse_id": warehouse.warehouse_id,
        "equipment_resource_id": resource_id, "state_revision": revision + 1,
        "previous_operation_id": None if previous is None else previous["operation_id"],
        "actor_user_id": actor_uid, "actor_sub": actor_sub,
        "approved_at": approved_at.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "request_hash": digest({"organization_id": warehouse.organization_id, "actor_user_id": actor_uid, "actor_sub": actor_sub, "request": request}),
        "request": request,
        "resource_binding": {"resource_id": resource.id, "warehouse_id": warehouse.warehouse_id, "kind": resource.kind,
            "parent_id": resource.parent_id, "topology_revision": resource.topology_revision, "ancestor_binding": chain},
        "profile_binding": {"profile_id": profile.id, "revision": profile.revision, "digest": profile.digest,
            "canonical_payload": profile.canonical_payload, "bound_mapping_revision": profile.bound_mapping_revision,
            "ancestor_binding": profile.ancestor_binding}}
    value["canonical_payload"] = canonical(value)
    value["digest"] = digest({key: item for key, item in value.items() if key not in {"canonical_payload", "digest"}})
    equipment_receipt(value)
    await connection.run_sync(lambda conn: equipment_current_binding(conn, value))
    return value



async def approve_measured_context(session, warehouse, data, approver):
    """Read-only physical footprint; executor persists exactly one metadata receipt."""
    from uuid import uuid4
    from modules.wms.physical_models import PhysicalTask, PhysicalHold, PickedAllocation, _cap_chain
    from modules.wms.placement_models import PlacementAssignment, PlacementClaim
    from modules.wms.physical_schemas import normalize_measurements_request, MEASUREMENTS_PRIVATE_KEY
    from modules.wms.physical_stock import canonical, digest, reject, protect_blind_projection
    from modules.wms.placement_policy_codec import measured_context_proof
    org, wh = warehouse.organization_id, warehouse.warehouse_id
    resources, places, profiles, slots = await capacity_metadata_rows(session, wh)
    carriers = list((await session.scalars(select(PhysicalCarrier).where(PhysicalCarrier.warehouse_id==wh).order_by(PhysicalCarrier.id).with_for_update().execution_options(populate_existing=True))).all())
    positions = list((await session.scalars(select(CarrierPosition).where(CarrierPosition.warehouse_id==wh).order_by(CarrierPosition.carrier_id,CarrierPosition.ordinal).with_for_update().execution_options(populate_existing=True))).all())
    for model, ordering in ((PhysicalTask,(PhysicalTask.id,)),(PlacementAssignment,(PlacementAssignment.task_id,)),(PlacementClaim,(PlacementClaim.task_id,PlacementClaim.ordinal)),(PhysicalHold,(PhysicalHold.id,))):
        (await session.scalars(select(model).where(model.warehouse_id==wh).order_by(*ordering).with_for_update().execution_options(populate_existing=True))).all()
    (await session.scalars(select(PickedAllocation).join(PhysicalHold,PhysicalHold.id==PickedAllocation.hold_id).where(PhysicalHold.warehouse_id==wh).order_by(PickedAllocation.id).with_for_update(of=PickedAllocation).execution_options(populate_existing=True))).all()
    units = list((await session.scalars(select(StockUnit).where(StockUnit.warehouse_id==wh).order_by(StockUnit.id).with_for_update().execution_options(populate_existing=True))).all())
    await protect_blind_projection(session,[wh])
    units = await _addressable_capacity_units(session, org, units)
    connection = await session.connection()
    head = await connection.run_sync(lambda conn: measurement_state_head(conn,org,wh))
    if data.expected_revision != (0 if head is None else head["revision"]) or (None if data.previous_operation_id is None else str(data.previous_operation_id)) != (None if head is None else head["operation_id"]) or data.expected_revision == 2147483647:
        reject("measurement_revision_or_previous_stale",409)
    request = normalize_measurements_request(data)
    by_place = {row.location_id:row for row in places}
    resolved = []
    for frame in request["frames"]:
        if frame["anchor_location_id"] not in by_place:
            reject("measurement_anchor_scope_missing",409)
        if frame["kind"] == "new":
            frame_id, resolution = str(uuid4()), {"kind":"new"}
        else:
            table = PhysicalOperation.__table__
            prior = (await session.execute(select(table).where(table.c.organization_id==org,table.c.warehouse_id==wh,table.c.operation_id==frame["approval_operation_id"]))).mappings().one_or_none()
            if prior is None:
                reject("measurement_reuse_approval_missing",409)
            await connection.run_sync(lambda conn: measurement_receipt(prior["result"],prior,conn))
            prior_proof = prior["result"][MEASUREMENTS_PRIVATE_KEY]
            match = [row for row in prior_proof["resolved_frames"] if row["frame_id"]==frame["frame_id"] and all(row[key]==frame[key] for key in ("anchor_location_id","datum_mark","axes"))]
            if not match:
                reject("measurement_reuse_datum_mismatch",409)
            frame_id = frame["frame_id"]
            resolution = {"kind":"reuse","frame_id":frame_id,"approval_operation_id":frame["approval_operation_id"],"approval_digest":prior_proof["digest"]}
        resolved.append({key:frame[key] for key in ("frame_key","anchor_location_id","datum_mark","axes","source")} | {"frame_id":frame_id,"resolution":resolution})
    frame_by_key = {row["frame_key"]:row for row in resolved}
    bindings = []
    for declaration in request["profile_frames"]:
        candidates = [row for row in profiles if row.id==declaration["profile_id"]]
        profile = candidates[0] if len(candidates)==1 else None
        if profile is None or profile.organization_id!=org or profile.location_id is None or not profile.sealed or profile.digest!=declaration["expected_digest"] or declaration["frame_key"] not in frame_by_key:
            reject("measurement_location_profile_missing_or_stale",409)
        current = max((row for row in profiles if row.organization_id==org and row.location_id==profile.location_id),key=lambda row:row.revision)
        if current.id != profile.id:
            reject("measurement_location_profile_not_current",409)
        children = [row for row in slots if row.profile_id==profile.id]
        check_profile_snapshot(profile,children)
        bindings.append({"profile_id":profile.id,"revision":profile.revision,"digest":profile.digest,"sealed":True,"canonical_payload":profile.canonical_payload,
            "snapshot":json.loads(profile.canonical_payload),"frame_key":declaration["frame_key"],"frame_id":frame_by_key[declaration["frame_key"]]["frame_id"]})
    required_locations = {row["anchor_location_id"] for row in resolved} | {row["snapshot"]["profile"]["location_id"] for row in bindings}
    if request["routes"]["state"] == "confirmed":
        for row in request["routes"]["value"]:
            required_locations.update(row["phase_location_ids"])
    if request["supports"]["state"] == "confirmed":
        required_locations.update(row["location_id"] for row in request["supports"]["value"])
    location_bindings=[]
    for location_id in sorted(required_locations):
        place=by_place.get(location_id)
        if place is None or place.resource_id is None or place.resource_mapping_revision<=0:
            reject("measurement_datum_location_mapping_unknown",409)
        chain = await connection.run_sync(lambda conn:_cap_chain(conn,wh,place.resource_id))
        location_bindings.append({"warehouse_id":wh,"location_id":location_id,"resource_id":place.resource_id,"resource_mapping_revision":place.resource_mapping_revision,"ancestor_binding":chain})
    resource_ids={item["resource_id"] for row in location_bindings for item in row["ancestor_binding"]}
    topology={"resources":[_observation_row(row) for row in sorted(resources,key=lambda row:row.id) if row.id in resource_ids],"locations":location_bindings}
    requested_observations={}
    if request["supports"]["state"]=="confirmed":
        for row in request["supports"]["value"]:
            if row["bearing"]["kind"]=="carrier":
                bearing=row["bearing"]
                selected=(bearing["observation_operation_id"],bearing["expected_content_revision"],bearing["expected_content_digest"])
                if bearing["carrier_id"] in requested_observations and requested_observations[bearing["carrier_id"]]!=selected:
                    reject("measurement_conflicting_carrier_observation",409)
                requested_observations[bearing["carrier_id"]]=selected
    if request["carrier_support_relations"]["state"]=="confirmed":
        for row in request["carrier_support_relations"]["value"]:
            selected=(row["observation_operation_id"],row["expected_content_revision"],row["expected_content_digest"])
            if row["carrier_id"] in requested_observations and requested_observations[row["carrier_id"]]!=selected:
                reject("measurement_conflicting_carrier_observation",409)
            requested_observations[row["carrier_id"]]=selected
    observations=[]
    for carrier_id,(operation_id,revision,content_digest) in sorted(requested_observations.items()):
        carrier=next((row for row in carriers if row.id==carrier_id),None)
        operation=await session.scalar(select(PhysicalOperation).where(PhysicalOperation.organization_id==org,PhysicalOperation.warehouse_id==wh,PhysicalOperation.operation_id==operation_id))
        if carrier is None or operation is None or type(operation.result) is not dict:
            reject("measurement_carrier_observation_missing",409)
        after=_observation_frame(carrier,positions,units,carrier.location_id)
        observation={"carrier_id":carrier_id,"observation_operation_id":operation_id,"observation_request_hash":operation.request_hash,"content_revision":revision,"content_digest":content_digest,"after":after}
        await connection.run_sync(lambda conn:_measurement_observation_receipt(conn,org,wh,observation))
        observations.append(observation)
    proof={"protocol":1,"organization_id":org,"warehouse_id":wh,"operation_id":request["operation_id"],"kind":request["kind"],"revision":data.expected_revision+1,
        "previous_operation_id":request["previous_operation_id"],"request_hash":digest({"organization_id":org,"actor_user_id":approver["user_id"],"actor_sub":approver["sub"],"request":request}),
        "request":request,"approver":approver,"resolved_frames":resolved,"profile_bindings":bindings,"topology":topology,"carrier_observations":observations}
    proof["canonical_payload"]=canonical(proof)
    proof["digest"]=digest({key:value for key,value in proof.items() if key not in {"canonical_payload","digest"}})
    measured_context_proof(proof)
    _measurement_geometry(proof)
    await connection.run_sync(lambda conn:measurement_current_binding(conn,proof,require_head=False))
    return {**measurement_ack(proof),MEASUREMENTS_PRIVATE_KEY:proof}
