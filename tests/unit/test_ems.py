import glob

from custom_components.smart_rce.domain.charge_slots import (
    MAX_CONSECUTIVE_HOURS,
    shift_earlier_if_cheap,
)
from custom_components.smart_rce.domain.rce import RceDayPrices
import orjson
import pytest

from tests.unit._csv_fixture import create_csv


@pytest.mark.parametrize(("year", "month"), [(2024, 8), (2024, 9), (2026, 4)])
def test_find_charge_hours(year, month) -> None:
    produced_csv = []
    for path in sorted(glob.glob(f"tests/fixtures/raw/rce_{year}_{month:02}*")):
        with open(path, encoding="utf-8") as file:
            rce_prices_raw = orjson.loads(file.read())
            rce_prices: RceDayPrices = RceDayPrices.create_from_json(rce_prices_raw)
            produced_csv.extend(create_csv(rce_prices))
    produced = "\n".join(produced_csv) + "\n"

    path = f"tests/fixtures/charge_hours_{year}_{month:02}.csv"
    with open(path, encoding="utf-8") as file:
        assert produced == file.read()


def _prices(**overrides: float) -> list[float]:
    """Zwraca 24h płaski baseline 500 PLN/MWh z podmienionymi godzinami."""
    base = [500.0] * 24
    for hour, price in overrides.items():
        base[int(hour.lstrip("h"))] = price
    return base


def test_shift_earlier_extends_when_added_hour_is_cheap() -> None:
    # Anchor (h14) = 100; h10=130 daje diff 30 < 40 → extend.
    # h9=160 daje diff 60 > 40 → stop.
    prices = _prices(h9=160, h10=130, h11=50, h12=50, h13=50, h14=100)
    new_consec, new_start = shift_earlier_if_cheap(
        prices, start=11, consecutive_hours=4
    )
    assert (new_consec, new_start) == (5, 10)


def test_shift_earlier_does_not_shift_when_diff_above_threshold() -> None:
    # Anchor (h14) = 100; h10=141 daje diff 41 > 40 → no shift.
    prices = _prices(h10=141, h11=50, h12=50, h13=50, h14=100)
    new_consec, new_start = shift_earlier_if_cheap(
        prices, start=11, consecutive_hours=4
    )
    assert (new_consec, new_start) == (4, 11)


def test_shift_earlier_anchored_to_original_end_not_cumulative() -> None:
    # Kotwica = prices[end] = 100 (stała). Kolejne wcześniejsze godziny
    # coraz droższe: 139, 138, 135 (diff 39, 38, 35 — wszystkie < 40), aż
    # h7=141 (diff 41 > 40) — stop. Gdyby algorytm porównywał do poprzednio
    # dodanej godziny zamiast do kotwicy, różnice 139→138→135→141 byłyby
    # ~1-6 i extend nie zatrzymałby się na h7.
    prices = _prices(h7=141, h8=135, h9=138, h10=139, h11=50, h12=50, h13=50, h14=100)
    new_consec, new_start = shift_earlier_if_cheap(
        prices, start=11, consecutive_hours=4
    )
    assert (new_consec, new_start) == (7, 8)


def test_shift_earlier_respects_earliest_charge_hour() -> None:
    # Wszystkie godziny tanie, ale nie schodzimy poniżej 7:00.
    prices = [50.0] * 24
    new_consec, new_start = shift_earlier_if_cheap(
        prices, start=10, consecutive_hours=3
    )
    assert new_start == 7
    assert new_consec == 3 + (10 - 7)  # rozszerzone o ile kroków shift


