"""ERP physical stock. Legacy movement history is deliberately not an opening balance.

All writes are serialized by WarehouseControl (one or two operators). Lots remain
owned by integrations; batch_id is a reference through StockGateway, not a cross-
module foreign key. Quantities are always in the SKU's base unit.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Index,
    Numeric,
    String,
    Table,
    UniqueConstraint,
    event,
    func,
    inspect,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy.orm.attributes import NO_VALUE

from core.db.base import Base

QTY = Numeric(20, 6)


class WarehouseControl(Base):
    __tablename__ = "physical_warehouse"
    __table_args__ = (
        UniqueConstraint("organization_id", "onec_ref", name="physical_warehouse_onec_identity"),
        UniqueConstraint("organization_id", "warehouse_id", name="cap_warehouse_scope"),
        CheckConstraint("mode IN ('preparing', 'erp')", name="physical_warehouse_mode"),
        ForeignKeyConstraint(
            ["organization_id", "warehouse_id"],
            ["wms.warehouse_directory.organization_id", "wms.warehouse_directory.id"],
            name="cap_physical_warehouse_directory_scope",
        ),
        {"schema": "wms"},
    )
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("wms.warehouse_directory.id"), primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, index=True)
    onec_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    mode: Mapped[str] = mapped_column(String(16), default="preparing")
    opening_count_id: Mapped[str | None] = mapped_column(String(36))
    cutover_evidence: Mapped[dict | None] = mapped_column(JSON)
    cutover_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WarehouseResponsible(Base):
    """Current source-warehouse approver; immutable operations retain its history."""
    __tablename__ = "warehouse_responsible"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "warehouse_id"],
            ["wms.physical_warehouse.organization_id", "wms.physical_warehouse.warehouse_id"],
            name="responsible_warehouse_scope", ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "warehouse_id", "operation_id"],
            ["wms.physical_operation.organization_id", "wms.physical_operation.warehouse_id", "wms.physical_operation.operation_id"],
            name="responsible_operation_scope", ondelete="RESTRICT",
        ),
        CheckConstraint("revision BETWEEN 1 AND 2147483647", name="responsible_revision"),
        CheckConstraint("user_id BETWEEN 1 AND 2147483647", name="responsible_user_id"),
        CheckConstraint("length(subject) > 0 AND subject = trim(subject)", name="responsible_subject"),
        {"schema": "wms"},
    )
    organization_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer)
    subject: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean)
    revision: Mapped[int] = mapped_column(Integer)
    operation_id: Mapped[str] = mapped_column(String(36))


class PhysicalLocation(Base):
    __tablename__ = "physical_location"
    __table_args__ = (
        UniqueConstraint("warehouse_id", "code", name="physical_location_code"),
        UniqueConstraint("warehouse_id", "location_id", name="cap_location_scope"),
        CheckConstraint("capacity_qty IS NULL OR capacity_qty > 0", name="physical_location_capacity"),
        ForeignKeyConstraint(
            ["warehouse_id", "resource_id"],
            ["wms.physical_load_resource.warehouse_id", "wms.physical_load_resource.id"],
            name="cap_location_resource", ondelete="RESTRICT",
        ),
        CheckConstraint("resource_mapping_revision >= 0", name="cap_location_mapping_revision"),
        {"schema": "wms"},
    )
    location_id: Mapped[int] = mapped_column(ForeignKey("wms.location.id"), primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("wms.physical_warehouse.warehouse_id"), index=True)
    code: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    capacity_sku: Mapped[str | None] = mapped_column(String(64))
    capacity_qty: Mapped[Decimal | None] = mapped_column(QTY)
    load_evidence: Mapped[str] = mapped_column(String(1000), default="")
    resource_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    resource_mapping_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )


class StoragePolicy(Base):
    __tablename__ = "physical_policy"
    __table_args__ = (
        CheckConstraint("precision >= 0 AND precision <= 6", name="physical_policy_precision"),
        CheckConstraint("min_shelf_days >= 0", name="physical_policy_shelf"),
        CheckConstraint("max_age_days IS NULL OR max_age_days > 0", name="physical_policy_age"),
        CheckConstraint("maintenance_days IS NULL OR maintenance_days > 0", name="physical_policy_maintenance"),
        {"schema": "wms"},
    )
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("wms.physical_warehouse.warehouse_id"), primary_key=True)
    sku_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    precision: Mapped[int] = mapped_column(Integer, default=0)
    base_unit: Mapped[str] = mapped_column(String(16), default="шт")
    expiry_required: Mapped[bool] = mapped_column(Boolean, default=False)
    manufacture_required: Mapped[bool] = mapped_column(Boolean, default=False)
    min_shelf_days: Mapped[int] = mapped_column(Integer, default=0)
    max_age_days: Mapped[int | None] = mapped_column(Integer)
    maintenance_days: Mapped[int | None] = mapped_column(Integer)
    manufacturer_source: Mapped[str] = mapped_column(String(1000))
    storage_rule: Mapped[str] = mapped_column(String(1000), default="")
    warranty_rule: Mapped[str] = mapped_column(String(1000), default="")
    count_class: Mapped[str] = mapped_column(String(1), default="C")


class Packaging(Base):
    __tablename__ = "physical_packaging"
    __table_args__ = (
        UniqueConstraint("organization_id", "barcode", name="physical_packaging_barcode"),
        UniqueConstraint("organization_id", "id", name="cap_packaging_scope"),
        CheckConstraint("factor > 0", name="physical_packaging_factor"),
        {"schema": "wms"},
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, index=True)
    sku_code: Mapped[str] = mapped_column(String(64), ForeignKey("sku.code", name="fk_physical_packaging_sku_code", ondelete="RESTRICT"))
    barcode: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(128))
    factor: Mapped[Decimal] = mapped_column(QTY)
    sealed_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    evidence: Mapped[str] = mapped_column(String(1000))


# Frozen receipt layout: adding a DB column must not rewrite measurement v1.
STOCK_UNIT_MEASUREMENT_V1_COLUMNS = (
    "id", "assembly_output_id", "warehouse_id", "location_id", "sku_code",
    "batch_id", "characteristic", "purpose", "qty", "reserved", "quality",
    "packaging_id", "carrier_id", "package_state", "container", "last_checked",
    "next_service", "inspection_ref", "service_result", "external_operations",
    "arrived_at", "arrival_source",
)


class CurrentStockStateError(ValueError):
    """Current addressable stock cannot be proved from this row."""


def require_current_on_hand(unit):
    """Validate current rows before a legacy projection drops state information."""
    try:
        state, dispatch, cell = (
            (unit["stock_state"], unit["dispatch_operation_id"], unit["location_id"])
            if isinstance(unit, Mapping) else
            (unit.stock_state, unit.dispatch_operation_id, unit.location_id)
        )
    except (KeyError, AttributeError) as exc:
        raise CurrentStockStateError("current_stock_state_unproven") from exc
    if state != "on_hand" or dispatch is not None or type(cell) is not int or cell <= 0:
        raise CurrentStockStateError("current_stock_state_unproven")


def require_current_in_transit(unit):
    try:
        state, cell, carrier = unit.stock_state, unit.location_id, unit.carrier_id
        reserved, dispatch = unit.reserved, unit.dispatch_operation_id
    except AttributeError as exc:
        raise CurrentStockStateError("current_transit_state_unproven") from exc
    if (state != "in_transit" or cell is not None or carrier is not None
            or type(reserved) is not Decimal or reserved != 0 or type(dispatch) is not str):
        raise CurrentStockStateError("current_transit_state_unproven")
    try:
        valid = str(UUID(dispatch)) == dispatch
    except ValueError as exc:
        raise CurrentStockStateError("current_transit_state_unproven") from exc
    if not valid:
        raise CurrentStockStateError("current_transit_state_unproven")


class StockUnit(Base):
    """A distinguishable subset of a lot. Partial QC/service creates a new subset."""
    __tablename__ = "physical_unit"
    __table_args__ = (
        CheckConstraint("qty >= 0 AND reserved >= 0 AND reserved <= qty", name="physical_unit_nonnegative"),
        CheckConstraint("quality IN ('quarantine','released','blocked')", name="physical_unit_quality"),
        CheckConstraint("package_state IN ('loose','sealed','open')", name="physical_unit_package"),
        CheckConstraint("assembly_output_id IS NULL OR (qty IN (0,1) AND reserved IN (0,1))", name="physical_unit_assembly_whole"),
        CheckConstraint(
            "(stock_state = 'on_hand' AND location_id IS NOT NULL AND location_id > 0 AND dispatch_operation_id IS NULL) OR "
            "(stock_state = 'in_transit' AND location_id IS NULL AND carrier_id IS NULL "
            "AND reserved = 0 AND dispatch_operation_id IS NOT NULL)",
            name="physical_unit_stock_state",
        ),
        ForeignKeyConstraint(
            ["warehouse_id", "dispatch_operation_id"],
            ["wms.physical_operation.warehouse_id", "wms.physical_operation.operation_id"],
            name="physical_unit_dispatch", ondelete="RESTRICT",
            deferrable=True, initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["warehouse_id", "location_id"],
            ["wms.physical_location.warehouse_id", "wms.physical_location.location_id"],
            name="physical_unit_location_scope", ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["warehouse_id", "location_id", "carrier_id"],
            ["wms.physical_carrier.warehouse_id", "wms.physical_carrier.location_id", "wms.physical_carrier.id"],
            name="cap_unit_carrier", ondelete="RESTRICT",
            deferrable=True, initially="DEFERRED",
        ),
        {"schema": "wms"},
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    assembly_output_id: Mapped[str | None] = mapped_column(
        ForeignKey("wms.assembly_output_identity.id", ondelete="RESTRICT"), nullable=True, index=True,
    )
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("wms.physical_warehouse.warehouse_id"), index=True)
    location_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    stock_state: Mapped[str] = mapped_column(String(16), default="on_hand", server_default="on_hand", nullable=False)
    dispatch_operation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    sku_code: Mapped[str] = mapped_column(String(64), index=True)
    batch_id: Mapped[int] = mapped_column(Integer)
    characteristic: Mapped[str] = mapped_column(String(128), default="")
    purpose: Mapped[str] = mapped_column(String(128), default="")
    qty: Mapped[Decimal] = mapped_column(QTY)
    reserved: Mapped[Decimal] = mapped_column(QTY, default=Decimal("0"))
    quality: Mapped[str] = mapped_column(String(16), default="quarantine")
    packaging_id: Mapped[int | None] = mapped_column(ForeignKey("wms.physical_packaging.id"))
    carrier_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    package_state: Mapped[str] = mapped_column(String(16), default="loose")
    container: Mapped[str] = mapped_column(String(64), default="")
    last_checked: Mapped[date | None] = mapped_column(Date)
    next_service: Mapped[date | None] = mapped_column(Date)
    inspection_ref: Mapped[str] = mapped_column(String(1000), default="")
    service_result: Mapped[dict | None] = mapped_column(JSON)
    external_operations: Mapped[list] = mapped_column(JSON, default=list)
    arrived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    arrival_source: Mapped[str] = mapped_column(String(1000), default="erp_receive")


class PhysicalOperation(Base):
    """Immutable receipt for retries. Identifier is unique within the organization."""
    __tablename__ = "physical_operation"
    __table_args__ = (
        UniqueConstraint("organization_id", "operation_id", name="physical_operation_identity"),
        UniqueConstraint("warehouse_id", "operation_id", name="physical_operation_dispatch_scope"),
        UniqueConstraint(
            "organization_id", "warehouse_id", "operation_id", name="cap_operation_scope",
        ),
        {"schema": "wms"},
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer)
    operation_id: Mapped[str] = mapped_column(String(36))
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("wms.physical_warehouse.warehouse_id"))
    kind: Mapped[str] = mapped_column(String(32))
    request_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DeliveryBindingRevision(Base):
    """Immutable verified attribution of one delivery execution to warehouse tasks."""
    __tablename__ = "physical_delivery_binding"
    __table_args__ = (
        UniqueConstraint("binding_key", "revision", name="physical_delivery_binding_revision"),
        UniqueConstraint("operation_pk", name="physical_delivery_binding_operation"),
        CheckConstraint("revision > 0", name="physical_delivery_binding_revision_positive"),
        {"schema": "wms"},
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    binding_key: Mapped[str] = mapped_column(String(200), index=True)
    organization_id: Mapped[int] = mapped_column(Integer, index=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("wms.physical_warehouse.warehouse_id"), index=True)
    execution_id: Mapped[str] = mapped_column(String(36), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    previous_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("wms.physical_delivery_binding.id"), nullable=True,
    )
    operation_pk: Mapped[int] = mapped_column(ForeignKey("wms.physical_operation.id"), unique=True)
    snapshot: Mapped[dict] = mapped_column(JSON)
    digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PhysicalLedger(Base):
    """Physical deltas only. A reserve/pick never creates a warehouse write-off."""
    __tablename__ = "physical_ledger"
    __table_args__ = {"schema": "wms"}
    id: Mapped[int] = mapped_column(primary_key=True)
    operation_pk: Mapped[int] = mapped_column(ForeignKey("wms.physical_operation.id"), index=True)
    unit_id: Mapped[str] = mapped_column(ForeignKey("wms.physical_unit.id"))
    qty_delta: Mapped[Decimal] = mapped_column(QTY)
    dimensions: Mapped[dict] = mapped_column(JSON)


class PhysicalHold(Base):
    __tablename__ = "physical_hold"
    __table_args__ = (
        CheckConstraint("qty > 0 AND picked >= 0 AND shipped >= 0 AND consumed >= 0 AND released >= 0 AND shipped + consumed <= picked AND picked + released <= qty", name="physical_hold_quantities"),
        {"schema": "wms"},
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("wms.physical_warehouse.warehouse_id"), index=True)
    source: Mapped[str] = mapped_column(String(200), index=True)
    order_ref: Mapped[str] = mapped_column(String(200), index=True)
    unit_id: Mapped[str] = mapped_column(ForeignKey("wms.physical_unit.id"))
    qty: Mapped[Decimal] = mapped_column(QTY)
    picked: Mapped[Decimal] = mapped_column(QTY, default=Decimal("0"))
    shipped: Mapped[Decimal] = mapped_column(QTY, default=Decimal("0"))
    consumed: Mapped[Decimal] = mapped_column(QTY, default=Decimal("0"), server_default="0")
    released: Mapped[Decimal] = mapped_column(QTY, default=Decimal("0"))
    min_shelf_days: Mapped[int] = mapped_column(Integer, default=0)
    container: Mapped[str] = mapped_column(String(64))


class PhysicalTask(Base):
    __tablename__ = "physical_task"
    __table_args__ = (
        UniqueConstraint("warehouse_id", "id", name="cap_task_warehouse_identity"),
        CheckConstraint(
            "(kind = 'cap_place' AND qty IS NULL AND unit_id IS NULL AND hold_id IS NULL)"
            " OR (kind <> 'cap_place' AND qty IS NOT NULL)",
            name="cap_task_quantity_protocol",
        ),
        {"schema": "wms"},
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("wms.physical_warehouse.warehouse_id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="open")
    source: Mapped[str] = mapped_column(String(200), default="")
    unit_id: Mapped[str | None] = mapped_column(ForeignKey("wms.physical_unit.id"))
    hold_id: Mapped[int | None] = mapped_column(ForeignKey("wms.physical_hold.id"))
    qty: Mapped[Decimal | None] = mapped_column(QTY, nullable=True)
    destination_id: Mapped[int | None] = mapped_column(ForeignKey("wms.physical_location.location_id"))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assignee: Mapped[str] = mapped_column(String(200), default="")
    evidence: Mapped[str] = mapped_column(String(1000), default="")
    binding_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("wms.physical_delivery_binding.id"), nullable=True, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AssemblyExecution(Base):
    __tablename__ = "assembly_execution"
    __table_args__ = (
        CheckConstraint("output_qty > 0", name="assembly_execution_output_positive"),
        CheckConstraint("status IN ('active','closed')", name="assembly_execution_status"),
        UniqueConstraint("organization_id", "bind_operation_id", name="uq_assembly_execution_org_bind_operation"),
        {"schema": "wms"},
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("accounting.organization.id", ondelete="RESTRICT"),
        nullable=False, index=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("wms.physical_warehouse.warehouse_id"), nullable=False, index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("production.order_ownership.order_id"), nullable=False, index=True)
    specification_id: Mapped[int] = mapped_column(ForeignKey("production.assembly_specification.id"), nullable=False, index=True)
    specification_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    container: Mapped[str] = mapped_column(String(24), nullable=False)
    output_keys: Mapped[list] = mapped_column(JSON, nullable=False)
    consumed_output_keys: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    output_qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    bind_operation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active", nullable=False)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AssemblyProcessRun(Base):
    """Append-only prepared/confirmed process metadata; it is not a QC result."""
    __tablename__ = "assembly_process_run"
    __table_args__ = (
        UniqueConstraint("organization_id", "request_id", name="uq_assembly_process_run_org_request"),
        UniqueConstraint("organization_id", "order_id", "revision",
            name="uq_assembly_process_run_org_order_revision"),
        UniqueConstraint("organization_id", "order_id", "id",
            name="uq_assembly_process_run_org_order_id"),
        ForeignKeyConstraint(
            ["organization_id", "order_id", "previous_revision_id"],
            ["wms.assembly_process_run.organization_id", "wms.assembly_process_run.order_id",
             "wms.assembly_process_run.id"],
            name="fk_assembly_process_run_previous_scope", ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "order_id", "prepared_revision_id"],
            ["wms.assembly_process_run.organization_id", "wms.assembly_process_run.order_id",
             "wms.assembly_process_run.id"],
            name="fk_assembly_process_run_prepared_scope", ondelete="RESTRICT",
        ),
        CheckConstraint("revision > 0", name="ck_assembly_process_run_revision_positive"),
        CheckConstraint("kind IN ('prepared', 'confirmed')", name="ck_assembly_process_run_kind"),
        CheckConstraint("command_schema_version = 1", name="ck_assembly_process_run_command_schema"),
        {"schema": "wms"},
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("accounting.organization.id", ondelete="RESTRICT"), nullable=False,
    )
    order_id: Mapped[int] = mapped_column(
        ForeignKey("production.order_ownership.order_id", ondelete="RESTRICT"), nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    command_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    command: Mapped[dict] = mapped_column(JSON, nullable=False)
    command_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[dict] = mapped_column(JSON, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    prepared_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    result: Mapped[dict] = mapped_column(JSON, nullable=False)
    actor_sub: Mapped[str] = mapped_column(String(200), nullable=False)
    actor_user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


class AssemblyInputLink(Base):
    __tablename__ = "assembly_input_link"
    __table_args__ = (
        CheckConstraint("qty > 0 AND consumed_qty >= 0 AND returned_qty >= 0 AND consumed_qty + returned_qty <= qty",
            name="assembly_input_link_quantities"),
        UniqueConstraint("kit_move_operation_id", "hold_id", name="uq_assembly_input_link_move_hold"),
        {"schema": "wms"},
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    execution_id: Mapped[str] = mapped_column(ForeignKey("wms.assembly_execution.id"), nullable=False, index=True)
    specification_line_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    hold_id: Mapped[int] = mapped_column(ForeignKey("wms.physical_hold.id"), nullable=False, index=True)
    original_unit_id: Mapped[str] = mapped_column(ForeignKey("wms.physical_unit.id"), nullable=False)
    actual_kit_unit_id: Mapped[str] = mapped_column(ForeignKey("wms.physical_unit.id"), nullable=False)
    kit_move_operation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    consumed_qty: Mapped[Decimal] = mapped_column(QTY, default=Decimal("0"), server_default="0", nullable=False)
    returned_qty: Mapped[Decimal] = mapped_column(QTY, default=Decimal("0"), server_default="0", nullable=False)
    source_dimensions: Mapped[dict] = mapped_column(JSON, nullable=False)
    kit_dimensions: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AssemblyConsumption(Base):
    __tablename__ = "assembly_consumption"
    __table_args__ = (
        CheckConstraint("qty > 0", name="assembly_consumption_positive"),
        UniqueConstraint("execution_id", "output_key", "specification_line_id", "input_link_id",
            name="uq_assembly_consumption_output_link"),
        {"schema": "wms"},
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    execution_id: Mapped[str] = mapped_column(ForeignKey("wms.assembly_execution.id"), nullable=False, index=True)
    input_link_id: Mapped[int] = mapped_column(ForeignKey("wms.assembly_input_link.id"), nullable=False, index=True)
    specification_line_id: Mapped[str] = mapped_column(String(64), nullable=False)
    output_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    operation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    qty: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    dimensions: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AssemblyOutputIdentity(Base):
    """One immutable finished identity; its zero/shipped history retains serial scope."""
    __tablename__ = "assembly_output_identity"
    __table_args__ = (
        UniqueConstraint("organization_id", "order_id", "output_key", name="uq_assembly_output_key"),
        UniqueConstraint("birth_operation_pk", name="uq_assembly_output_birth"),
        CheckConstraint("serial_scope IS NULL OR serial_scope IN ('organization','sku')", name="ck_assembly_serial_scope"),
        CheckConstraint("(serial_number IS NULL) = (serial_scope IS NULL)", name="ck_assembly_serial_pair"),
        Index("ix_assembly_output_serial", "organization_id", "serial_number"),
        {"schema": "wms"},
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("accounting.organization.id", ondelete="RESTRICT"), nullable=False, index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("production.order_ownership.order_id", ondelete="RESTRICT"), nullable=False, index=True)
    execution_id: Mapped[str] = mapped_column(ForeignKey("wms.assembly_execution.id", ondelete="RESTRICT"), nullable=False, index=True)
    output_key: Mapped[str] = mapped_column(String(64), nullable=False)
    sku_code: Mapped[str] = mapped_column(ForeignKey("sku.code", ondelete="RESTRICT"), nullable=False)
    batch_id: Mapped[int] = mapped_column(Integer, nullable=False)
    serial_number: Mapped[str | None] = mapped_column(String(256), nullable=True)
    serial_scope: Mapped[str | None] = mapped_column(String(16), nullable=True)
    anchor: Mapped[dict] = mapped_column(JSON, nullable=False)
    genealogy: Mapped[list] = mapped_column(JSON, nullable=False)
    genealogy_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    effective_lot: Mapped[dict] = mapped_column(JSON, nullable=False)
    output_executor: Mapped[dict] = mapped_column(JSON, nullable=False)
    consume_executor: Mapped[dict] = mapped_column(JSON, nullable=False)
    birth_operation_pk: Mapped[int] = mapped_column(ForeignKey("wms.physical_operation.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PickedAllocation(Base):
    __tablename__ = "physical_pick"
    __table_args__ = (
        CheckConstraint("qty > 0 AND shipped >= 0 AND shipped <= qty", name="physical_pick_quantity"),
        {"schema": "wms"},
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    hold_id: Mapped[int] = mapped_column(ForeignKey("wms.physical_hold.id"), index=True)
    unit_id: Mapped[str] = mapped_column(ForeignKey("wms.physical_unit.id"))
    qty: Mapped[Decimal] = mapped_column(QTY)
    shipped: Mapped[Decimal] = mapped_column(QTY, default=Decimal("0"))
    packed: Mapped[bool] = mapped_column(Boolean, default=False)
    canceled: Mapped[bool] = mapped_column(Boolean, default=False)
    packing_evidence: Mapped[str] = mapped_column(String(1000), default="")
    task_id: Mapped[str | None] = mapped_column(ForeignKey("wms.physical_task.id"), nullable=True, index=True)
    binding_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("wms.physical_delivery_binding.id"), nullable=True, index=True,
    )


class PickRule(Base):
    __tablename__ = "physical_pick_rule"
    __table_args__ = (
        UniqueConstraint("warehouse_id", "sku_code", "location_id", name="physical_pick_rule_identity"),
        CheckConstraint("min_qty >= 0 AND max_qty > min_qty", name="physical_pick_rule_minmax"),
        {"schema": "wms"},
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("wms.physical_warehouse.warehouse_id"))
    sku_code: Mapped[str] = mapped_column(String(64))
    location_id: Mapped[int] = mapped_column(ForeignKey("wms.physical_location.location_id"))
    min_qty: Mapped[Decimal] = mapped_column(QTY)
    max_qty: Mapped[Decimal] = mapped_column(QTY)


class PhysicalCount(Base):
    __tablename__ = "physical_count"
    __table_args__ = {"schema": "wms"}
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("wms.physical_warehouse.warehouse_id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="open")
    locations: Mapped[list] = mapped_column(JSON)
    completed_locations: Mapped[list] = mapped_column(JSON, default=list)
    expected: Mapped[dict] = mapped_column(JSON)
    actor: Mapped[str] = mapped_column(String(200))
    evidence: Mapped[str] = mapped_column(String(1000), default="")
    approved_by: Mapped[str | None] = mapped_column(String(200))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CountLocationLock(Base):
    __tablename__ = "physical_count_lock"
    __table_args__ = {"schema": "wms"}
    location_id: Mapped[int] = mapped_column(ForeignKey("wms.physical_location.location_id"), primary_key=True)
    count_id: Mapped[str] = mapped_column(ForeignKey("wms.physical_count.id"))


class PhysicalCountLine(Base):
    __tablename__ = "physical_count_line"
    __table_args__ = (
        UniqueConstraint("count_id", "identity", name="physical_count_line_identity"),
        CheckConstraint("qty IS NULL OR qty >= 0", name="physical_count_line_quantity"),
        {"schema": "wms"},
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    count_id: Mapped[str] = mapped_column(ForeignKey("wms.physical_count.id"))
    identity: Mapped[str] = mapped_column(String(64))
    unit_id: Mapped[str | None] = mapped_column(ForeignKey("wms.physical_unit.id"))
    location_id: Mapped[int] = mapped_column(ForeignKey("wms.physical_location.location_id"))
    dimensions: Mapped[dict] = mapped_column(JSON)
    qty: Mapped[Decimal | None] = mapped_column(QTY)


class ExchangeJob(Base):
    __tablename__ = "physical_exchange"
    __table_args__ = (UniqueConstraint("operation_pk", name="physical_exchange_operation"), {"schema": "wms"})
    id: Mapped[int] = mapped_column(primary_key=True)
    operation_pk: Mapped[int] = mapped_column(ForeignKey("wms.physical_operation.id"))
    organization_id: Mapped[int] = mapped_column(Integer, index=True)
    operation_id: Mapped[str] = mapped_column(String(36))
    payload: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    depends_on: Mapped[str | None] = mapped_column(String(36))
    dependencies: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    document_ref: Mapped[str | None] = mapped_column(String(255))
    document_state: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str] = mapped_column(String(1000), default="")


class ReconciliationSnapshot(Base):
    __tablename__ = "physical_reconciliation"
    __table_args__ = {"schema": "wms"}
    id: Mapped[int] = mapped_column(primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("wms.physical_warehouse.warehouse_id"))
    snapshot_ref: Mapped[str] = mapped_column(String(255))
    external: Mapped[dict] = mapped_column(JSON)
    observed: Mapped[dict] = mapped_column(JSON)
    differences: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


CAPACITY_EDGE_WS = (
    "\u0009\u000a\u000b\u000c\u000d\u001c\u001d\u001e\u001f\u0020"
    "\u0085\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008"
    "\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)
CAPACITY_WS_SQL = "'" + CAPACITY_EDGE_WS + "'"


CAP_MAX = "99999999999999.999999"
CAP_POSITIVE = (
    "length_mm", "width_mm", "height_mm", "opening_width_mm",
    "opening_height_mm", "gross_limit_kg",
)
CAP_NONNEGATIVE = ("clearance_mm", "tare_kg", "top_load_limit_kg")
CAP_MEASURES = CAP_POSITIVE + CAP_NONNEGATIVE

CAP_BINDING_CHECK = """
(packaging_id IS NOT NULL
 AND bound_mapping_revision IS NULL AND ancestor_binding IS NULL)
