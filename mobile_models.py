"""Durable identity and replay records for the constrained WMS mobile API."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base


class MobileDevice(Base):
    __tablename__ = "mobile_device"
    __table_args__ = (
        CheckConstraint("state IN ('pending', 'active', 'revoked')", name="mobile_device_state"),
        CheckConstraint("revision > 0", name="mobile_device_revision"),
        UniqueConstraint("jwk_thumbprint", name="mobile_device_jwk_thumbprint"),
        UniqueConstraint("enrollment_jti", name="mobile_device_enrollment_jti"),
        {"schema": "wms"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_sub: Mapped[str] = mapped_column(String(200), index=True)
    public_jwk: Mapped[dict] = mapped_column(JSON)
    jwk_thumbprint: Mapped[str] = mapped_column(String(64))
    enrollment_jti: Mapped[str] = mapped_column(String(128))
    enrollment_digest: Mapped[str] = mapped_column(String(64))
    requested_organization_id: Mapped[int] = mapped_column(
        ForeignKey("accounting.organization.id", ondelete="RESTRICT"), index=True
    )
    requested_warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("wms.physical_warehouse.warehouse_id", ondelete="RESTRICT"), index=True
    )
    requested_actions: Mapped[list[str]] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(16), default="pending")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    enrolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    approved_by: Mapped[str | None] = mapped_column(String(200))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[str | None] = mapped_column(String(200))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MobileGrant(Base):
    __tablename__ = "mobile_grant"
    __table_args__ = (
        CheckConstraint("state IN ('active', 'revoked')", name="mobile_grant_state"),
        CheckConstraint("revision > 0", name="mobile_grant_revision"),
        UniqueConstraint(
            "device_id", "organization_id", "warehouse_id", name="mobile_grant_scope"
        ),
        {"schema": "wms"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    device_id: Mapped[str] = mapped_column(
        ForeignKey("wms.mobile_device.id", ondelete="RESTRICT"), index=True
    )
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("accounting.organization.id", ondelete="RESTRICT"), index=True
    )
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("wms.physical_warehouse.warehouse_id", ondelete="RESTRICT"), index=True
    )
    actions: Mapped[list[str]] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(16), default="active")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    approved_by: Mapped[str] = mapped_column(String(200))
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    changed_by: Mapped[str] = mapped_column(String(200))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MobileProofUse(Base):
    __tablename__ = "mobile_proof_use"
    __table_args__ = (
        CheckConstraint("length(jti) > 0", name="mobile_proof_jti_nonempty"),
        CheckConstraint("length(request_digest) = 64", name="mobile_proof_digest_length"),
        {"schema": "wms"},
    )

    device_id: Mapped[str] = mapped_column(
        ForeignKey("wms.mobile_device.id", ondelete="RESTRICT"), primary_key=True
    )
    jti: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_digest: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    operation_id: Mapped[str | None] = mapped_column(String(36))
    grant_id: Mapped[str | None] = mapped_column(
        ForeignKey("wms.mobile_grant.id", ondelete="RESTRICT")
    )
    grant_revision: Mapped[int | None] = mapped_column(Integer)
