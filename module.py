"""Модуль WMS (Склад) — реализация ModuleContract."""
from __future__ import annotations

from core.runtime.contract import ModuleContract, Widget
from core.runtime.core import Core
from modules.wms import routes


class WmsModule(ModuleContract):
    name = "wms"
    version = "0.1.0"
    api_prefix = "/wms"

    def register(self, core: Core) -> None:
        core.include_router(routes.router, prefix=self.api_prefix)
        core.register_widget(Widget("wms", "Склад", source="wms.movements"))


def get_module() -> ModuleContract:
    return WmsModule()
