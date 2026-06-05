"""Стадии воронки складских операций (порядок списка = порядок колонок)."""

STAGES: list[dict] = [
    {"id": "inbound", "title": "Ожидается поступление", "color": "#2563EB"},
    {"id": "receiving", "title": "Приёмка / Пересчёт", "color": "#4F46E5"},
    {"id": "qc", "title": "Контроль качества", "color": "#D97706"},
    {"id": "putaway", "title": "Размещение", "color": "#0891B2"},
    {"id": "picking", "title": "Подготовка", "color": "#7C3AED"},
    {"id": "ready", "title": "Готово к отгрузке", "color": "#0D9488"},
    {"id": "shipped", "title": "Отгружено: Успешно", "color": "#16A34A"},
]
