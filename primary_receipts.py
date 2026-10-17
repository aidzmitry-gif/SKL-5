"""Primary-to-physical receipt command; caller owns commit/rollback."""
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    event,
    func,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column

from config.access import is_package_allowed
from core.db.base import Base
from core.domain.models import OutboxEvent, Sku
from modules.wms.models import Receipt, ReceiptLine, WarehouseDirectory
from modules.wms.physical_models import WarehouseControl


class PhysicalLine(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    position: int = Field(gt=0, strict=True)
    sku_code: str = Field(min_length=1, max_length=64)
    quantity: Decimal = Field(gt=0, max_digits=14, decimal_places=2)

    @field_validator("quantity", mode="before")
    @classmethod
    def exact_quantity(cls, value):
        if isinstance(value, (float, bool)):
            raise ValueError("Use exact decimal strings for physical quantities")
        return value


class PrimaryReceiptCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_key: UUID
    expected_version: int = Field(gt=0, strict=True)
    warehouse_id: int | None = Field(default=None, gt=0, strict=True,
                                     exclude_if=lambda value: value is None)
    warehouse: str = Field(min_length=1, max_length=128)
    evidence: str = Field(min_length=1, max_length=1000)
    lines: list[PhysicalLine] = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def unique_positions(self):
        if len({line.position for line in self.lines}) != len(self.lines):
            raise ValueError("Each primary line may occur only once in a receipt command")
        return self


class PrimaryReceiptBinding(Base):
    __tablename__ = "primary_receipt_binding"
    __table_args__ = (UniqueConstraint("organization_id", "request_key"), {"schema": "wms"})
    receipt_id: Mapped[int] = mapped_column(ForeignKey("wms.receipt.id"), primary_key=True)
    organization_id: Mapped[int] = mapped_column(Integer, index=True)
    request_key: Mapped[str] = mapped_column(String(36))
    source_receipt_id: Mapped[int] = mapped_column(Integer, index=True)
    source_version: Mapped[int] = mapped_column(Integer)
    command: Mapped[dict] = mapped_column(JSON)
    source_snapshot: Mapped[dict] = mapped_column(JSON)
    line_bindings: Mapped[list] = mapped_column(JSON)
    actor: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def immutable(mapper, connection, target):
    raise ValueError("Primary receipt binding is immutable")


event.listen(PrimaryReceiptBinding, "before_update", immutable)
event.listen(PrimaryReceiptBinding, "before_delete", immutable)


async def _reject_native_legacy_receipt(session, organization_id, warehouse_name):
    query = select(WarehouseControl.warehouse_id).join(
        WarehouseDirectory, WarehouseDirectory.id == WarehouseControl.warehouse_id,
    ).where(WarehouseDirectory.name == warehouse_name, WarehouseControl.onec_ref.is_(None),
        WarehouseControl.mode.in_(["preparing", "erp"]))
    if organization_id is not None:
        query = query.where(WarehouseControl.organization_id == organization_id)
    native = await session.scalar(query.limit(1))
    if native is not None:
        raise HTTPException(409, "Native warehouse receipts must use a typed physical operation")


async def validate_primary_acceptance(session, core, receipt, lines):
    """Recheck bound facts under the caller's organization/receipt locks before stock."""
    await _reject_native_legacy_receipt(session, receipt.organization_id, receipt.warehouse)
    source_event = await session.get(OutboxEvent, receipt.source_event_id) if receipt.source_event_id is not None else None
    if (source_event is not None and source_event.event_type == "production.output.confirmed"
            or (receipt.entity_ref or "").startswith("production_output:")):
        gateway = getattr(core.services, "production_output", None)
        if gateway is None:
            raise HTTPException(503, "Production output source service unavailable")
        try:
            if source_event is None or source_event.event_type != "production.output.confirmed" or source_event.version != 1:
                raise ValueError("Production receipt has no confirmed source event")
            snapshot = await gateway.validate_delivery(session, source_event.id, source_event.payload)
            command = snapshot["command"]
            if (receipt.source != "production" or receipt.organization_id != snapshot["organization_id"]
                    or receipt.entity_ref != source_event.payload["entity_ref"]
                    or receipt.warehouse != command["warehouse"] or len(lines) != 1):
                raise ValueError("Production receipt differs from its confirmed output")
            line = lines[0]
            if (line.sku_code != command["sku_code"] or line.expected_qty != Decimal(command["quantity"])
                    or line.batch_ref != command["lot"] or line.location_id != command["location_id"]):
                raise ValueError("Production receipt line differs from its confirmed output")
            sku = await session.scalar(select(Sku).where(Sku.code == line.sku_code).with_for_update()
                .execution_options(populate_existing=True))
            if sku is None or not sku.is_active or sku.unit != snapshot["sku_snapshot"]["unit"]:
                raise ValueError("Production SKU or unit changed after output confirmation")
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    if receipt.source == "production":
        if not lines:
            raise HTTPException(409, "Production receipt requires product lines")
        for line in lines:
            sku = await session.scalar(select(Sku).where(Sku.code == line.sku_code).with_for_update())
            if sku is None:
                raise HTTPException(409, "Production receipt requires a verified catalog SKU")
            if (line.accepted_qty is None or line.rejected_qty is None
                    or line.accepted_qty < 0 or line.rejected_qty < 0
                    or line.accepted_qty + line.rejected_qty != line.expected_qty):
                raise HTTPException(409, "Record explicit QC quantities for every production line")
    binding = await session.get(PrimaryReceiptBinding, receipt.id)
    if binding is None:
        return
    procurement = getattr(core.services, "procurement_source", None)
    if procurement is None:
        raise HTTPException(503, "Primary receipt service unavailable")
    try:
        if receipt.organization_id != binding.organization_id or receipt.warehouse != binding.command["warehouse"]:
            raise ValueError("Physical receipt owner or warehouse differs from the saved primary binding")
        source = await procurement.warehouse_receipt_source(session, binding.organization_id,
                                                           binding.source_receipt_id, binding.source_version)
        if source != binding.source_snapshot:
            raise ValueError("Primary source changed; reconciliation required before QC acceptance")
        if {line.id for line in lines} != {item["receipt_line_id"] for item in binding.line_bindings}:
            raise ValueError("Physical receipt lines differ from the saved primary binding")
        by_id = {line.id: line for line in lines}
        for item in binding.line_bindings:
            line = by_id[item["receipt_line_id"]]
            fact = source["lines"][item["position"] - 1]
            if (line.sku_code != item["sku_code"] or line.expected_qty != Decimal(item["quantity"])
                    or line.batch_ref != fact["lot"]):
                raise ValueError("Physical receipt SKU, quantity or lot differs from the primary binding")
            if (line.accepted_qty is None or line.rejected_qty is None or line.accepted_qty < 0
                    or line.rejected_qty < 0 or line.accepted_qty + line.rejected_qty != line.expected_qty):
                raise ValueError("Record explicit QC quantities covering every primary receipt line")
            sku = await session.scalar(select(Sku).where(Sku.code == line.sku_code).with_for_update())
            if sku is None or sku.unit != item["unit"]:
                raise ValueError("Warehouse SKU unit changed after primary preparation")
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


async def create_from_primary(session, core, user, organization_id, source_receipt_id, command: PrimaryReceiptCommand):
    if not is_package_allowed("wms", user.roles):
        raise HTTPException(403, "Warehouse access required")
    accounting = getattr(core.services, "accounting", None)
    procurement = getattr(core.services, "procurement_source", None)
    if accounting is None or procurement is None:
        raise HTTPException(503, "Primary receipt services unavailable")
    # The shared organization lock serializes commands with primary edits and peers.
    actor = await accounting.source_member(session, organization_id, user)
    payload = command.model_dump(mode="json")
    previous = await session.scalar(select(PrimaryReceiptBinding).where(
        PrimaryReceiptBinding.organization_id == organization_id,
        PrimaryReceiptBinding.request_key == str(command.request_key),
    ))
    if previous:
        if previous.source_receipt_id != source_receipt_id or previous.command != payload:
            raise ValueError("Receipt request key already identifies different content")
        receipt = await session.get(Receipt, previous.receipt_id)
        if receipt is None or receipt.organization_id != organization_id:
            raise ValueError("Stored physical receipt ownership is inconsistent")
        return previous
    if command.warehouse_id is None:
        raise ValueError("Select the physical warehouse from the organization catalogue")
    warehouse = await session.scalar(select(WarehouseDirectory).where(
        WarehouseDirectory.id == command.warehouse_id,
        WarehouseDirectory.organization_id == organization_id,
        WarehouseDirectory.is_active.is_(True),
        WarehouseDirectory.name == command.warehouse,
    ).with_for_update(read=True))
    if warehouse is None:
        raise ValueError("Physical warehouse catalogue selection changed; select it again")
    await _reject_native_legacy_receipt(session, organization_id, warehouse.name)
    source = await procurement.warehouse_receipt_source(session, organization_id, source_receipt_id, command.expected_version)
    from modules.wms.physical_stock import PhysicalStock

    history = await PhysicalStock(core.services.stock, services=core.services)._native_receipt_history(
        session, organization_id, source, legacy_caller=True,
    )
    existing = (await session.scalars(select(PrimaryReceiptBinding).where(
        PrimaryReceiptBinding.organization_id == organization_id,
        PrimaryReceiptBinding.source_receipt_id == source_receipt_id,
    ))).all()
    allocated = {}
    mapping = {}
    for binding in existing:
        if binding.source_version != command.expected_version or binding.source_snapshot != source:
            raise ValueError("Primary receipt changed after physical preparation; reconciliation required")
        for line in binding.line_bindings:
            position = line["position"]
            allocated[position] = allocated.get(position, Decimal(0)) + Decimal(line["quantity"])
            mapping[position] = line["sku_code"]
    if history["has_native_for_source"]:
        # The shared view already includes legacy bindings: replace, never add twice.
        allocated = history["allocated"]
    prepared = []
    for selected in command.lines:
        if selected.position > len(source["lines"]):
            raise ValueError("Primary receipt line does not exist")
        fact = source["lines"][selected.position - 1]
        if allocated.get(selected.position, Decimal(0)) + selected.quantity > Decimal(fact["quantity"]):
            raise ValueError("Physical receipts exceed the primary line quantity")
        if selected.position in mapping and mapping[selected.position] != selected.sku_code:
            raise ValueError("Primary line already maps to a different warehouse SKU")
        sku = await session.scalar(select(Sku).where(Sku.code == selected.sku_code).with_for_update())
        if sku is None or sku.unit != fact["unit"]:
            raise ValueError("Select an existing warehouse SKU with the exact primary unit")
        if history["has_native_for_source"]:
            pinned = history["owners"][(source_receipt_id, command.expected_version)]["lines"][selected.position - 1]
            if (sku.id != pinned["sku_id"] or sku.code != pinned["sku"]
                    or sku.unit != pinned["unit"] or fact["lot"] != pinned["lot"]):
                raise ValueError("Mixed receipt requires the exact native source SKU, unit and lot")
        if len(fact["lot"]) > 64:
            raise ValueError("Primary lot exceeds physical receipt storage; explicit mapping required")
        prepared.append((selected, fact, sku))
    receipt = Receipt(organization_id=organization_id, source="procurement", warehouse=command.warehouse,
                      entity_ref=f"procurement:receipt:{source_receipt_id}:{command.expected_version}", status="pending_qc")
    session.add(receipt)
    await session.flush()
    receipt.number = f"ПРМ-{receipt.id:06d}"
    line_bindings = []
    for selected, fact, sku in prepared:
        line = ReceiptLine(receipt_id=receipt.id, sku_code=sku.code, sku_title=sku.title,
                           expected_qty=selected.quantity, batch_ref=fact["lot"])
        session.add(line)
        await session.flush()
        line_bindings.append({"receipt_line_id": line.id, "source_line": fact["source_line"],
                              "position": selected.position, "sku_code": sku.code,
                              "quantity": str(selected.quantity), "unit": fact["unit"]})
    binding = PrimaryReceiptBinding(receipt_id=receipt.id, organization_id=organization_id,
                                    request_key=str(command.request_key), source_receipt_id=source_receipt_id,
                                    source_version=command.expected_version, command=payload,
                                    source_snapshot=source, line_bindings=line_bindings, actor=actor)
    session.add(binding)
    await session.flush()
    return binding
