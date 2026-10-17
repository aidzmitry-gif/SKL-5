"""Bounded GS1 element-string parser (01, 10, 11, 15, 17, 21).

Source: https://ref.gs1.org/guidelines/2d-in-retail/1.0.0/
Unknown AIs fail visibly; variable-length fields require FNC1 separators in
scanner output. AI 11 manufacture is never substituted for AI 17 expiry.
"""
import calendar
import re
from datetime import date

FIXED = {"01": 14, "11": 6, "15": 6, "17": 6}
VARIABLE = {"10": 20, "21": 20}


def gs1_date(value, *, expiry=False):
    if len(value) != 6 or not value.isdigit():
        raise ValueError("Некорректная дата GS1")
    year, month, day = 2000 + int(value[:2]), int(value[2:4]), int(value[4:])
    if day == 0:
        # Month precision is explicit. Do not invent a day for manufacture-age
        # decisions; expiry/best-before DD=00 denotes the end of the month.
        end = calendar.monthrange(year, month)[1]
        return {"value": date(year, month, end if expiry else 1).isoformat(), "precision": "month"}
    return {"value": date(year, month, day).isoformat(), "precision": "day"}


def parse_gs1(raw):
    if not isinstance(raw, str) or not raw or len(raw) > 256:
        raise ValueError("Пустой или слишком длинный код GS1")
    raw = re.sub(r"^\](?:d2|C1|Q3)", "", raw)
    values = {}
    if raw.startswith("("):
        # Human-readable notation is accepted for controlled manual entry.
        matches = list(re.finditer(r"\((\d{2})\)([^()]*)", raw))
        if "".join(m.group(0) for m in matches) != raw:
            raise ValueError("Повреждена запись идентификаторов GS1")
        parts = [(match[1], match[2]) for match in matches]
    else:
        parts = []
        while raw:
            ai, raw = raw[:2], raw[2:]
            if ai in FIXED:
                width = FIXED[ai]
                value, raw = raw[:width], raw[width:]
            elif ai in VARIABLE:
                value, separator, raw = raw.partition("\x1d")
                if not separator:
                    raw = ""
            else:
                raise ValueError(f"Неподдерживаемый идентификатор GS1 {ai}")
            parts.append((ai, value))
            raw = raw.lstrip("\x1d")
    for ai, value in parts:
        if ai in values or ai not in (FIXED.keys() | VARIABLE.keys()):
            raise ValueError("Неизвестный или повторяющийся идентификатор GS1")
        if ai in FIXED and (len(value) != FIXED[ai] or not value.isdigit()):
            raise ValueError("Повреждено поле фиксированной длины GS1")
        if ai in VARIABLE and (not value or len(value) > VARIABLE[ai] or any(ord(c) < 32 for c in value)):
            raise ValueError("Повреждено поле переменной длины GS1")
        values[ai] = value
    if "01" not in values:
        raise ValueError("GS1 не содержит GTIN")
    gtin = values["01"]
    expected = (10 - sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(gtin[:-1]))) % 10) % 10
    if int(gtin[-1]) != expected:
        raise ValueError("Не совпадает контрольная цифра GTIN")
    result = {"gtin": gtin, "lot_no": values.get("10"), "serial": values.get("21")}
    for ai, key in (("11", "manufacture"), ("15", "best_before"), ("17", "expiry")):
        result[key] = gs1_date(values[ai], expiry=ai != "11") if ai in values else None
    return result
