import glob

from custom_components.smart_rce.domain.ems import (
    MAX_CONSECUTIVE_HOURS,
    create_csv,
    shift_earlier_if_cheap,
)
from custom_components.smart_rce.domain.rce import RceDayPrices
import orjson
import pytest


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
