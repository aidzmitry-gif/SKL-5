"""Модуль WMS (Склад) — реализация ModuleContract."""
from __future__ import annotations

from core.runtime.contract import ModuleContract, Widget
from core.runtime.core import Core
from modules.wms import routes
from modules.wms.events import on_goods_received, on_stock_reserved


class WmsModule(ModuleContract):
    name = "wms"
    version = "0.1.0"
    api_prefix = "/wms"

    def register(self, core: Core) -> None:
        core.include_router(routes.router, prefix=self.api_prefix)
        # межмодульные связи склада (§2.5): резерв под заказ → расход;
        # приёмка из закупок/производства → приход
        core.subscribe("sales.stock.reserved", on_stock_reserved)
        core.subscribe("procurement.received", on_goods_received)
        core.subscribe("production.completed", on_goods_received)
        core.register_widget(Widget("wms", "Склад", source="wms.movements"))


def get_module() -> ModuleContract:
    return WmsModule()
