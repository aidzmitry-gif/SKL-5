"""RBAC модуля WMS: право чтения склада + роли-носители.

Право навешивается на read-роуты через ``require_permission`` (канон seam-doc:
«для WMS и любого модуля без RBAC — навешивать права так»). Роли — слаги
``config/access.py``, у которых в матрице есть модуль ``wms`` (warehouse/logistics/
production/hr); ``director``/``commercial`` — суперроли (полный доступ минуя список).
"""
from __future__ import annotations

from core.runtime.contract import Permission, Role

PERMISSIONS = [
    Permission("wms.read", "Просмотр склада (остатки, движения, операции)"),
    Permission("wms.count", "Проведение инвентаризации (пересчёт, расхождения)"),
]

# Роли с доступом к модулю склада — зеркало ACCESS_MATRIX по слагу «wms».
# Пересчёт (запись инвентаризации) — у кладовщика; логистика/производство/HR — только чтение.
ROLES = [
    Role("warehouse", ("wms.read", "wms.count")),
    Role("logistics", ("wms.read",)),
    Role("production", ("wms.read",)),
    Role("hr", ("wms.read",)),
]
