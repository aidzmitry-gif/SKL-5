"""Immutable WMS source documents for material issued to an owned production order."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator
from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    event,
    func,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base
from modules.production.accounting_ownership import ProductionOrderOwnership, order_snapshot
from modules.production.models import ProductionOrder
from modules.wms.invoice_shipments import guard_free
from modules.wms.models import Location, StockMovement, WarehouseDirectory
from modules.wms.physical_models import WarehouseControl


class ProductionMaterialIssueCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    request_id: UUID
    order_id: int = Field(gt=0, strict=True)
    expected_order_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    operation_date: date
    sku_code: str = Field(min_length=1, max_length=64)
    quantity: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    warehouse: str = Field(min_length=1, max_length=128)
    location_id: int | None = Field(default=None, gt=0, strict=True)
    lot: str = Field(min_length=1, max_length=64)
    evidence: str = Field(min_length=10, max_length=255)

    @field_serializer("quantity")
    def serialize_quantity(self, value: Decimal):
        return format(value, ".2f")

    @field_validator("quantity", mode="before")
    @classmethod
    def exact_quantity(cls, value):
        if not isinstance(value, (str, Decimal)):
            raise ValueError("Material issue quantity requires an exact decimal string")
        return value


class ProductionMaterialIssue(Base):
    """Permanent source binding between one physical issue and one production order."""

    __tablename__ = "production_material_issue"
    __table_args__ = (
        UniqueConstraint("organization_id", "request_key", name="uq_production_material_issue_request"),
        UniqueConstraint("movement_id", name="uq_production_material_issue_movement"),
        {"schema": "wms"},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(nullable=False, index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("production.order_ownership.order_id"), nullable=False, index=True)
    movement_id: Mapped[int] = mapped_column(ForeignKey("wms.stock_movement.id"), nullable=False)
    request_key: Mapped[str] = mapped_column(String(36), nullable=False)
    operation_date: Mapped[date] = mapped_column(Date, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def immutable(mapper, connection, target):
    raise ValueError("Production material issue binding is immutable")


event.listen(ProductionMaterialIssue, "before_update", immutable)
event.listen(ProductionMaterialIssue, "before_delete", immutable)


def _source(request_id: UUID) -> str:
    return f"production_material:{request_id}"


async def _owned_order(session, organization_id: int, command: ProductionMaterialIssueCommand):
    order = await session.scalar(select(ProductionOrder).join(
        ProductionOrderOwnership, ProductionOrderOwnership.order_id == ProductionOrder.id,
    ).where(
        ProductionOrder.id == command.order_id,
        ProductionOrderOwnership.organization_id == organization_id,
    ).with_for_update().execution_options(populate_existing=True))
    if order is None:
        raise HTTPException(404, "Production order not found in this organization")
    snapshot, digest = order_snapshot(order)
    if digest != command.expected_order_digest:
        raise HTTPException(409, "Production order changed; review material issue again")
    return order, snapshot


async def preview_material_issue(session, gateway, organization_id: int, user, command: ProductionMaterialIssueCommand):
    actor = await gateway.source_member(session, organization_id, user)
    order, order_snapshot_value = await _owned_order(session, organization_id, command)
    if command.operation_date is None:
        raise HTTPException(422, "Material issue operation date is required")
    if command.location_id is not None:
        location = await session.scalar(select(Location).where(Location.id == command.location_id).with_for_update())
        if location is None or not location.is_active or location.warehouse != command.warehouse:
            raise HTTPException(422, "Material issue location must be active in the selected warehouse")
    return {
        "organization_id": organization_id,
        "principal": actor,
        "command": command.model_dump(mode="json"),
        "order_snapshot": order_snapshot_value,
        "source": _source(command.request_id),
        "stock_effect": {"sku_code": command.sku_code, "warehouse": command.warehouse,
                          "kind": "out", "qty": format(command.quantity, ".2f"),
                          "reason": "production_issue", "location_id": command.location_id,
                          "lot": command.lot, "doc_ref": _source(command.request_id)},
        "posted": False,
        "confirmation_available": True,
        "final_cost_certified": False,
        "order_id": order.id,
    }


async def record_material_issue(session, gateway, organization_id: int, user,
                                command: ProductionMaterialIssueCommand, event_bus=None):
    actor = await gateway.source_write_authority(session, organization_id, user)
    existing = await session.scalar(select(ProductionMaterialIssue).where(
        ProductionMaterialIssue.organization_id == organization_id,
        ProductionMaterialIssue.request_key == str(command.request_id),
    ).with_for_update())
    normalized = command.model_dump(mode="json")
    if existing is not None:
        if existing.actor != actor or existing.snapshot.get("command") != normalized:
            raise HTTPException(409, "Production material issue request conflicts with its saved source")
        movement = await session.get(StockMovement, existing.movement_id)
        if movement is None:
            raise HTTPException(409, "Saved material issue has no physical movement")
        return existing, movement

    _, snapshot = await _owned_order(session, organization_id, command)
    from modules.production.assembly_specifications import AssemblySpecification
    from modules.wms.physical_models import AssemblyExecution

    assembly_spec = await session.scalar(select(AssemblySpecification.id).where(
        AssemblySpecification.organization_id == organization_id,
        AssemblySpecification.order_id == command.order_id).limit(1))
    assembly_execution = await session.scalar(select(AssemblyExecution.id).where(
        AssemblyExecution.organization_id == organization_id,
        AssemblyExecution.order_id == command.order_id).limit(1))
    if assembly_spec is not None or assembly_execution is not None:
        raise HTTPException(409, "Assembly orders require typed physical component operations")
    native = await session.scalar(select(WarehouseControl.warehouse_id).join(
        WarehouseDirectory, WarehouseDirectory.id == WarehouseControl.warehouse_id,
    ).where(WarehouseControl.organization_id == organization_id,
        WarehouseDirectory.name == command.warehouse, WarehouseControl.onec_ref.is_(None)).limit(1))
    if native is not None:
        raise HTTPException(409, "Native assembly components must use typed physical assembly commands")
    await guard_free(session, organization_id, {(command.sku_code, command.warehouse): command.quantity})
    if command.location_id is not None:
        location = await session.scalar(select(Location).where(Location.id == command.location_id).with_for_update())
        if location is None or not location.is_active or location.warehouse != command.warehouse:
            raise HTTPException(422, "Material issue location must be active in the selected warehouse")
    source = _source(command.request_id)
    movement = StockMovement(
        organization_id=organization_id, sku_code=command.sku_code, warehouse=command.warehouse,
        kind="out", qty=command.quantity, reason="production_issue", location_id=command.location_id,
        batch_ref=command.lot, doc_ref=source, note=command.evidence,
    )
    session.add(movement)
    await session.flush()
    binding_snapshot = {"command": normalized, "order_snapshot": snapshot,
                        "movement": {"id": movement.id, "organization_id": movement.organization_id,
                                     "sku_code": movement.sku_code, "warehouse": movement.warehouse,
                                     "kind": movement.kind, "qty": format(movement.qty, ".2f"),
                                     "reason": movement.reason, "location_id": movement.location_id,
                                     "lot": movement.batch_ref, "doc_ref": movement.doc_ref}}
    row = ProductionMaterialIssue(
        organization_id=organization_id, order_id=command.order_id, movement_id=movement.id,
        request_key=str(command.request_id), operation_date=command.operation_date,
        snapshot=binding_snapshot, actor=actor,
    )
    session.add(row)
    await session.flush()
    if event_bus is not None:
        event_bus.emit(session, "production.material.issued", {
            "organization_id": organization_id, "order_id": command.order_id,
            "issue_id": row.id, "movement_id": movement.id, "request_id": str(command.request_id),
            "sku_code": command.sku_code, "qty": format(command.quantity, ".2f"),
            "operation_date": command.operation_date.isoformat(), "entity_ref": source,
        })
    return row, movement


async def read_issue(session, organization_id: int, request_id: UUID):
    row = await session.scalar(select(ProductionMaterialIssue).where(
        ProductionMaterialIssue.organization_id == organization_id,
        ProductionMaterialIssue.request_key == str(request_id),
    ))
    if row is None:
        raise HTTPException(404, "Production material issue not found in this organization")
    movement = await session.get(StockMovement, row.movement_id)
    if movement is None:
        raise HTTPException(409, "Production material issue movement is missing")
    return row, movement
