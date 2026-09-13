"""Organization-owned primary receipt preparation; commit before HTTP success."""
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.exc import DBAPIError

from core.runtime.deps import get_core, get_session
from core.services.auth import require_permission
from modules.wms.primary_receipts import PrimaryReceiptCommand, create_from_primary

router = APIRouter(tags=["Приёмка по первичным накладным"])


async def transaction(session=Depends(get_session)):
    try:
        yield session
        await session.commit()
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(409, str(exc)) from exc
    except DBAPIError as exc:
        await session.rollback()
        raise HTTPException(409, "Receipt transaction was not saved; refresh the source and retry the same command") from exc
    except Exception:
        await session.rollback()
        raise


@router.get("/organizations/{organization_id}/primary-receipts/{source_receipt_id}/source")
async def primary_source(organization_id: int, source_receipt_id: int, response: Response,
                         expected_version: int = Query(ge=1),
                         session=Depends(transaction, scope="function"), core=Depends(get_core),
                         user=Depends(require_permission("wms.count"))):
    accounting = getattr(core.services, "accounting", None)
    procurement = getattr(core.services, "procurement_source", None)
    if accounting is None or procurement is None:
        raise HTTPException(503, "Primary receipt services unavailable")
    await accounting.source_member(session, organization_id, user)
    source = await procurement.warehouse_receipt_source(session, organization_id, source_receipt_id, expected_version)
    response.headers["Cache-Control"] = "private, no-store"
    return {"organization_id": organization_id, "source_receipt_id": source_receipt_id,
            "source_version": source["version"], "source": source["source"],
            "source_warehouse": source["document"]["warehouse"], "lines": source["lines"]}


@router.post("/organizations/{organization_id}/primary-receipts/{source_receipt_id}", status_code=201)
async def prepare_primary_receipt(organization_id: int, source_receipt_id: int, command: PrimaryReceiptCommand,
                                  session=Depends(transaction, scope="function"), core=Depends(get_core),
                                  user=Depends(require_permission("wms.count"))):
    binding = await create_from_primary(session, core, user, organization_id, source_receipt_id, command)
    return {"organization_id": binding.organization_id, "receipt_id": binding.receipt_id,
            "source_receipt_id": binding.source_receipt_id, "source_version": binding.source_version,
            "request_key": binding.request_key, "lines": binding.line_bindings}