def test_shift_earlier_extends_past_max_when_floor_zero() -> None:
    # Profil typu weekend: 9:00 prawie 0, 10:00-17:00 wszystko 0, anchor=0.
    # find_best_consecutive_hours wybiera N=MAX (=8), start=10. Shift powinien
    # rozszerzyć do start=9 (4.11<40), stop na 8 (71>40). Bez tego (z guardem
    # MAX) ładowalibyśmy 10-17 zamiast 9-17 — gubiąc ekstra-tanią godzinę.
    prices = [500.0] * 24
    prices[8] = 71.24
    prices[9] = 4.11
    for h in range(10, 18):
        prices[h] = 0.0
    new_consec, new_start = shift_earlier_if_cheap(
        prices, start=10, consecutive_hours=MAX_CONSECUTIVE_HOURS
    )
    assert (new_consec, new_start) == (MAX_CONSECUTIVE_HOURS + 1, 9)


def test_shift_earlier_skipped_when_heaters_blocked_all_day() -> None:
    # Wszystkie godziny ≥ 350 PLN/MWh → grzałki off cały dzień. Sink dla
    # surplus PV = tylko bateria (fixed cap) → szersze okno nie dodaje
    # absorpcji. Skip shift, zostaw oryginalne (start, n).
    prices = [400.0] * 24  # all hours >= 350
    new_consec, new_start = shift_earlier_if_cheap(
        prices, start=11, consecutive_hours=4
    )
    assert (new_consec, new_start) == (4, 11)


def test_shift_earlier_runs_when_3_hours_below_threshold() -> None:
    # Tolerance = 3: dokładnie 3h poniżej 350 → wciąż skip (≤ tolerance).
    prices = [400.0] * 24
    prices[12] = 100.0
    prices[13] = 100.0
    prices[14] = 100.0  # 3h below 350 → still skip
    new_consec, new_start = shift_earlier_if_cheap(
        prices, start=12, consecutive_hours=3
    )
    assert (new_consec, new_start) == (3, 12)


def test_shift_earlier_runs_when_4_hours_below_threshold() -> None:
    # 4h below 350 → above tolerance → standard shift logic kicks in.
    prices = [400.0] * 24
    for h in (11, 12, 13, 14):
        prices[h] = 100.0
    # Anchor = prices[14] = 100. prices[10] = 400 → diff 300 > 40 → no extend.
    # But heater_hours=4 > tolerance=3 → original logic runs (no early skip).
    new_consec, new_start = shift_earlier_if_cheap(
        prices, start=11, consecutive_hours=4
    )
    assert (new_consec, new_start) == (4, 11)


def test_shift_earlier_skipped_today_2026_05_07() -> None:
    # Real prices from 2026-05-07: min=459.94 (h13), all 24h >= 459 (>= 350).
    # Heater_hours = 0 → skip shift. Without skip, algorithm extends start
    # 10 → 8 (h8=613 within 40 of anchor h17=573), giving 08:00-18:00 window.
    # With skip: 10:00-18:00 (start_charge_hours[8] = 10, no extension).
    prices = [
        533.17,
        497.57,
        493.23,
        501.55,
        513.33,
        542.94,
        622.16,
        635.20,
        613.11,
        586.81,
        506.17,
        481.31,
        465.55,
        459.94,
        479.02,
        479.97,
        528.30,
        573.54,
        660.36,
        833.89,
        943.73,
        754.32,
        655.47,
        591.48,
    ]
    new_consec, new_start = shift_earlier_if_cheap(
        prices, start=10, consecutive_hours=8
    )
    assert (new_consec, new_start) == (8, 10)


@pytest.mark.skip
@pytest.mark.parametrize(("year", "month"), [(2024, 8), (2024, 9), (2026, 4)])
def test_snapshot_charge_hours(year, month) -> None:
    produced_csv = []
    for path in sorted(glob.glob(f"tests/fixtures/raw/rce_{year}_{month:02}*")):
        with open(path, encoding="utf-8") as file:
            rce_prices_raw = orjson.loads(file.read())
            rce_prices: RceDayPrices = RceDayPrices.create_from_json(rce_prices_raw)
            produced_csv.extend(create_csv(rce_prices))

    path = f"tests/fixtures/charge_hours_{year}_{month:02}.csv"
    with open(path, mode="w+", encoding="utf-8") as out:
        for line in produced_csv:
            out.write(line)
            out.write("\n")
