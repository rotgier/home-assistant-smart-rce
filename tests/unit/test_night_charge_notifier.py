"""Telegram text of the workday night charge (user's decision 19.11.2026)."""

from datetime import date, time
from unittest.mock import MagicMock

from custom_components.smart_rce.application.night_charge_report import (
    NightChargeReport,
)
from custom_components.smart_rce.domain.battery_schedule import MorningChargeFloor
from custom_components.smart_rce.domain.dear_day_choice import DearDayVariant
from custom_components.smart_rce.infrastructure.night_charge_notifier import render


def _report(**changes) -> NightChargeReport:
    base = {
        "day": date(2026, 11, 19),
        "latched": True,
        "target": 22,
        "reserve": 22,
        "window": (time(4), time(7)),
        "export_threshold": 750.0,
        "first_hour": 7,
        "t2_hour": 16,
        "cheap": (9, 10, 11),
        "floor": MorningChargeFloor(
            day=date(2026, 11, 19),
            surplus_kwh=13.93,
            fill=10,
            dip=11,
            target=None,
            dear_day=False,
        ),
        "dear_choice": None,
        "morning_plan": None,
        "morning_why_none": "najdroższa 08:00 (869) nie przebija 12:00 (932)",
    }
    return NightChargeReport(**{**base, **changes})


def test_a_normal_day_leaves_room_for_the_sun_19_11_2026():
    text = render(_report())

    assert "Dziś 19.11 (zatrzaśnięte o 04:00): noc ładuje do 22%" in text
    assert "nadwyżka PV 13,9 kWh w tanich godzinach (09–11)" in text
    assert "resztę miejsca zostawia słońcu" in text
    assert "Poranna sprzedaż: brak — najdroższa 08:00" in text


def test_a_weak_sun_lifts_the_night_above_the_reserve():
    floor = MorningChargeFloor(
        day=date(2026, 11, 19), surplus_kwh=2.1, fill=78, dip=12, target=90
    )
    text = render(_report(target=90, reserve=33, floor=floor, latched=False))

    assert "Jutro 19.11 (zapowiedź): noc ładuje do 90%" in text
    assert "dopełni baterię dopiero od 78%" in text


def test_a_dear_day_shows_the_full_battery_choice():
    floor = MorningChargeFloor(
        day=date(2026, 11, 19),
        surplus_kwh=0,
        fill=100,
        dip=18,
        target=100,
        dear_day=True,
    )
    choice = MagicMock(chosen=DearDayVariant.FULL)
    choice.full.value_zl = 19.9
    choice.normal.value_zl = 19.49
    text = render(_report(target=100, floor=floor, dear_choice=choice, cheap=()))

    assert "Drogi dzień: każda godzina 07–16:00 płaci co najmniej 750 zł/MWh." in text
    assert "Pełna bateria +19,90 zł wobec zwykłego dnia +19,49 zł" in text
    assert "Poranna sprzedaż: brak — pełna bateria pokrywa T1." in text
    assert "najdroższa" not in text


def test_a_planned_morning_sale_is_shown():
    plan = MagicMock(hour=7, target=33, gross_price=1468.0)
    text = render(_report(morning_plan=plan))

    assert "Poranna sprzedaż: 07:00 do 33% (1,47 zł/kWh)." in text


def test_no_line_begins_with_start():
    text = render(_report())

    assert not any(line.lower().startswith("start") for line in text.splitlines())