OR
(packaging_id IS NULL
 AND bound_mapping_revision IS NOT NULL AND bound_mapping_revision > 0
 AND ancestor_binding IS NOT NULL)
"""

def cap_measure_checks(names, positive):
    comparison = ">" if positive else ">="
    return [
        CheckConstraint(
            f"{n} IS NULL OR ({n} {comparison} 0 AND {n} <= {CAP_MAX})",
            name=f"cap_profile_{n}",
        )
        for n in names
    ]

def cap_evidence_columns():
    return [
        Column("source_ref", String(1000), nullable=False),
        Column("source_revision", String(200), nullable=False),
        Column("evidence", String(1000), nullable=False),
        Column("approved_by", String(200), nullable=False),
        Column("approved_at", DateTime(timezone=True), nullable=False),
    ]

def cap_evidence_checks(prefix):
    return [
        CheckConstraint(
            f"{n} = trim({n}, {CAPACITY_WS_SQL}) "
            f"AND length({n}) BETWEEN 1 AND {limit}",
            name=f"{prefix}_{n}",
        )
        for n, limit in (
            ("source_ref", 1000), ("source_revision", 200),
            ("evidence", 1000), ("approved_by", 200),
        )
    ]

load_resource_table = Table(
    "physical_load_resource", Base.metadata,
    Column("id", String(36), primary_key=True),
    Column("warehouse_id", Integer,
           ForeignKey("wms.physical_warehouse.warehouse_id"), nullable=False),
    Column("parent_id", String(36), nullable=True),
    Column("kind", String(24), nullable=False),
    Column("topology_revision", Integer, nullable=False),
    *cap_evidence_columns(),
    UniqueConstraint("warehouse_id", "id", name="cap_resource_scope"),
    ForeignKeyConstraint(
        ["warehouse_id", "parent_id"],
        ["wms.physical_load_resource.warehouse_id",
         "wms.physical_load_resource.id"],
        name="cap_resource_parent", ondelete="RESTRICT",
    ),
    CheckConstraint("length(id) = 36", name="cap_resource_id"),
    CheckConstraint("topology_revision > 0", name="cap_resource_revision"),
    CheckConstraint(
        "kind IN ('warehouse','storage_area','floor','rack','shelf',"
        "'cell','staging','equipment')", name="cap_resource_kind",
    ),
    CheckConstraint(
        "(kind = 'warehouse' AND parent_id IS NULL) OR "
        "(kind <> 'warehouse' AND parent_id IS NOT NULL)",
        name="cap_resource_root",
    ),
    CheckConstraint("parent_id IS NULL OR parent_id <> id",
                    name="cap_resource_not_self"),
    *cap_evidence_checks("cap_resource"),
    schema="wms",
)

profile_table = Table(
    "physical_placement_profile", Base.metadata,
    Column("id", String(36), primary_key=True),
    Column("organization_id", Integer,
           ForeignKey("accounting.organization.id"), nullable=False),
    Column("warehouse_id", Integer, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("packaging_id", Integer, nullable=True),
    Column("location_id", Integer, nullable=True),
    Column("resource_id", String(36), nullable=True),
    *[Column(n, Numeric(20, 6), nullable=True) for n in CAP_MEASURES],
    Column("max_layers", Integer, nullable=True),
    Column("orientations", JSON(none_as_null=True), nullable=True),
    Column("load_type", String(24), nullable=True),
    Column("support_rule", String(24), nullable=True),
    Column("access_method", String(24), nullable=True),
    Column("equipment_ref", String(1000), nullable=True),
    Column("restrictions", JSON(none_as_null=True), nullable=True),
    *cap_evidence_columns(),
    Column("bound_mapping_revision", Integer, nullable=True),
    Column("ancestor_binding", JSON(none_as_null=True), nullable=True),
    Column("digest", String(64), nullable=False),
    Column("canonical_payload", String, nullable=False),
    Column("sealed", Boolean, nullable=False,
           default=False, server_default="false"),
    UniqueConstraint("warehouse_id", "packaging_id", "revision",
                     name="cap_profile_packaging_revision"),
    UniqueConstraint("warehouse_id", "location_id", "revision",
                     name="cap_profile_location_revision"),
    UniqueConstraint("warehouse_id", "resource_id", "revision",
                     name="cap_profile_resource_revision"),
    UniqueConstraint("warehouse_id", "packaging_id", "id",
                     name="cap_profile_packaging_reference"),
    UniqueConstraint("warehouse_id", "location_id", "id",
                     name="cap_profile_location_reference"),
    ForeignKeyConstraint(
        ["organization_id", "warehouse_id"],
        ["wms.physical_warehouse.organization_id",
         "wms.physical_warehouse.warehouse_id"],
        name="cap_profile_warehouse", ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["organization_id", "packaging_id"],
        ["wms.physical_packaging.organization_id",
         "wms.physical_packaging.id"],
        name="cap_profile_packaging", ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["warehouse_id", "location_id"],
        ["wms.physical_location.warehouse_id",
         "wms.physical_location.location_id"],
        name="cap_profile_location", ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["warehouse_id", "resource_id"],
        ["wms.physical_load_resource.warehouse_id",
         "wms.physical_load_resource.id"],
        name="cap_profile_resource", ondelete="RESTRICT",
    ),
    CheckConstraint("length(id) = 36", name="cap_profile_id"),
    CheckConstraint("revision > 0", name="cap_profile_revision"),
    CheckConstraint(
        "(CASE WHEN packaging_id IS NULL THEN 0 ELSE 1 END"
        " + CASE WHEN location_id IS NULL THEN 0 ELSE 1 END"
        " + CASE WHEN resource_id IS NULL THEN 0 ELSE 1 END) = 1",
        name="cap_profile_one_target",
    ),
    CheckConstraint(CAP_BINDING_CHECK, name="cap_profile_binding_check"),
    *cap_measure_checks(CAP_POSITIVE, True),
    *cap_measure_checks(CAP_NONNEGATIVE, False),
    CheckConstraint("max_layers IS NULL OR max_layers > 0",
                    name="cap_profile_layers"),
    CheckConstraint(
        "load_type IS NULL OR load_type IN "
        "('total_mass','concentrated','distributed')",
        name="cap_profile_load_type",
    ),
    CheckConstraint(
        "support_rule IS NULL OR support_rule IN ('full_base','layered')",
        name="cap_profile_support",
    ),
    CheckConstraint(
        "access_method IS NULL OR access_method IN ('manual','equipment')",
        name="cap_profile_access",
    ),
    CheckConstraint(
        "("
        "(access_method = 'equipment' AND equipment_ref IS NOT NULL"
        f" AND equipment_ref = trim(equipment_ref, {CAPACITY_WS_SQL})"
        " AND length(equipment_ref) BETWEEN 1 AND 1000)"
        " OR ((access_method IS NULL OR access_method <> 'equipment')"
        " AND equipment_ref IS NULL)"
        ") IS TRUE",
        name="cap_profile_equipment",
    ),
    CheckConstraint(
        "gross_limit_kg IS NULL OR tare_kg IS NULL OR tare_kg <= gross_limit_kg",
        name="cap_profile_mass",
    ),
    CheckConstraint("length(digest) = 64", name="cap_profile_digest_length"),
    *cap_evidence_checks("cap_profile"),
    schema="wms",
)

slot_table = Table(
    "physical_placement_slot", Base.metadata,
    Column("warehouse_id", Integer, primary_key=True),
    Column("location_id", Integer, primary_key=True),
    Column("profile_id", String(36), primary_key=True),
    Column("slot_key", String(64), primary_key=True),
    Column("x_mm", Numeric(20, 6), nullable=False),
    Column("y_mm", Numeric(20, 6), nullable=False),
    Column("z_mm", Numeric(20, 6), nullable=False),
    Column("width_mm", Numeric(20, 6), nullable=False),
    Column("height_mm", Numeric(20, 6), nullable=False),
    Column("depth_mm", Numeric(20, 6), nullable=False),
    ForeignKeyConstraint(
        ["warehouse_id", "location_id", "profile_id"],
        ["wms.physical_placement_profile.warehouse_id",
         "wms.physical_placement_profile.location_id",
         "wms.physical_placement_profile.id"],
        name="cap_slot_profile", ondelete="RESTRICT",
    ),
    CheckConstraint(
        f"slot_key = trim(slot_key, {CAPACITY_WS_SQL}) "
        "AND length(slot_key) BETWEEN 1 AND 64",
        name="cap_slot_key",
    ),
    *[
        CheckConstraint(f"{n} >= 0 AND {n} <= {CAP_MAX}",
                        name=f"cap_slot_{n}")
        for n in ("x_mm", "y_mm", "z_mm")
    ],
    *[
        CheckConstraint(f"{n} > 0 AND {n} <= {CAP_MAX}",
                        name=f"cap_slot_{n}")
        for n in ("width_mm", "height_mm", "depth_mm")
    ],
    schema="wms",
)

carrier_table = Table(
    "physical_carrier", Base.metadata,
    Column("id", String(36), primary_key=True),
    Column("warehouse_id", Integer, nullable=False),
    Column("location_id", Integer, nullable=False),
    Column("packaging_id", Integer, nullable=True),
    Column("packaging_profile_id", String(36), nullable=True),
    Column("content_revision", Integer, nullable=False),
    Column("confirmed_content_revision", Integer, nullable=True),
    Column("gross_kg", Numeric(20, 6), nullable=True),
    Column("tare_kg", Numeric(20, 6), nullable=True),
    Column("measurement_evidence", String(1000), nullable=False),
    Column("observed_by", String(200), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("state", String(16), nullable=False),
    UniqueConstraint("warehouse_id", "location_id", "id",
                     name="cap_carrier_scope"),
    UniqueConstraint("warehouse_id", "id",
                     name="cap_carrier_warehouse_identity"),
    ForeignKeyConstraint(
        ["warehouse_id", "location_id"],
        ["wms.physical_location.warehouse_id",
         "wms.physical_location.location_id"],
        name="cap_carrier_location", ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["warehouse_id", "packaging_id", "packaging_profile_id"],
        ["wms.physical_placement_profile.warehouse_id",
         "wms.physical_placement_profile.packaging_id",
         "wms.physical_placement_profile.id"],
        name="cap_carrier_packaging", ondelete="RESTRICT",
    ),
    CheckConstraint("length(id) = 36", name="cap_carrier_id"),
    CheckConstraint(
        "(packaging_id IS NULL AND packaging_profile_id IS NULL)"
        " OR (packaging_id IS NOT NULL AND packaging_profile_id IS NOT NULL)",
        name="cap_carrier_packaging_pair",
    ),
    CheckConstraint("content_revision > 0", name="cap_carrier_revision"),
    CheckConstraint(
        "confirmed_content_revision IS NULL OR "
        "(confirmed_content_revision > 0"
        " AND confirmed_content_revision <= content_revision)",
        name="cap_carrier_confirmation_revision",
    ),
    CheckConstraint(
        "gross_kg IS NULL OR "
        f"(gross_kg >= 0 AND gross_kg <= {CAP_MAX})",
        name="cap_carrier_gross",
    ),
    CheckConstraint(
        "tare_kg IS NULL OR "
        f"(tare_kg >= 0 AND tare_kg <= {CAP_MAX})",
        name="cap_carrier_tare",
    ),
    CheckConstraint("gross_kg IS NULL OR tare_kg IS NULL OR gross_kg >= tare_kg",
                    name="cap_carrier_mass"),
    CheckConstraint(
        "confirmed_content_revision IS NULL OR "
        "(gross_kg IS NOT NULL AND tare_kg IS NOT NULL)",
        name="cap_carrier_measured_confirmation",
    ),
    CheckConstraint("state IN ('present','departed')",
                    name="cap_carrier_state"),
    CheckConstraint(
        f"measurement_evidence = trim(measurement_evidence, {CAPACITY_WS_SQL}) "
        "AND length(measurement_evidence) BETWEEN 1 AND 1000",
        name="cap_carrier_evidence",
    ),
    CheckConstraint(
        f"observed_by = trim(observed_by, {CAPACITY_WS_SQL}) "
        "AND length(observed_by) BETWEEN 1 AND 200",
        name="cap_carrier_observer",
    ),
    schema="wms",
)

position_table = Table(
    "physical_carrier_position", Base.metadata,
    Column("carrier_id", String(36), primary_key=True),
    Column("ordinal", Integer, primary_key=True),
    Column("warehouse_id", Integer, nullable=False),
    Column("location_id", Integer, nullable=False),
    Column("location_profile_id", String(36), nullable=False),
    Column("slot_key", String(64), nullable=False),
    UniqueConstraint("carrier_id", "location_profile_id", "slot_key",
                     name="cap_carrier_position_identity"),
    ForeignKeyConstraint(
        ["warehouse_id", "location_id", "carrier_id"],
        ["wms.physical_carrier.warehouse_id",
         "wms.physical_carrier.location_id", "wms.physical_carrier.id"],
        name="cap_position_carrier", ondelete="RESTRICT",
        deferrable=True, initially="DEFERRED",
    ),
    ForeignKeyConstraint(
        ["warehouse_id", "location_id", "location_profile_id", "slot_key"],
        ["wms.physical_placement_slot.warehouse_id",
         "wms.physical_placement_slot.location_id",
         "wms.physical_placement_slot.profile_id",
         "wms.physical_placement_slot.slot_key"],
        name="cap_position_slot", ondelete="RESTRICT",
    ),
    CheckConstraint("ordinal >= 0", name="cap_position_ordinal"),
    schema="wms",
)

class LoadResource(Base):
    __table__ = load_resource_table
    __mapper_args__ = {"batch": False}

class PlacementProfile(Base):
    __table__ = profile_table

class PlacementSlot(Base):
    __table__ = slot_table

class PhysicalCarrier(Base):
    __table__ = carrier_table

class CarrierPosition(Base):
    __table__ = position_table

_CAP_SESSION_KEY = "wms.g06.profile_guard.v1"
_CAP_LIMIT = Decimal(CAP_MAX)
_CAP_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$"
)
_CAP_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_CAP_FIXED_RE = re.compile(r"^(0|[1-9][0-9]{0,13})\.[0-9]{6}$")

_CAP_SLOT_NONNEGATIVE = ("x_mm", "y_mm", "z_mm")
_CAP_SLOT_POSITIVE = ("width_mm", "height_mm", "depth_mm")
_CAP_SLOT_MEASURES = _CAP_SLOT_NONNEGATIVE + _CAP_SLOT_POSITIVE
_CAP_SLOT_COLUMNS = frozenset((
    "warehouse_id",
    "location_id",
    "profile_id",
    "slot_key",
    "x_mm",
    "y_mm",
    "z_mm",
    "width_mm",
    "height_mm",
    "depth_mm",
))
_CAP_RESOURCE_KINDS = frozenset((
    "warehouse", "storage_area", "floor", "rack", "shelf",
    "cell", "staging", "equipment",
))
_CAP_ORIENTATIONS = frozenset((
    "LWH", "LHW", "WLH", "WHL", "HLW", "HWL",
))
_CAP_PROFILE_OMIT = frozenset((
    "digest", "canonical_payload", "sealed",
))


def _cap_fail(code):
    raise ValueError(code)


def _cap_json(value):
    # Reuse the real physical protocol canonical serializer.
    # The import happens when a callback runs, after models are registered.
    from modules.wms.physical_stock import canonical
    return canonical(value)


def _cap_digest(value):
    from modules.wms.physical_stock import digest

    return digest(value)


def _cap_integer(value, *, allow_zero=False):
    if type(value) is not int:
        _cap_fail("capacity_integer_type")
    lower = 0 if allow_zero else 1
    if not lower <= value <= 2147483647:
        _cap_fail("capacity_integer_range")
    return value


def _cap_uuid(value):
    if not isinstance(value, str) or not _CAP_UUID_RE.fullmatch(value):
        _cap_fail("capacity_uuid_shape")
    return value


def _cap_text(value, maximum):
    if (not isinstance(value, str)
            or not 1 <= len(value) <= maximum
            or value != value.strip(CAPACITY_EDGE_WS)):
        _cap_fail("capacity_text_shape")
    return value


def _cap_number(value, *, positive):
    if isinstance(value, (bool, float)):
        _cap_fail("capacity_decimal_type")
    if not isinstance(value, (Decimal, int, str)):
        _cap_fail("capacity_decimal_type")
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("capacity_decimal_type") from exc
    if (not result.is_finite()
            or result.as_tuple().exponent < -6
            or result > _CAP_LIMIT
            or result < 0
            or (positive and result == 0)):
        _cap_fail("capacity_decimal_range")
    return result


def _cap_fixed(value):
    number = _cap_number(value, positive=False)
    # Numeric(20,6) does not preserve a negative sign on numeric zero.
    if number == 0:
        number = Decimal("0")
    return format(number, ".6f")


def _cap_signed_decimal(value, *, positive):
    if not isinstance(value, str) or not _CAP_FIXED_RE.fullmatch(value):
        _cap_fail("capacity_signed_decimal_shape")
    number = _cap_number(value, positive=positive)
    if _cap_fixed(number) != value:
        _cap_fail("capacity_signed_decimal_spelling")
    return number


def _cap_utc(value, *, database_value):
    if not isinstance(value, datetime):
        _cap_fail("capacity_timestamp_type")
    if value.tzinfo is None:
        if not database_value:
            _cap_fail("capacity_server_timestamp_requires_timezone")
        # Newly declared capacity timestamps are written in UTC.
        # SQLite DateTime round trips discard tzinfo; this is a persistence
        # convention for these new tables, not interpretation of old history.
        value = value.replace(tzinfo=timezone.utc)
    if not database_value and value.utcoffset() != timedelta(0):
        _cap_fail("capacity_server_timestamp_requires_utc")
    return value.astimezone(timezone.utc)


def _cap_timestamp_text(value):
    return (
        _cap_utc(value, database_value=True)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _cap_same_scalar(a, b):
    if isinstance(a, datetime) and isinstance(b, datetime):
        return (
            _cap_utc(a, database_value=True)
            == _cap_utc(b, database_value=True)
        )
    if isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
        return _cap_json(a) == _cap_json(b)
    # bool must not compare equal to an integer.
    if type(a) is bool or type(b) is bool:
        return type(a) is type(b) and a == b
    return a == b


def _cap_target_values(target, old=None):
    """Passive history/loaded values; no ORM lazy-load or object-alias snapshot."""
    state = inspect(target)
    result = {}
    for column in target.__table__.columns:
        attribute = state.attrs[column.name]
        history = attribute.history
        if history.added:
            value = history.added[-1]
        elif attribute.loaded_value is not NO_VALUE:
            value = attribute.loaded_value
        elif old is not None:
            value = old[column.name]
        else:
            value = None
        result[column.name] = value
    return result


def _cap_original_row(connection, model, target):
    state = inspect(target)
    identity = state.identity
    if identity is None:
        _cap_fail("capacity_persistent_identity_missing")
    table = model.__table__
    columns = tuple(table.primary_key.columns)
    if len(identity) != len(columns):
        _cap_fail("capacity_persistent_identity_shape")
    row = connection.execute(
        select(table).where(*(
            column == value
            for column, value in zip(columns, identity, strict=True)
        ))
    ).mappings().one_or_none()
    if row is None:
        _cap_fail("capacity_persistent_row_missing")
    return dict(row)


def _cap_lock_warehouse(connection, warehouse_id, organization_id=None):
    _cap_integer(warehouse_id)
    table = WarehouseControl.__table__
    row = connection.execute(
        select(table)
        .where(table.c.warehouse_id == warehouse_id)
        .with_for_update()
    ).mappings().one_or_none()
    if row is None:
        _cap_fail("capacity_warehouse_missing")
    if (organization_id is not None
            and row["organization_id"] != organization_id):
        _cap_fail("capacity_warehouse_organization")
    return dict(row)


def _cap_evidence(values, *, database_value):
    _cap_text(values["source_ref"], 1000)
    _cap_text(values["source_revision"], 200)
    _cap_text(values["evidence"], 1000)
    _cap_text(values["approved_by"], 200)
    _cap_utc(values["approved_at"], database_value=database_value)


def _cap_chain(connection, warehouse_id, start_id):
    """WH mutex already held; chain reads do not acquire late resource locks."""
    _cap_integer(warehouse_id)
    _cap_uuid(start_id)
    table = LoadResource.__table__
    current_id = start_id
    seen = set()
    chain = []
    while current_id is not None:
        if current_id in seen:
            _cap_fail("capacity_topology_cycle")
        seen.add(current_id)
        row = connection.execute(
            select(table).where(
                table.c.warehouse_id == warehouse_id,
                table.c.id == current_id,
            )
        ).mappings().one_or_none()
        if row is None:
            _cap_fail("capacity_chain_scope_missing")
        values = dict(row)
        _cap_uuid(values["id"])
        _cap_integer(values["topology_revision"])
        _cap_evidence(values, database_value=True)
        kind = values["kind"]
        parent_id = values["parent_id"]
        if kind not in _CAP_RESOURCE_KINDS:
            _cap_fail("capacity_resource_kind")
        if parent_id is not None:
            _cap_uuid(parent_id)
        if (kind == "warehouse") != (parent_id is None):
            _cap_fail("capacity_chain_root")
        chain.append({
            "resource_id": values["id"],
            "parent_id": parent_id,
            "topology_revision": values["topology_revision"],
        })
        current_id = parent_id
    return chain


def _cap_validate_chain_shape(chain):
    if not isinstance(chain, list) or not chain:
        _cap_fail("capacity_binding_shape")
    seen = set()
    for index, item in enumerate(chain):
        if (not isinstance(item, dict)
                or set(item) != {
                    "resource_id", "parent_id", "topology_revision",
                }):
            _cap_fail("capacity_binding_shape")
        resource_id = _cap_uuid(item["resource_id"])
        _cap_integer(item["topology_revision"])
        parent_id = item["parent_id"]
        if parent_id is not None:
            _cap_uuid(parent_id)
        if resource_id in seen:
            _cap_fail("capacity_binding_cycle")
        seen.add(resource_id)
        if index + 1 < len(chain):
            if parent_id != chain[index + 1]["resource_id"]:
                _cap_fail("capacity_binding_chain_order")
        elif parent_id is not None:
            _cap_fail("capacity_binding_incomplete_root")


def _cap_current_binding(connection, profile):
    warehouse_id = profile["warehouse_id"]
    packaging_id = profile["packaging_id"]
    bound = profile["bound_mapping_revision"]
    signed_chain = profile["ancestor_binding"]

    if packaging_id is not None:
        if bound is not None or signed_chain is not None:
            _cap_fail("capacity_packaging_topology_binding")
        return

    # Explicit Python equivalent of the corrected IS NOT NULL SQL CHECK.
    if bound is None:
        _cap_fail("capacity_binding_revision_missing")
    _cap_integer(bound)
    _cap_validate_chain_shape(signed_chain)

    if profile["location_id"] is not None:
        table = PhysicalLocation.__table__
        row = connection.execute(
            select(table).where(
                table.c.warehouse_id == warehouse_id,
                table.c.location_id == profile["location_id"],
            )
        ).mappings().one_or_none()
        if row is None:
            _cap_fail("capacity_location_scope_missing")
        current_id = row["resource_id"]
        current_revision = row["resource_mapping_revision"]
    else:
        table = LoadResource.__table__
        row = connection.execute(
            select(table).where(
                table.c.warehouse_id == warehouse_id,
                table.c.id == profile["resource_id"],
            )
        ).mappings().one_or_none()
        if row is None:
            _cap_fail("capacity_resource_scope_missing")
        current_id = row["id"]
        current_revision = row["topology_revision"]

    if current_id is None or current_revision != bound:
        _cap_fail("capacity_binding_stale_or_fabricated")
    fresh_chain = _cap_chain(connection, warehouse_id, current_id)
    if _cap_json(signed_chain) != _cap_json(fresh_chain):
        _cap_fail("capacity_binding_stale_or_fabricated")


def _cap_string_array(value, *, allowed=None, nonempty=False, maximum=1000):
    if value is None:
        return
    if not isinstance(value, list):
        _cap_fail("capacity_array_type")
    if len(value) > maximum or (nonempty and not value):
        _cap_fail("capacity_array_length")
    for item in value:
        _cap_text(item, 1000)
        if allowed is not None and item not in allowed:
            _cap_fail("capacity_array_value")
    if len(value) != len(set(value)):
        _cap_fail("capacity_array_duplicate")


def _cap_profile_shape(connection, values, *, database_value):
    _cap_uuid(values["id"])
    _cap_integer(values["organization_id"])
    _cap_integer(values["warehouse_id"])
    _cap_integer(values["revision"])
    _cap_evidence(values, database_value=database_value)

    packaging_id = values["packaging_id"]
    location_id = values["location_id"]
    resource_id = values["resource_id"]
    if sum(x is not None for x in (
        packaging_id, location_id, resource_id,
    )) != 1:
        _cap_fail("capacity_profile_one_target")

    if packaging_id is not None:
        _cap_integer(packaging_id)
        table = Packaging.__table__
        row = connection.execute(
            select(table.c.id).where(
                table.c.organization_id == values["organization_id"],
                table.c.id == packaging_id,
            )
        ).first()
        if row is None:
            _cap_fail("capacity_packaging_scope_missing")
    if location_id is not None:
        _cap_integer(location_id)
        table = PhysicalLocation.__table__
        row = connection.execute(
            select(table.c.location_id).where(
                table.c.warehouse_id == values["warehouse_id"],
                table.c.location_id == location_id,
            )
        ).first()
        if row is None:
            _cap_fail("capacity_location_scope_missing")
    if resource_id is not None:
        _cap_uuid(resource_id)

    for name in CAP_POSITIVE:
        if values[name] is not None:
            _cap_number(values[name], positive=True)
    for name in CAP_NONNEGATIVE:
        if values[name] is not None:
            _cap_number(values[name], positive=False)

    if values["max_layers"] is not None:
        _cap_integer(values["max_layers"])
    _cap_string_array(
        values["orientations"],
        allowed=_CAP_ORIENTATIONS,
        nonempty=True,
        maximum=6,
    )
    _cap_string_array(values["restrictions"], maximum=1000)

    for name, allowed in (
        ("load_type", {"total_mass", "concentrated", "distributed"}),
        ("support_rule", {"full_base", "layered"}),
        ("access_method", {"manual", "equipment"}),
    ):
        if values[name] is not None and values[name] not in allowed:
            _cap_fail(f"capacity_profile_{name}")

    if values["access_method"] == "equipment":
        _cap_text(values["equipment_ref"], 1000)
    elif values["equipment_ref"] is not None:
        _cap_fail("capacity_profile_equipment")

    if (values["gross_limit_kg"] is not None
            and values["tare_kg"] is not None
            and Decimal(values["tare_kg"]) > Decimal(values["gross_limit_kg"])):
        _cap_fail("capacity_profile_mass")

    if type(values["sealed"]) is not bool:
        _cap_fail("capacity_profile_sealed_type")
    if (not isinstance(values["digest"], str)
            or not _CAP_HEX_RE.fullmatch(values["digest"])):
        _cap_fail("capacity_profile_digest_shape")
    if (not isinstance(values["canonical_payload"], str)
            or not values["canonical_payload"]):
        _cap_fail("capacity_profile_payload_type")

    _cap_current_binding(connection, values)


def _cap_profile_json(values):
    # Full actual declared column set; explicit nulls are retained.
    result = {
        column.name: values[column.name]
        for column in PlacementProfile.__table__.columns
        if column.name not in _CAP_PROFILE_OMIT
    }
    for name in CAP_MEASURES:
        value = result[name]
        result[name] = None if value is None else _cap_fixed(value)
    result["approved_at"] = _cap_timestamp_text(result["approved_at"])
    return result


def _cap_slot_shape(values):
    if set(values) != _CAP_SLOT_COLUMNS:
        _cap_fail("capacity_slot_column_shape")
    _cap_integer(values["warehouse_id"])
    _cap_integer(values["location_id"])
    _cap_uuid(values["profile_id"])
    _cap_text(values["slot_key"], 64)
    for name in _CAP_SLOT_NONNEGATIVE:
        _cap_number(values[name], positive=False)
    for name in _CAP_SLOT_POSITIVE:
        _cap_number(values[name], positive=True)


def _cap_slot_json(values):
    _cap_slot_shape(values)
    result = dict(values)
    for name in _CAP_SLOT_MEASURES:
        result[name] = _cap_fixed(result[name])
    return result


def _cap_signed_slot(values, profile):
    if not isinstance(values, dict) or set(values) != _CAP_SLOT_COLUMNS:
        _cap_fail("capacity_signed_slot_shape")
    result = dict(values)
    _cap_integer(result["warehouse_id"])
    _cap_integer(result["location_id"])
    _cap_uuid(result["profile_id"])
    _cap_text(result["slot_key"], 64)
    if (result["warehouse_id"] != profile["warehouse_id"]
            or result["location_id"] != profile["location_id"]
            or result["profile_id"] != profile["id"]):
        _cap_fail("capacity_signed_slot_scope")
    for name in _CAP_SLOT_NONNEGATIVE:
        result[name] = _cap_signed_decimal(result[name], positive=False)
    for name in _CAP_SLOT_POSITIVE:
        result[name] = _cap_signed_decimal(result[name], positive=True)
    return result


def _cap_slot_geometry(profile, slots):
    if not slots:
        return
    if (profile["location_id"] is None
            or any(profile[name] is None for name in (
                "length_mm", "width_mm", "height_mm",
            ))):
        _cap_fail("capacity_slot_envelope_unknown")

    length = Decimal(profile["length_mm"])
    height = Decimal(profile["height_mm"])
    width = Decimal(profile["width_mm"])
    keys = set()
    for slot in slots:
        _cap_slot_shape(slot)
        if slot["slot_key"] in keys:
            _cap_fail("capacity_signed_slot_duplicate")
        keys.add(slot["slot_key"])
        if (slot["warehouse_id"] != profile["warehouse_id"]
                or slot["location_id"] != profile["location_id"]
                or slot["profile_id"] != profile["id"]):
            _cap_fail("capacity_slot_parent_scope")
        if (Decimal(slot["x_mm"]) + Decimal(slot["width_mm"]) > length
                or Decimal(slot["y_mm"]) + Decimal(slot["height_mm"]) > height
                or Decimal(slot["z_mm"]) + Decimal(slot["depth_mm"]) > width):
            _cap_fail("capacity_slot_outside_envelope")

    for index, a in enumerate(slots):
        for b in slots[index + 1:]:
            intersects = (
                Decimal(a["x_mm"]) < Decimal(b["x_mm"]) + Decimal(b["width_mm"])
                and Decimal(b["x_mm"]) < Decimal(a["x_mm"]) + Decimal(a["width_mm"])
                and Decimal(a["y_mm"]) < Decimal(b["y_mm"]) + Decimal(b["height_mm"])
                and Decimal(b["y_mm"]) < Decimal(a["y_mm"]) + Decimal(a["height_mm"])
                and Decimal(a["z_mm"]) < Decimal(b["z_mm"]) + Decimal(b["depth_mm"])
                and Decimal(b["z_mm"]) < Decimal(a["z_mm"]) + Decimal(a["depth_mm"])
            )
            if intersects:
                _cap_fail("capacity_slot_intersection")


def _cap_signed_payload(profile):
    try:
        payload = json.loads(profile["canonical_payload"])
    except (TypeError, ValueError) as exc:
        raise ValueError("capacity_profile_payload_json") from exc
    if (not isinstance(payload, dict)
            or set(payload) != {"profile", "slots"}
            or not isinstance(payload["profile"], dict)
            or not isinstance(payload["slots"], list)
            or len(payload["slots"]) > 1000):
        _cap_fail("capacity_profile_payload_shape")

    expected_profile = _cap_profile_json(profile)
    # Canonical comparison distinguishes True/1 and 1/1.0.
    if _cap_json(payload["profile"]) != _cap_json(expected_profile):
        _cap_fail("capacity_profile_full_row_mismatch")

    typed_slots = [
        _cap_signed_slot(slot, profile)
        for slot in payload["slots"]
    ]
    _cap_slot_geometry(profile, typed_slots)
    expected_slots = sorted(
        [_cap_slot_json(slot) for slot in typed_slots],
        key=lambda slot: slot["slot_key"],
    )
    expected = {"profile": expected_profile, "slots": expected_slots}
    if (profile["canonical_payload"] != _cap_json(expected)
            or profile["digest"] != _cap_digest(expected)):
        _cap_fail("capacity_profile_canonical_or_digest")
    return payload


def _cap_actual_slots(connection, profile_id):
    table = PlacementSlot.__table__
    rows = connection.execute(
        select(table)
        .where(table.c.profile_id == profile_id)
        .order_by(table.c.slot_key)
    ).mappings().all()
    result = [dict(row) for row in rows]
    for values in result:
        _cap_slot_shape(values)
    return result


def _cap_full_snapshot(connection, profile):
    signed = _cap_signed_payload(profile)
    actual = _cap_actual_slots(connection, profile["id"])
    _cap_slot_geometry(profile, actual)
    expected_slots = sorted(
        [_cap_slot_json(row) for row in actual],
        key=lambda slot: slot["slot_key"],
    )

    signed_multiset = Counter(_cap_json(row) for row in signed["slots"])
    actual_multiset = Counter(_cap_json(row) for row in expected_slots)
    if signed_multiset != actual_multiset:
        _cap_fail("capacity_slot_multiset_mismatch")

    expected = {
        "profile": _cap_profile_json(profile),
        "slots": expected_slots,
    }
    if (profile["canonical_payload"] != _cap_json(expected)
            or profile["digest"] != _cap_digest(expected)):
        _cap_fail("capacity_profile_snapshot_mismatch")


def _cap_require_read_committed(connection):
    from modules.wms.physical_capacity import require_capacity_read_committed

    require_capacity_read_committed(connection)

# ---------- Mapper guards ----------

def cap_resource_before_insert(mapper, connection, target):
    _cap_require_read_committed(connection)
    values = _cap_target_values(target)
    _cap_uuid(values["id"])
    _cap_integer(values["warehouse_id"])
    _cap_evidence(values, database_value=False)
    if values["kind"] not in _CAP_RESOURCE_KINDS:
        _cap_fail("capacity_resource_kind")
    if values["topology_revision"] != 1:
        _cap_fail("capacity_resource_initial_revision")
    _cap_integer(values["topology_revision"])
    parent_id = values["parent_id"]
    if (values["kind"] == "warehouse") != (parent_id is None):
        _cap_fail("capacity_resource_root")
    _cap_lock_warehouse(connection, values["warehouse_id"])
    if parent_id is not None:
        _cap_uuid(parent_id)
        if parent_id == values["id"]:
            _cap_fail("capacity_resource_not_self")
        chain = _cap_chain(connection, values["warehouse_id"], parent_id)
        if any(row["resource_id"] == values["id"] for row in chain):
            _cap_fail("capacity_topology_cycle")


def cap_resource_before_update(mapper, connection, target):
    _cap_require_read_committed(connection)
    # Discovery is by the ORIGINAL mapper identity, never target.id.
    discovered = _cap_original_row(connection, LoadResource, target)
    _cap_lock_warehouse(connection, discovered["warehouse_id"])
    # Capture fresh immutable scalar data after the WH lock.
    old = _cap_original_row(connection, LoadResource, target)
    new = _cap_target_values(target, old)
    for name in ("id", "warehouse_id", "kind"):
        if not _cap_same_scalar(new[name], old[name]):
            _cap_fail("capacity_resource_identity")
    _cap_uuid(new["id"])
    _cap_integer(new["topology_revision"])
    _cap_evidence(new, database_value=False)
    if new["topology_revision"] != old["topology_revision"] + 1:
        _cap_fail("capacity_resource_revision_step")
    parent_id = new["parent_id"]
    if (new["kind"] == "warehouse") != (parent_id is None):
        _cap_fail("capacity_resource_root")
    if parent_id is not None:
        _cap_uuid(parent_id)
        chain = _cap_chain(connection, old["warehouse_id"], parent_id)
        if any(row["resource_id"] == old["id"] for row in chain):
            _cap_fail("capacity_topology_cycle")


def cap_resource_before_delete(mapper, connection, target):
    _cap_require_read_committed(connection)
    _cap_fail("capacity_resource_delete_forbidden")


def cap_mapping_before_insert(mapper, connection, target):
    values = _cap_target_values(target)
    _cap_integer(values["warehouse_id"])
    _cap_integer(values["location_id"])
    resource_id = values["resource_id"]
    expected = 0 if resource_id is None else 1
    supplied = values["resource_mapping_revision"]

    # Existing LocationSetup does not set the new column.
    # This is a topology generation, not an invented load/capacity.
    if supplied is None:
        target.resource_mapping_revision = expected
        supplied = expected
    _cap_integer(supplied, allow_zero=True)
    if supplied != expected:
        _cap_fail("capacity_mapping_initial_revision")

    if resource_id is not None:
        _cap_require_read_committed(connection)
    _cap_lock_warehouse(connection, values["warehouse_id"])
    if resource_id is None:
        # No query to LoadResource for an old unbound location.
        return
    _cap_uuid(resource_id)
    _cap_chain(connection, values["warehouse_id"], resource_id)


def cap_mapping_before_update(mapper, connection, target):
    discovered = _cap_original_row(connection, PhysicalLocation, target)
    proposed = _cap_target_values(target, discovered)
    mapping_mutation = (
        proposed["resource_id"] != discovered["resource_id"]
        or proposed["resource_mapping_revision"]
        != discovered["resource_mapping_revision"]
    )
    if mapping_mutation:
        _cap_require_read_committed(connection)
    _cap_lock_warehouse(connection, discovered["warehouse_id"])
    old = _cap_original_row(connection, PhysicalLocation, target)
    new = _cap_target_values(target, old)

    for name in ("warehouse_id", "location_id"):
        if not _cap_same_scalar(new[name], old[name]):
            _cap_fail("capacity_location_identity")
    _cap_integer(new["resource_mapping_revision"], allow_zero=True)

    changed = new["resource_id"] != old["resource_id"]
    expected = old["resource_mapping_revision"] + (1 if changed else 0)
    if new["resource_mapping_revision"] != expected:
        _cap_fail("capacity_mapping_revision_step")
    if new["resource_id"] is not None:
        _cap_uuid(new["resource_id"])
        _cap_chain(connection, old["warehouse_id"], new["resource_id"])
    # None transitions retain/increment the existing generation.
    # No profile/slot/position or historical receipt is rewritten.


def cap_profile_before_insert(mapper, connection, target):
    _cap_require_read_committed(connection)
    values = _cap_target_values(target)
    _cap_lock_warehouse(
        connection, values["warehouse_id"], values["organization_id"],
    )
    _cap_profile_shape(connection, values, database_value=False)
    if values["sealed"] is not False:
        _cap_fail("capacity_profile_requires_draft")

    table = PlacementProfile.__table__
    conditions = [
        table.c.warehouse_id == values["warehouse_id"],
    ]
    for name in ("packaging_id", "location_id", "resource_id"):
        value = values[name]
        conditions.append(
            table.c[name].is_(None)
            if value is None else table.c[name] == value
        )
    prior = connection.execute(
        select(func.max(table.c.revision)).where(*conditions)
    ).scalar_one()
    if values["revision"] != (prior or 0) + 1:
        _cap_fail("capacity_profile_revision_step")

    # Validate the entire signed future child set now.
    # Its match to actual rows is checked again at seal and commit.
    _cap_signed_payload(values)


def cap_profile_before_update(mapper, connection, target):
    _cap_require_read_committed(connection)
    discovered = _cap_original_row(connection, PlacementProfile, target)
    _cap_lock_warehouse(
        connection, discovered["warehouse_id"], discovered["organization_id"],
    )
    old = _cap_original_row(connection, PlacementProfile, target)
    new = _cap_target_values(target, old)
    if old["sealed"] is not False or new["sealed"] is not True:
        _cap_fail("capacity_profile_immutable")
    for column in PlacementProfile.__table__.columns:
        if column.name == "sealed":
            continue
        if not _cap_same_scalar(new[column.name], old[column.name]):
            _cap_fail("capacity_profile_immutable")

    _cap_profile_shape(connection, new, database_value=True)
    _cap_full_snapshot(connection, new)


def cap_profile_before_delete(mapper, connection, target):
    _cap_require_read_committed(connection)
    _cap_fail("capacity_profile_delete_forbidden")


def cap_slot_before_insert(mapper, connection, target):
    _cap_require_read_committed(connection)
    values = _cap_target_values(target)
    _cap_slot_shape(values)
    _cap_lock_warehouse(connection, values["warehouse_id"])
    table = PlacementProfile.__table__
    row = connection.execute(
        select(table)
        .where(table.c.id == values["profile_id"])
        .with_for_update()
    ).mappings().one_or_none()
    if row is None or row["sealed"] is not False:
        _cap_fail("capacity_slot_parent_not_draft")
    if (row["warehouse_id"] != values["warehouse_id"]
            or row["location_id"] != values["location_id"]):
        _cap_fail("capacity_slot_parent_scope")


def cap_slot_before_update(mapper, connection, target):
    _cap_require_read_committed(connection)
    _cap_fail("capacity_slot_immutable")


def cap_slot_before_delete(mapper, connection, target):
    _cap_require_read_committed(connection)
    _cap_fail("capacity_slot_immutable")


# ---------- Session-local deferred check ----------

def _cap_pending_profile_ids(session):
    result = set()
    for target in tuple(session.new) + tuple(session.dirty):
        if isinstance(target, PlacementProfile):
            identity = inspect(target).identity
            profile_id = (
                identity[0] if identity is not None
                else _cap_target_values(target)["id"]
            )
            result.add(_cap_uuid(profile_id))
        elif isinstance(target, PlacementSlot):
            # Dirty slots cannot pass their mapper guard.
            result.add(_cap_uuid(
                _cap_target_values(target)["profile_id"]
            ))
    return result


def _cap_tracking(session, *, create):
    root = session.get_transaction()
    state = session.info.get(_CAP_SESSION_KEY)
    if state is not None and state["root"] is not root:
        session.info.pop(_CAP_SESSION_KEY, None)
        state = None
    if state is None and create:
        if root is None:
            _cap_fail("capacity_transaction_missing")
        state = {
            "root": root,
            "levels": {},
            "equipment_levels": {},
            "measurement_levels": {},
            "committed": set(),
        }
        session.info[_CAP_SESSION_KEY] = state
    return state


def cap_session_before_flush(session, flush_context, instances):
    _equipment_assert_loaded(session)
    _measurement_assert_loaded(session)
    from modules.wms.placement_models import _work_assert_loaded
    _work_assert_loaded(session)
    profile_ids = _cap_pending_profile_ids(session)
    if not profile_ids:
        # Pure Python return: zero new-table query.
        return
    state = _cap_tracking(session, create=True)
    level = session.get_nested_transaction() or session.get_transaction()
    if level is None:
        _cap_fail("capacity_transaction_missing")
    state["levels"].setdefault(level, set()).update(profile_ids)


def _cap_profile_session_before_commit(session):
    pending = _cap_pending_profile_ids(session)
    state = _cap_tracking(session, create=False)
    if not pending and (state is None or not any(state["levels"].values())):
        return
    if session._flushing:
        _cap_fail("capacity_commit_during_flush")

    # Newly added profiles must become tracked before the deferred check.
    session.flush()
    state = _cap_tracking(session, create=False)
    if state is None:
        return

    profile_ids = set()
    for ids in state["levels"].values():
        profile_ids.update(ids)
    if not profile_ids:
        return

    connection = session.connection(
        bind_arguments={"mapper": PlacementProfile},
    )
    _cap_require_read_committed(connection)
    table = PlacementProfile.__table__
    discovered = connection.execute(
        select(table.c.id, table.c.organization_id, table.c.warehouse_id)
        .where(table.c.id.in_(sorted(profile_ids)))
    ).mappings().all()
    if {row["id"] for row in discovered} != profile_ids:
        _cap_fail("capacity_tracked_profile_missing")

    # Entire touched footprint before revalidating any chain/profile.
    keys = sorted({
        (row["organization_id"], row["warehouse_id"])
        for row in discovered
    })
    for org_id, warehouse_id in keys:
        _cap_lock_warehouse(connection, warehouse_id, org_id)

    fresh_rows = connection.execute(
        select(table)
        .where(table.c.id.in_(sorted(profile_ids)))
        .order_by(table.c.organization_id, table.c.warehouse_id, table.c.id)
    ).mappings().all()
    if {row["id"] for row in fresh_rows} != profile_ids:
        _cap_fail("capacity_tracked_profile_missing")
    for row in fresh_rows:
        values = dict(row)
        if values["sealed"] is not True:
            _cap_fail("capacity_profile_unsealed_commit")
        _cap_profile_shape(connection, values, database_value=True)
        _cap_full_snapshot(connection, values)


def cap_session_before_commit(session):
    _equipment_assert_loaded(session)
    _measurement_assert_loaded(session)
    if _measurement_obligations(session) and session._flushing:
        _cap_fail("capacity_commit_during_flush")
    if _equipment_obligations(session) and session._flushing:
        _cap_fail("capacity_commit_during_flush")
    # The legacy profile guard retains every branch and flush; equipment runs after it.
    _cap_profile_session_before_commit(session)
    if _equipment_obligations(session):
        session.flush()
        if session.new or session.dirty or session.deleted:
            _cap_fail("equipment_changes_after_final_flush")
        if session.get_nested_transaction() is None:
            cap_validate_equipment_root(session)
            if session.new or session.dirty or session.deleted:
                _cap_fail("equipment_changes_after_final_guard")
    if _measurement_obligations(session):
        session.flush()
        if session.new or session.dirty or session.deleted:
            _cap_fail("measurement_changes_after_final_flush")
        if session.get_nested_transaction() is None:
            cap_validate_measurement_root(session)
            if session.new or session.dirty or session.deleted:
                _cap_fail("measurement_changes_after_final_guard")
    from modules.wms.placement_models import cap_work_before_commit
    cap_work_before_commit(session)
    from modules.wms.placement_models import cap_placement_before_commit
    cap_placement_before_commit(session)


def cap_session_after_commit(session):
    state = _cap_tracking(session, create=False)
    if state is None:
        return
    level = session.get_nested_transaction() or session.get_transaction()
    if level is not None:
        state["committed"].add(level)


def cap_session_after_transaction_end(session, transaction):
    state = session.info.get(_CAP_SESSION_KEY)
    if state is None:
        return
    if transaction is state["root"]:
        session.info.pop(_CAP_SESSION_KEY, None)
        session.info.pop("wms_work_final_authority_v1", None)
        session.info.pop("wms_placement_final_authority_v1", None)
        return

    # Flush-internal transactions do not own a tracked level.
    if not transaction.nested:
        return

    ids = state["levels"].pop(transaction, set())
    equipment = state["equipment_levels"].pop(transaction, {})
    measurement = state["measurement_levels"].pop(transaction, {})
    committed = transaction in state["committed"]
    state["committed"].discard(transaction)
    from modules.wms.placement_models import cap_work_transaction_end
    cap_work_transaction_end(session, transaction, state, committed)
    from modules.wms.placement_models import cap_placement_transaction_end
    cap_placement_transaction_end(session, transaction, state, committed)
    if committed and equipment:
        parent = transaction.parent
        if parent is None:
            _cap_fail("equipment_nested_parent_missing")
        parent_entries = state["equipment_levels"].setdefault(parent, {})
        for key, item in equipment.items():
            if key in parent_entries and parent_entries[key]["expected"] != item["expected"]:
                _cap_fail("equipment_nested_obligation_conflict")
            if key not in parent_entries:
                parent_entries[key] = {**item, "authorities": list(item["authorities"])}
            else:
                outer = parent_entries[key]
                if outer["persistent_pk"] is not None and item["persistent_pk"] is not None and outer["persistent_pk"] != item["persistent_pk"]:
                    _cap_fail("equipment_persistent_identity_conflict")
                outer["insert"] = outer["insert"] or item["insert"]
                if outer["persistent_pk"] is None:
                    outer["persistent_pk"] = item["persistent_pk"]
                outer["authorities"].extend(item["authorities"])
    if committed and measurement:
        parent = transaction.parent
        if parent is None:
            _cap_fail("measurement_nested_parent_missing")
        parent_entries = state["measurement_levels"].setdefault(parent, {})
        for key, item in measurement.items():
            if key in parent_entries and parent_entries[key]["expected"] != item["expected"]:
                _cap_fail("measurement_nested_obligation_conflict")
            if key not in parent_entries:
                parent_entries[key] = {**item, "authorities": list(item["authorities"])}
            else:
                outer = parent_entries[key]
                if outer["persistent_pk"] is not None and item["persistent_pk"] is not None and outer["persistent_pk"] != item["persistent_pk"]:
                    _cap_fail("measurement_persistent_identity_conflict")
                outer["insert"] = outer["insert"] or item["insert"]
                if outer["persistent_pk"] is None:
                    outer["persistent_pk"] = item["persistent_pk"]
                outer["authorities"].extend(item["authorities"])
    if committed and ids:
        parent = transaction.parent
        if parent is None:
            _cap_fail("capacity_nested_parent_missing")
        state["levels"].setdefault(parent, set()).update(ids)
    # A rolled-back SAVEPOINT does not leak its newly created profile IDs.


def cap_session_after_soft_rollback(session, previous_transaction):
    state = session.info.get(_CAP_SESSION_KEY)
    if state is None:
        return
    if previous_transaction is state["root"]:
        session.info.pop(_CAP_SESSION_KEY, None)
        session.info.pop("wms_work_final_authority_v1", None)
        return
    state["levels"].pop(previous_transaction, None)
    state["equipment_levels"].pop(previous_transaction, None)
    state["measurement_levels"].pop(previous_transaction, None)
    state.setdefault("work_levels", {}).pop(previous_transaction, None)
    state["committed"].discard(previous_transaction)


def cap_capacity_record_isolation(mapper, connection, target):
    _cap_require_read_committed(connection)

# ---------- Attach once in physical_models.py ----------

event.listen(LoadResource, "before_insert", cap_resource_before_insert)
event.listen(LoadResource, "before_update", cap_resource_before_update)
event.listen(LoadResource, "before_delete", cap_resource_before_delete)

for model in (PhysicalCarrier, CarrierPosition):
    event.listen(model, "before_insert", cap_capacity_record_isolation)
    event.listen(model, "before_update", cap_capacity_record_isolation)
    event.listen(model, "before_delete", cap_capacity_record_isolation)

event.listen(PhysicalLocation, "before_insert", cap_mapping_before_insert)
event.listen(PhysicalLocation, "before_update", cap_mapping_before_update)

event.listen(PlacementProfile, "before_insert", cap_profile_before_insert)
event.listen(PlacementProfile, "before_update", cap_profile_before_update)
event.listen(PlacementProfile, "before_delete", cap_profile_before_delete)

event.listen(PlacementSlot, "before_insert", cap_slot_before_insert)
event.listen(PlacementSlot, "before_update", cap_slot_before_update)
event.listen(PlacementSlot, "before_delete", cap_slot_before_delete)

event.listen(Session, "before_flush", cap_session_before_flush)
event.listen(Session, "before_commit", cap_session_before_commit)
event.listen(Session, "after_commit", cap_session_after_commit)
event.listen(Session, "after_transaction_end", cap_session_after_transaction_end)
event.listen(Session, "after_soft_rollback", cap_session_after_soft_rollback)

def _packaging_catalog_before_write(mapper, connection, target):
    """Keep issued package identities stable, including expired/unloaded writes."""
    state = inspect(target)
    old = None
    if state.identity is not None:
        table = Packaging.__table__
        old = connection.execute(
            select(table).where(table.c.id == state.identity[0]).with_for_update()
        ).mappings().one_or_none()
        if old is None:
            raise ValueError("Не найдена исходная запись упаковки")
    values = _cap_target_values(target, old)
    if old is not None:
        for name in ("id", "organization_id", "sku_code", "factor", "sealed_allowed"):
            if not _cap_same_scalar(values[name], old[name]):
                raise ValueError("Идентичность и коэффициент упаковки неизменяемы; создайте новую упаковку")
    barcode_written = old is None or state.attrs.barcode.history.has_changes()
    if barcode_written and (old is None or values["barcode"] != old["barcode"]):
        barcode = values["barcode"]
        if isinstance(barcode, str) and barcode.strip().casefold().startswith(("sku-", "erp-p-")):
            raise ValueError("Префиксы SKU- и ERP-P- зарезервированы для внутренних кодов")


def _packaging_catalog_before_delete(mapper, connection, target):
    raise ValueError("Упаковку нельзя удалять: её идентификатор используется в складской истории")


event.listen(Packaging, "before_insert", _packaging_catalog_before_write)
event.listen(Packaging, "before_update", _packaging_catalog_before_write)
event.listen(Packaging, "before_delete", _packaging_catalog_before_delete)


def immutable_physical_history(mapper, connection, target):
    raise ValueError("Physical operation receipts and ledger entries are immutable")


def immutable_delivery_binding(mapper, connection, target):
    raise ValueError("Delivery binding revisions are immutable")


def preserve_task_binding_identity(mapper, connection, target):
    history = inspect(target).attrs.binding_revision_id.history
    if history.has_changes() and history.deleted and history.deleted[0] is not None:
        raise ValueError("A started delivery task cannot be rebound")


def preserve_pick_delivery_identity(mapper, connection, target):
    state = inspect(target)
    if any(state.attrs[name].history.has_changes() for name in ("task_id", "binding_revision_id")):
        raise ValueError("Pick task and delivery attribution are immutable")


def _original_value(target, name):
    history = inspect(target).attrs[name].history
    return history.deleted[0] if history.deleted else getattr(target, name)


def immutable_assembly_execution(mapper, connection, target):
    state = inspect(target)
    immutable = ("id", "organization_id", "warehouse_id", "order_id", "specification_id",
        "specification_digest", "source_snapshot", "container", "output_keys", "output_qty",
        "bind_operation_id", "actor", "created_at")
    if any(state.attrs[name].history.has_changes() for name in immutable):
        raise ValueError("Assembly execution binding is immutable")
    if _original_value(target, "status") == "closed" and target.status != "closed":
        raise ValueError("Closed assembly execution cannot be reopened")
    old_keys = _original_value(target, "consumed_output_keys") or []
    new_keys = target.consumed_output_keys or []
    if len(new_keys) != len(set(new_keys)) or not set(old_keys).issubset(new_keys):
        raise ValueError("Consumed assembly output keys are append-only")


def immutable_assembly_execution_delete(mapper, connection, target):
    raise ValueError("Assembly execution history cannot be deleted")


def immutable_assembly_input_link(mapper, connection, target):
    state = inspect(target)
    immutable = ("id", "execution_id", "specification_line_id", "hold_id", "original_unit_id",
        "actual_kit_unit_id", "kit_move_operation_id", "qty", "source_dimensions", "kit_dimensions", "created_at")
    if any(state.attrs[name].history.has_changes() for name in immutable):
        raise ValueError("Assembly input link identity is immutable")
    if (target.consumed_qty < _original_value(target, "consumed_qty")
            or target.returned_qty < _original_value(target, "returned_qty")):
        raise ValueError("Assembly input link lifecycle counters are append-only")


def immutable_assembly_input_link_delete(mapper, connection, target):
    raise ValueError("Assembly input link history cannot be deleted")


event.listen(AssemblyExecution, "before_update", immutable_assembly_execution)
event.listen(AssemblyExecution, "before_delete", immutable_assembly_execution_delete)
event.listen(AssemblyInputLink, "before_update", immutable_assembly_input_link)
event.listen(AssemblyInputLink, "before_delete", immutable_assembly_input_link_delete)


for history_model in (PhysicalOperation, PhysicalLedger, AssemblyConsumption, AssemblyProcessRun, AssemblyOutputIdentity):
    event.listen(history_model, "before_update", immutable_physical_history)
    event.listen(history_model, "before_delete", immutable_physical_history)

event.listen(DeliveryBindingRevision, "before_update", immutable_delivery_binding)
event.listen(DeliveryBindingRevision, "before_delete", immutable_delivery_binding)
event.listen(PhysicalTask, "before_update", preserve_task_binding_identity)
event.listen(PickedAllocation, "before_update", preserve_pick_delivery_identity)


def preserve_assembly_unit_identity(mapper, connection, target):
    for name in ("stock_state", "dispatch_operation_id"):
        if inspect(target).attrs[name].history.has_changes():
            raise CurrentStockStateError("Stock unit state and dispatch identity cannot change")
    old = _original_value(target, "assembly_output_id")
    if old is not None and old != target.assembly_output_id:
        raise ValueError("Assembly unit identity cannot change")


event.listen(StockUnit, "before_update", preserve_assembly_unit_identity)

# Register the separate placement declarations through this existing metadata entry point.
from modules.wms import placement_models as placement_models  # noqa: E402, F401

# Equipment obligations share the existing root/savepoint lifetime, never authority flags.
_EQUIPMENT_OLD_SNAPSHOT = "wms_equipment_original_columns_v1"


def _equipment_columns(target):
    # Passive only: never load an expired attribute during a mapper/session callback.
    values = inspect(target).dict
    return {key: values[key] for key in ("id", "organization_id", "warehouse_id", "operation_id", "kind", "request_hash", "result", "actor") if key in values}


def cap_freeze_equipment_receipt(session, value, *, insert, authority, persistent_pk=None):
    from modules.wms.physical_schemas import equipment_receipt

    equipment_receipt(value)
    state = _cap_tracking(session, create=True)
    level = session.get_nested_transaction() or session.get_transaction()
    key = (value["organization_id"], value["warehouse_id"], value["operation_id"])
    expected = _cap_json(value)
    entries = state["equipment_levels"].setdefault(level, {})
    inherited = _equipment_obligations(session).get(key)
    if inherited is not None:
        if inherited["expected"] != expected:
            _cap_fail("equipment_producer_obligation_conflict")
        insert = insert or inherited["insert"]
        if persistent_pk is None:
            persistent_pk = inherited["persistent_pk"]
        elif inherited["persistent_pk"] is not None and persistent_pk != inherited["persistent_pk"]:
            _cap_fail("equipment_persistent_identity_conflict")
    old = entries.get(key)
    # Keep only this level's callbacks here: rollback can then discard exactly this level.
    authorities = [] if old is None else list(old["authorities"])
    authorities.append(authority)
    entries[key] = {"expected": expected, "insert": insert, "authorities": authorities, "persistent_pk": persistent_pk}


def _equipment_obligations(session):
    state = _cap_tracking(session, create=False)
    result = {}
    if state is not None:
        for entries in state["equipment_levels"].values():
            for key, value in entries.items():
                if key not in result:
                    result[key] = {**value, "authorities": list(value["authorities"])}
                    continue
                old = result[key]
                if old["expected"] != value["expected"]:
                    _cap_fail("equipment_duplicate_obligation")
                if old["persistent_pk"] is not None and value["persistent_pk"] is not None and old["persistent_pk"] != value["persistent_pk"]:
                    _cap_fail("equipment_persistent_identity_conflict")
                old["insert"] = old["insert"] or value["insert"]
                if old["persistent_pk"] is None:
                    old["persistent_pk"] = value["persistent_pk"]
                old["authorities"].extend(value["authorities"])
    return result


def _equipment_assert_loaded(session):
    from modules.wms.physical_capacity import equipment_operation_indicator
    obligations = _equipment_obligations(session)
    for target in tuple(session.new) + tuple(session.identity_map.values()):
        if not isinstance(target, PhysicalOperation):
            continue
        state = inspect(target)
        columns = _equipment_columns(target)
        snapshot = state.info.get(_EQUIPMENT_OLD_SNAPSHOT)
        if snapshot is not None:
            import json
            original = json.loads(snapshot)
            if any(_cap_json(value) != _cap_json(original.get(key)) for key, value in columns.items()):
                _cap_fail("equipment_persistent_json_or_identity_mutated")
        key = (columns.get("organization_id"), columns.get("warehouse_id"), columns.get("operation_id"))
        obligation = obligations.get(key)
        indicator = equipment_operation_indicator(columns.get("kind"), columns.get("result"))
        if target in session.new and indicator and (obligation is None or not obligation["insert"]):
            _cap_fail("equipment_insert_without_verified_producer")
        if obligation is not None:
            import json
            value = json.loads(obligation["expected"])
            expected = {name: value[name] for name in ("organization_id", "warehouse_id", "operation_id", "kind", "request_hash")}
            expected.update(result=value, actor=value["actor_sub"])
            if any(name not in columns or _cap_json(columns[name]) != _cap_json(item) for name, item in expected.items()):
                _cap_fail("equipment_loaded_receipt_mismatch")
            pk = obligation["persistent_pk"]
            if pk is not None and (columns.get("id", pk) != pk or (state.identity is not None and state.identity != (pk,))):
                _cap_fail("equipment_persistent_identity_mutated")
            if target in session.new and pk is None and columns.get("id") is not None:
                _cap_fail("equipment_insert_primary_key_must_be_server_generated")


def cap_equipment_before_insert(mapper, connection, target):
    from sqlalchemy.orm import object_session

    from modules.wms.physical_capacity import equipment_operation_indicator
    values = _equipment_columns(target)
    if equipment_operation_indicator(values.get("kind"), values.get("result")):
        session = object_session(target)
        if session is None:
            _cap_fail("equipment_producer_session_missing")
        _cap_require_read_committed(connection)
        _equipment_assert_loaded(session)


def cap_equipment_after_insert(mapper, connection, target):
    session = inspect(target).session
    columns = _equipment_columns(target)
    key = (columns.get("organization_id"), columns.get("warehouse_id"), columns.get("operation_id"))
    state = _cap_tracking(session, create=False)
    if state is not None:
        tracked = False
        for entries in state["equipment_levels"].values():
            obligation = entries.get(key)
            if obligation is not None:
                if obligation["persistent_pk"] is not None and obligation["persistent_pk"] != columns["id"]:
                    _cap_fail("equipment_persistent_identity_conflict")
                obligation["persistent_pk"] = columns["id"]
                tracked = True
        if tracked:
            inspect(target).info[_EQUIPMENT_OLD_SNAPSHOT] = _cap_json(columns)


def cap_equipment_loaded(target, context):
    from modules.wms.physical_capacity import equipment_operation_indicator
    state = inspect(target)
    values = _equipment_columns(target)
    if equipment_operation_indicator(values.get("kind"), values.get("result")) and _EQUIPMENT_OLD_SNAPSHOT not in state.info:
        state.info[_EQUIPMENT_OLD_SNAPSHOT] = _cap_json(values)


def cap_equipment_refreshed(target, context, attrs):
    cap_equipment_loaded(target, context)


def cap_validate_equipment_root(session):
    from datetime import datetime, timezone

    from modules.wms.physical_capacity import (
        equipment_current_binding,
        equipment_operation_receipt,
        equipment_state_heads,
    )
    obligations = _equipment_obligations(session)
    _equipment_assert_loaded(session)
    if not obligations:
        return
    connection = session.connection(bind_arguments={"mapper": PhysicalOperation})
    _cap_require_read_committed(connection)
    scopes = sorted({key[:2] for key in obligations})
    for org, wh in scopes:
        _cap_lock_warehouse(connection, wh, org)
    heads = {scope: equipment_state_heads(connection, *scope) for scope in scopes}
    table = PhysicalOperation.__table__
    for (org, wh, operation_id), obligation in obligations.items():
        row = connection.execute(select(table).where(table.c.organization_id == org,
            table.c.warehouse_id == wh, table.c.operation_id == operation_id)).mappings().one_or_none()
        if row is None or row["id"] != obligation["persistent_pk"]:
            _cap_fail("equipment_tracked_receipt_missing_or_identity_changed")
        value = equipment_operation_receipt(row, connection)
        if _cap_json(value) != obligation["expected"]:
            _cap_fail("equipment_tracked_receipt_mutated_or_resealed")
        head = heads[(org, wh)][value["equipment_resource_id"]]
        if obligation["insert"] and head["operation_id"] == operation_id:
            equipment_current_binding(connection, value)
            until = datetime.fromisoformat(value["request"]["valid_until"].replace("Z", "+00:00"))
            if datetime.now(timezone.utc) >= until:
                _cap_fail("equipment_terminal_observation_expired_before_commit")


async def cap_finish_equipment_root(async_session):
    session = async_session.sync_session
    if not _equipment_obligations(session) and not _measurement_obligations(session):
        return
    from fastapi import HTTPException
    try:
        await async_session.flush()
        with async_session.no_autoflush:
            for obligation in (*_equipment_obligations(session).values(), *_measurement_obligations(session).values()):
                for authority in obligation["authorities"]:
                    await authority()
        if async_session.new or async_session.dirty or async_session.deleted:
            _cap_fail("equipment_changes_after_final_flush")
        await async_session.run_sync(cap_validate_equipment_root)
        await async_session.run_sync(cap_validate_measurement_root)
        with async_session.no_autoflush:
            for obligation in _measurement_obligations(session).values():
                for authority in obligation["authorities"]:
                    await authority()
        _measurement_assert_loaded(session)
        if async_session.new or async_session.dirty or async_session.deleted:
            _cap_fail("equipment_changes_after_final_guard")
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


event.listen(PhysicalOperation, "before_insert", cap_equipment_before_insert)
event.listen(PhysicalOperation, "after_insert", cap_equipment_after_insert)
event.listen(PhysicalOperation, "load", cap_equipment_loaded)
event.listen(PhysicalOperation, "refresh", cap_equipment_refreshed)



_MEASUREMENT_OLD_SNAPSHOT = "wms_measurement_original_columns_v1"


def cap_freeze_measurement_receipt(session, value, *, insert, authority, persistent_pk=None):
    from modules.wms.physical_capacity import measurement_public_result

    measurement_public_result(value, "capacity_measurements_approve")
    proof = value["_placement_measured_context_v1"]
    state = _cap_tracking(session, create=True)
    level = session.get_nested_transaction() or session.get_transaction()
    key = (proof["organization_id"], proof["warehouse_id"], proof["operation_id"])
    expected = _cap_json(value)
    entries = state["measurement_levels"].setdefault(level, {})
    inherited = _measurement_obligations(session).get(key)
    if inherited is not None:
        if inherited["expected"] != expected:
            _cap_fail("measurement_producer_obligation_conflict")
        insert = insert or inherited["insert"]
        if persistent_pk is None:
            persistent_pk = inherited["persistent_pk"]
        elif inherited["persistent_pk"] is not None and persistent_pk != inherited["persistent_pk"]:
            _cap_fail("measurement_persistent_identity_conflict")
    old = entries.get(key)
    # Keep only this level's callbacks here: rollback can then discard exactly this level.
    authorities = [] if old is None else list(old["authorities"])
    authorities.append(authority)
    entries[key] = {"expected": expected, "insert": insert, "authorities": authorities, "persistent_pk": persistent_pk}


def _measurement_obligations(session):
    state = _cap_tracking(session, create=False)
    result = {}
    if state is not None:
        for entries in state["measurement_levels"].values():
            for key, value in entries.items():
                if key not in result:
                    result[key] = {**value, "authorities": list(value["authorities"])}
                    continue
                old = result[key]
                if old["expected"] != value["expected"]:
                    _cap_fail("measurement_duplicate_obligation")
                if old["persistent_pk"] is not None and value["persistent_pk"] is not None and old["persistent_pk"] != value["persistent_pk"]:
                    _cap_fail("measurement_persistent_identity_conflict")
                old["insert"] = old["insert"] or value["insert"]
                if old["persistent_pk"] is None:
                    old["persistent_pk"] = value["persistent_pk"]
                old["authorities"].extend(value["authorities"])
    return result


def _measurement_assert_loaded(session):
    from modules.wms.physical_capacity import measurement_operation_indicator, measurement_public_result
    obligations = _measurement_obligations(session)
    for target in tuple(session.new) + tuple(session.identity_map.values()):
        if not isinstance(target, PhysicalOperation):
            continue
        state = inspect(target)
        columns = _equipment_columns(target)
        snapshot = state.info.get(_MEASUREMENT_OLD_SNAPSHOT)
        if snapshot is not None:
            import json
            original = json.loads(snapshot)
            if any(_cap_json(value) != _cap_json(original.get(key)) for key, value in columns.items()):
                _cap_fail("measurement_persistent_json_or_identity_mutated")
        key = (columns.get("organization_id"), columns.get("warehouse_id"), columns.get("operation_id"))
        obligation = obligations.get(key)
        indicator = measurement_operation_indicator(columns.get("kind"), columns.get("result"))
        if indicator:
            measurement_public_result(columns.get("result"), columns.get("kind"))
        if target in session.new and indicator and (obligation is None or not obligation["insert"]):
            _cap_fail("measurement_insert_without_verified_producer")
        if obligation is not None:
            import json
            value = json.loads(obligation["expected"])
            proof = value["_placement_measured_context_v1"]
            expected = {name: proof[name] for name in ("organization_id", "warehouse_id", "operation_id", "kind", "request_hash")}
            expected.update(result=value, actor=proof["approver"]["sub"])
            if any(name not in columns or _cap_json(columns[name]) != _cap_json(item) for name, item in expected.items()):
                _cap_fail("measurement_loaded_receipt_mismatch")
            pk = obligation["persistent_pk"]
            if pk is not None and (columns.get("id", pk) != pk or (state.identity is not None and state.identity != (pk,))):
                _cap_fail("measurement_persistent_identity_mutated")
            if target in session.new and pk is None and columns.get("id") is not None:
                _cap_fail("measurement_insert_primary_key_must_be_server_generated")


def cap_measurement_before_insert(mapper, connection, target):
    from sqlalchemy.orm import object_session

    from modules.wms.physical_capacity import measurement_operation_indicator
    values = _equipment_columns(target)
    if measurement_operation_indicator(values.get("kind"), values.get("result")):
        session = object_session(target)
        if session is None:
            _cap_fail("measurement_producer_session_missing")
        _cap_require_read_committed(connection)
        _measurement_assert_loaded(session)


def cap_measurement_after_insert(mapper, connection, target):
    session = inspect(target).session
    columns = _equipment_columns(target)
    key = (columns.get("organization_id"), columns.get("warehouse_id"), columns.get("operation_id"))
    state = _cap_tracking(session, create=False)
    if state is not None:
        tracked = False
        for entries in state["measurement_levels"].values():
            obligation = entries.get(key)
            if obligation is not None:
                if obligation["persistent_pk"] is not None and obligation["persistent_pk"] != columns["id"]:
                    _cap_fail("measurement_persistent_identity_conflict")
                obligation["persistent_pk"] = columns["id"]
                tracked = True
        if tracked:
            inspect(target).info[_MEASUREMENT_OLD_SNAPSHOT] = _cap_json(columns)


def cap_measurement_loaded(target, context):
    from modules.wms.physical_capacity import measurement_operation_indicator
    state = inspect(target)
    values = _equipment_columns(target)
    if measurement_operation_indicator(values.get("kind"), values.get("result")) and _MEASUREMENT_OLD_SNAPSHOT not in state.info:
        state.info[_MEASUREMENT_OLD_SNAPSHOT] = _cap_json(values)


def cap_measurement_refreshed(target, context, attrs):
    cap_measurement_loaded(target, context)


def cap_validate_measurement_root(session):
    from modules.wms.physical_capacity import measurement_current_binding, measurement_receipt, measurement_state_head
    obligations = _measurement_obligations(session)
    _measurement_assert_loaded(session)
    if not obligations:
        return
    connection = session.connection(bind_arguments={"mapper":PhysicalOperation})
    _cap_require_read_committed(connection)
    scopes=sorted({key[:2] for key in obligations})
    for org,wh in scopes:
        _cap_lock_warehouse(connection,wh,org)
    heads={scope:measurement_state_head(connection,*scope) for scope in scopes}
    table=PhysicalOperation.__table__
    for (org,wh,operation_id),obligation in obligations.items():
        row=connection.execute(select(table).where(table.c.organization_id==org,table.c.warehouse_id==wh,table.c.operation_id==operation_id)).mappings().one_or_none()
        if row is None or row["id"] != obligation["persistent_pk"]:
            _cap_fail("measurement_tracked_receipt_missing_or_identity_changed")
        value=measurement_receipt(row["result"],row,connection)
        if _cap_json(value) != obligation["expected"]:
            _cap_fail("measurement_tracked_receipt_mutated_or_resealed")
        proof=value["_placement_measured_context_v1"]
        if obligation["insert"] and heads[(org,wh)]["operation_id"]==operation_id:
            measurement_current_binding(connection,proof)


event.listen(PhysicalOperation,"before_insert",cap_measurement_before_insert)
event.listen(PhysicalOperation,"after_insert",cap_measurement_after_insert)
event.listen(PhysicalOperation,"load",cap_measurement_loaded)
event.listen(PhysicalOperation,"refresh",cap_measurement_refreshed)
