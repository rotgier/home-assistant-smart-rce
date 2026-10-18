"""Charge curve — energy per pp of SoC by band, both directions."""

from custom_components.smart_rce.domain.charge_curve import kwh_to_full, pp_below_full
import pytest


def test_kwh_to_full_walks_the_bands():
    assert kwh_to_full(10) == pytest.approx(9.92)
    assert kwh_to_full(15) == pytest.approx(9.92 - 5 * 0.115)
    assert kwh_to_full(95) == pytest.approx(3 * 0.04 + 2 * 0.08)
    assert kwh_to_full(100) == 0.0


def test_pp_below_full_walks_down_from_the_top():
    assert pp_below_full(0.0) == 0.0
    assert pp_below_full(0.16) == pytest.approx(2)
    assert pp_below_full(0.16 + 0.28) == pytest.approx(9)
    assert pp_below_full(0.16 + 0.28 + 0.44) == pytest.approx(11)
    assert pp_below_full(0.16 + 0.28 + 0.44 + 1.54) == pytest.approx(25)
    assert pp_below_full(0.16 + 0.02) == pytest.approx(2.5)


def test_pp_below_full_stops_at_the_bottom_band():
    assert pp_below_full(100.0) == pytest.approx(90)


@pytest.mark.parametrize("soc", [10, 17, 37, 63, 90, 99])
def test_directions_agree(soc):
    assert pp_below_full(kwh_to_full(soc)) == pytest.approx(100 - soc)
