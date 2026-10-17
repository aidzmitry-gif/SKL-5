"""Модуль WMS (Склад) — реализация ModuleContract."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from core.runtime.contract import ModuleContract, Widget
from core.runtime.core import Core
from modules.wms import routes
from modules.wms.events import on_goods_received, on_stock_released, on_stock_reserved
from modules.wms.invoice_remainder import router as remainder_router
from modules.wms.invoice_shipments import router as physical_router
from modules.wms.mobile_routes import router as mobile_router
from modules.wms.permissions import PERMISSIONS, ROLES
from modules.wms.physical_exchange import tick_exchange
from modules.wms.physical_gateway import PhysicalStockGateway
from modules.wms.physical_legacy_guard import guard_legacy_write
from modules.wms.physical_reconciliation import on_stock_observed
from modules.wms.physical_routes import directory_router
from modules.wms.physical_routes import router as physical_stock_router
from modules.wms.physical_stock import tick_physical_tasks
from modules.wms.primary_receipt_routes import router as primary_receipt_router
from modules.wms.production_material_issues import (
    ProductionMaterialIssue,  # noqa: F401 — register durable source model
)
from modules.wms.reservation_gateway import WmsReservationService
from modules.wms.warehouse_catalog import router as warehouse_catalog_router


class WmsModule(ModuleContract):
    name = "wms"
    version = "0.1.0"
    api_prefix = "/wms"

    def register(self, core: Core) -> None:
        if core.services.stock is not None:
            core.services.stock = PhysicalStockGateway(core.services.stock, core.event_bus)
        core.services.wms_reservations = WmsReservationService(core.services.stock)
        legacy = APIRouter(dependencies=[Depends(guard_legacy_write)])
        legacy.include_router(routes.router)
        legacy.include_router(primary_receipt_router)
        core.include_router(legacy, prefix=self.api_prefix)
        core.include_router(physical_router, prefix=self.api_prefix)
        core.include_router(remainder_router, prefix=self.api_prefix)
        core.include_router(warehouse_catalog_router, prefix=self.api_prefix)
        core.include_router(physical_stock_router, prefix=self.api_prefix)
        core.include_router(mobile_router, prefix=self.api_prefix)
        core.include_router(directory_router, prefix=self.api_prefix)
        core.on_tick(tick_exchange)
        core.on_tick(tick_physical_tasks)
        # Резерв создаёт подбор; физическое движение требует отдельного подтверждения.
        core.subscribe("sales.stock.reserved", on_stock_reserved)
        core.subscribe("sales.stock.released", on_stock_released)
        core.subscribe("procurement.received", on_goods_received)
        core.subscribe("production.completed", on_goods_received)
        core.subscribe("production.output.confirmed", on_goods_received)
        core.subscribe("integration.1c.stock.observed", on_stock_observed)
        core.declare_permissions(PERMISSIONS)
        for role in ROLES:
            core.declare_role(role)
        core.register_widget(Widget("wms", "Склад", source="wms.movements"))


def get_module() -> ModuleContract:
    return WmsModule()
