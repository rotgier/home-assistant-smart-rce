import glob

from custom_components.smart_rce.domain.ems import create_csv
from custom_components.smart_rce.domain.rce import RceDayPrices
import orjson
import pytest


@pytest.mark.parametrize("month", [8, 9])
def test_find_charge_hours(month) -> None:
    produced_csv = []
    for path in sorted(glob.glob(f"tests/fixtures/raw/rce_2024_{month:02}*")):
        with open(path, encoding="utf-8") as file:
            rce_prices_raw = orjson.loads(file.read())
            rce_prices: RceDayPrices = RceDayPrices.create_from_json(rce_prices_raw)
            produced_csv.extend(create_csv(rce_prices))
    produced = "\n".join(produced_csv) + "\n"

    path = f"tests/fixtures/charge_hours_2024_{month:02}.csv"
    with open(path, encoding="utf-8") as file:
        assert produced == file.read()


@pytest.mark.skip
@pytest.mark.parametrize("month", [8, 9])
def test_snapshot_charge_hours(month) -> None:
    produced_csv = []
    for path in sorted(glob.glob(f"tests/fixtures/raw/rce_2024_{month:02}*")):
        with open(path, encoding="utf-8") as file:
            rce_prices_raw = orjson.loads(file.read())
            rce_prices: RceDayPrices = RceDayPrices.create_from_json(rce_prices_raw)
            produced_csv.extend(create_csv(rce_prices))

    path = f"tests/fixtures/charge_hours_2024_{month:02}.csv"
    with open(path, mode="w+", encoding="utf-8") as out:
        for line in produced_csv:
            out.write(line)
            out.write("\n")
