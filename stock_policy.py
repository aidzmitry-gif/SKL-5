"""Shared stock policy. Call inside the existing organization transaction lock."""
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select

from modules.wms.models import WarehouseDirectory


async def policies(session, organization_id, warehouses):
    names = set(warehouses)
    rows = (await session.scalars(select(WarehouseDirectory).where(
        WarehouseDirectory.organization_id == organization_id,
        WarehouseDirectory.name.in_(names), WarehouseDirectory.is_active.is_(True),
    ).order_by(WarehouseDirectory.name).with_for_update()
        .execution_options(populate_existing=True))).all()
    result = {}
    for row in rows:
        if type(row.prohibit_negative_stock) is not bool:
            raise HTTPException(409, "Настройка контроля остатков склада не определена")
        result[row.name] = row.prohibit_negative_stock
    # Legacy/missing catalogue entries cannot silently grant an exception.
    return {name: result.get(name, True) for name in names}


def shortage(sku, warehouse, requested, physical, reserved, *, own_reserved=Decimal("0")):
    requested, reserved = Decimal(requested), Decimal(reserved)
    other = max(Decimal("0"), reserved - Decimal(own_reserved))
    available = None if physical is None else Decimal(physical) - other
    if available is not None and available >= requested:
        return None
    def fmt(value):
        return None if value is None else format(value, ".2f")
    if available is None:
        reason = "unknown_stock"
        message = "Остаток не определён. Подтвердите приход или начальные остатки и проверьте складской журнал."
    elif other > 0:
        reason = "reserved_by_other_documents"
        message = "Товар зарезервирован другими документами. Освободите резерв или оформите поступление."
    else:
        reason = "insufficient_stock"
        message = "Недостаточно товара. Проверьте склад и оформите фактическое поступление или корректировку остатков."
    return {"code": "stock_shortage", "reason": reason, "message": message,
            "sku_code": sku, "warehouse": warehouse, "requested": fmt(requested),
            "physical": fmt(None if physical is None else Decimal(physical)),
            "reserved": fmt(reserved), "reserved_other": fmt(other),
            "available": fmt(available), "shortage": fmt(None if available is None else requested - available)}


def check(sku, warehouse, requested, physical, reserved, prohibit, *, own_reserved=Decimal("0")):
    issue = shortage(sku, warehouse, requested, physical, reserved, own_reserved=own_reserved)
    if issue and (prohibit or physical is None or issue["reason"] == "reserved_by_other_documents"):
        raise HTTPException(409, issue)
