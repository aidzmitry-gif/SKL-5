"""Реакции модуля WMS на события других модулей (через шину, §2.5).

Склад слушает товародвижение: резерв под заказ из sales становится расходным
движением; приёмка из закупок/производства — приходным.
"""
from __future__ import annotations

from decimal import Decimal

from modules.wms.models import StockMovement


async def on_stock_reserved(payload: dict, ctx) -> None:
    """Резерв под заказ (sales) → расходное движение по складу."""
    if ctx is None:
        return
    for item in payload.get("items", []):
        ctx.session.add(
            StockMovement(
                sku_code=item.get("sku_code", ""),
                warehouse=item.get("warehouse", "Главный"),
                kind="out",
                qty=Decimal(str(item.get("qty", 0))),
            )
        )


async def on_goods_received(payload: dict, ctx) -> None:
    """Приёмка из закупок/производства → приходное движение по складу."""
    if ctx is None:
        return
    ctx.session.add(
        StockMovement(
            sku_code=payload.get("sku_code", payload.get("item", "")),
            warehouse=payload.get("warehouse", "Главный"),
            kind="in",
            qty=Decimal(str(payload.get("qty", 0))),
        )
    )
