"""Модуль WMS (Склад) — реализация ModuleContract."""
from __future__ import annotations

from core.runtime.contract import ModuleContract, Widget
from core.runtime.core import Core
from modules.wms import routes
from modules.wms.events import on_goods_received, on_stock_released, on_stock_reserved
from modules.wms.invoice_remainder import router as remainder_router
from modules.wms.invoice_shipments import router as physical_router
from modules.wms.permissions import PERMISSIONS, ROLES
from modules.wms.primary_receipt_routes import router as primary_receipt_router
from modules.wms.production_material_issues import (
    ProductionMaterialIssue,  # noqa: F401 — register durable source model
)
from modules.wms.reservation_gateway import WmsReservationService


class WmsModule(ModuleContract):
    name = "wms"
    version = "0.1.0"
    api_prefix = "/wms"

    def register(self, core: Core) -> None:
        core.services.wms_reservations = WmsReservationService()
        core.include_router(routes.router, prefix=self.api_prefix)
        core.include_router(physical_router, prefix=self.api_prefix)
        core.include_router(remainder_router, prefix=self.api_prefix)
        core.include_router(primary_receipt_router, prefix=self.api_prefix)
        # Резерв создаёт подбор; физическое движение требует отдельного подтверждения.
        core.subscribe("sales.stock.reserved", on_stock_reserved)
        core.subscribe("sales.stock.released", on_stock_released)
        core.subscribe("procurement.received", on_goods_received)
        core.subscribe("production.completed", on_goods_received)
        core.subscribe("production.output.confirmed", on_goods_received)
        core.declare_permissions(PERMISSIONS)
        for role in ROLES:
            core.declare_role(role)
        core.register_widget(Widget("wms", "Склад", source="wms.movements"))


def get_module() -> ModuleContract:
    return WmsModule()
