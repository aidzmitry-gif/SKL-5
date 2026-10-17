"""RBAC модуля WMS: право чтения склада + роли-носители.

Право навешивается на read-роуты через ``require_permission`` (канон seam-doc:
«для WMS и любого модуля без RBAC — навешивать права так»). Роли — слаги
``config/access.py``, у которых в матрице есть модуль ``wms`` (warehouse/logistics/
production/hr); ``director``/``commercial`` — суперроли (полный доступ минуя список).
"""
from __future__ import annotations

from core.runtime.contract import Permission, Role

PERMISSIONS = [
    Permission("wms.packing.read", "Чтение подтверждённых мест и веса для накладной"),
    Permission("wms.read", "Просмотр склада (остатки, движения, операции)"),
    Permission("wms.count", "Проведение инвентаризации (пересчёт, расхождения)"),
    Permission("wms.operate", "Операции склада и ТСД"),
    Permission("wms.mobile", "Ограниченные мобильные операции склада"),
    Permission("wms.configure", "Правила, партии и адреса склада"),
    Permission("wms.approve", "Допуск качества, обслуживание и утверждение расхождений"),
]

# Роли с доступом к модулю склада — зеркало ACCESS_MATRIX по слагу «wms».
# Пересчёт (запись инвентаризации) — у кладовщика; логистика/производство/HR — только чтение.
ROLES = [
    Role("warehouse", ("wms.packing.read", "wms.read", "wms.count", "wms.operate")),
    Role("warehouse_manager", ("wms.packing.read", "wms.read", "wms.count", "wms.operate", "wms.configure", "wms.approve")),
    Role("wms_mobile", ("wms.mobile",)),
    Role("logistics", ("wms.read", "wms.packing.read")),
    Role("assistant", ("wms.packing.read",)),
    Role("finance", ("wms.packing.read",)),
    Role("production", ("wms.read",)),
    Role("hr", ("wms.read",)),
]
