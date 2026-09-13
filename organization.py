"""Company access via the public accounting facade; the caller owns its transaction."""
import hashlib
import json
from decimal import Decimal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select


class ReceiptOrganizationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    organization_id: int = Field(gt=0, strict=True)
    evidence: str = Field(min_length=1, max_length=1000)


def qc_revision(receipt, lines):
    def quantity(value):
        return None if value is None else str(Decimal(str(value)).quantize(Decimal("0.01")))
    snapshot = {"id": receipt.id, "status": receipt.status, "lines": [
        [row.id, quantity(row.expected_qty), quantity(row.accepted_qty), quantity(row.rejected_qty), row.reject_reason, row.location_id]
        for row in sorted(lines, key=lambda row: row.id)
    ]}
    return hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


async def authorize_row(session, core, user, row):
    organization_id = row.organization_id
    if organization_id is None:
        raise HTTPException(409, "Подтвердите юрлицо складского документа перед изменением")
    gateway = getattr(core.services, "accounting", None)
    if gateway is None:
        raise HTTPException(503, "Сервис доступа к юрлицам недоступен")
    await gateway.source_member(session, organization_id, user)
    model = type(row)
    current = await session.scalar(select(model).where(model.id == row.id).with_for_update().execution_options(populate_existing=True))
    if current is None or current.organization_id != organization_id:
        raise HTTPException(409, "Владелец документа изменился. Обновите документ")
    return current


async def visible_organization_ids(session, core, user, organization_id=None):
    gateway = getattr(core.services, "accounting", None)
    if gateway is None:
        raise HTTPException(503, "Сервис доступа к юрлицам недоступен")
    if organization_id is not None:
        await gateway.source_member(session, organization_id, user)
        return [organization_id]
    return [row["id"] for row in await gateway.source_organizations(session, user)]
