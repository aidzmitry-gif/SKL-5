"""Реакции модуля WMS на события других модулей (через шину, §2.5).

Склад слушает товародвижение: резерв под заказ из sales становится расходным
движением; приёмка из закупок/производства — приходным.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from core.domain.models import Sku
from modules.wms.models import Receipt, ReceiptLine, StockMovement, Task


async def on_stock_reserved(payload: dict, ctx) -> None:
    """Резерв под заказ (sales) → расходное движение reason=reserve + pick-задача под позицию.

    Резерв оставляем как есть (теневой расход), но дополнительно рождаем заявку на подбор
    (pick-задача), чтобы замкнуть отгрузку в волну подбора. ``doc_ref`` берём из payload
    (документ/сделка) для связи отгрузки с задачами.
    """
    if ctx is None:
        return
    doc_ref = payload.get("doc_ref") or payload.get("entity_ref") or ""
    for item in payload.get("items", []):
        sku_code = item.get("sku_code", "")
        warehouse = item.get("warehouse", "Главный")
        qty = Decimal(str(item.get("qty", 0)))
        ctx.session.add(
            StockMovement(
                sku_code=sku_code, warehouse=warehouse, kind="out", qty=qty, reason="reserve",
                doc_ref=doc_ref,
            )
        )
        ctx.session.add(
            Task(
                kind="pick", sku_code=sku_code, qty=qty, warehouse=warehouse,
                doc_ref=doc_ref, status="open",
            )
        )


async def on_stock_released(payload: dict, ctx) -> None:
    """Снятие резерва (sales) → приходное движение (возврат доступности, reason=release)."""
    if ctx is None:
        return
    for item in payload.get("items", []):
        ctx.session.add(
            StockMovement(
                sku_code=item.get("sku_code", ""),
                warehouse=item.get("warehouse", "Главный"),
                kind="in",
                qty=Decimal(str(item.get("qty", 0))),
                reason="release",
            )
        )


async def on_goods_received(payload: dict, ctx) -> None:
    """Приёмка из закупок/производства → документ приёмки в ``pending_qc`` (БЕЗ движения).

    Движение прихода пишется только после QC-приёмки (``/wms/receipts/{id}/accept``) по
    фактически принятому кол-ву — брак на свободный остаток не попадает. ``entity_ref``
    (напр. ``purchase:<id>``) сохраняется для трассировки; sku берётся из ``item``/``sku_code``.
    """
    if ctx is None:
        return
    sku_code = payload.get("sku_code") or payload.get("item") or ""
    entity_ref = payload.get("entity_ref", "")
    # ponytail: источник по префиксу entity_ref (purchase→procurement, прочее→production);
    # точный источник лучше передавать в payload, когда у событий появится поле source.
    source = "procurement" if entity_ref.startswith("purchase") else "production"
    title = ""
    sku = (
        await ctx.session.execute(select(Sku).where(Sku.code == sku_code))
    ).scalars().first()
    if sku:
        title = sku.title
    receipt = Receipt(
        source=source,
        entity_ref=entity_ref,
        warehouse=payload.get("warehouse", "Главный"),
        status="pending_qc",
    )
    ctx.session.add(receipt)
    await ctx.session.flush()
    receipt.number = f"ПРМ-2026-{receipt.id:04d}"
    ctx.session.add(
        ReceiptLine(
            receipt_id=receipt.id,
            sku_code=sku_code,
            sku_title=title,
            expected_qty=Decimal(str(payload.get("qty", 0))),
        )
    )
