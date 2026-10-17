from __future__ import annotations

import pytest

from modules.wms.gs1 import parse_gs1


def test_ai11_month_precision_uses_first_day_of_month() -> None:
    parsed = parse_gs1("010950600013435211260700")

    assert parsed["manufacture"] == {"value": "2026-07-01", "precision": "month"}


def test_ai15_month_precision_uses_last_day_of_leap_month() -> None:
    parsed = parse_gs1("010950600013435215280200")

    assert parsed["best_before"] == {"value": "2028-02-29", "precision": "month"}


def test_unseparated_variable_tail_is_not_guessed_as_next_ai() -> None:
    parsed = parse_gs1("010950600013435210LOT21SN1")

    assert parsed["lot_no"] == "LOT21SN1"
    assert parsed["serial"] is None


def test_gs1_rejects_invalid_calendar_day() -> None:
    with pytest.raises(ValueError):
        parse_gs1("010950600013435215260230")
