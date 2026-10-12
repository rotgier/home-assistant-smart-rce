"""Charge curve — energy per pp of SoC by band, both directions."""

from custom_components.smart_rce.domain.charge_curve import kwh_to_full, pp_below_full
import pytest


def test_kwh_to_full_walks_the_bands():
    assert kwh_to_full(10) == pytest.approx(10.64)
    assert kwh_to_full(15) == pytest.approx(5 * 0.148 + 10.64 - 1.48)
    assert kwh_to_full(95) == pytest.approx(5 * 0.119)
    assert kwh_to_full(100) == 0.0


def test_pp_below_full_walks_down_from_the_top():
    assert pp_below_full(0.0) == 0.0
    assert pp_below_full(1.19) == pytest.approx(10)
    assert pp_below_full(1.19 + 1.62) == pytest.approx(25)
    assert pp_below_full(1.19 + 0.054) == pytest.approx(10.5)


def test_pp_below_full_stops_at_the_bottom_band():
    assert pp_below_full(100.0) == pytest.approx(90)


@pytest.mark.parametrize("soc", [10, 17, 37, 63, 90, 99])
def test_directions_agree(soc):
    assert pp_below_full(kwh_to_full(soc)) == pytest.approx(100 - soc)
