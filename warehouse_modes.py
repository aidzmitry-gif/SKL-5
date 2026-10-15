"""Strict current and frozen per-warehouse WMS execution modes."""

from sqlalchemy import select

from modules.wms.models import WarehouseDirectory


async def current_modes(session, organization_id, warehouse_names, *, lock=True):
    """Resolve every requested active warehouse; unknown or malformed mode fails closed."""
    names = set(warehouse_names)
    if (type(organization_id) is not int or organization_id <= 0 or not names
            or any(not isinstance(name, str) or not name.strip() or len(name) > 128 for name in names)):
        raise ValueError("Exact organization and warehouse names are required")
    query = select(WarehouseDirectory).where(
        WarehouseDirectory.organization_id == organization_id,
        WarehouseDirectory.is_active.is_(True),
        WarehouseDirectory.name.in_(sorted(names)),
    ).order_by(WarehouseDirectory.id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    rows = (await session.scalars(query)).all()
    modes = {}
    for row in rows:
        if type(row.wms_enabled) is not bool or row.name in modes:
            raise ValueError("Warehouse execution mode is invalid")
        modes[row.name] = row.wms_enabled
    if set(modes) != names:
        raise ValueError("Warehouse is unknown, inactive, or outside the organization")
    return modes


def frozen_modes(execution_modes, warehouse_names):
    """Legacy NULL means WMS-on; new reservations require an exact boolean map."""
    names = set(warehouse_names)
    if execution_modes is None:
        return {name: True for name in names}
    if (not isinstance(execution_modes, dict) or set(execution_modes) != names
            or any(type(value) is not bool for value in execution_modes.values())):
        raise ValueError("Stored warehouse execution mode requires reconciliation")
    return dict(execution_modes)
