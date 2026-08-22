"""Reading RCEm off the PSE page — the quirks that make or break the parse.

The sample mirrors the real page: a table per year, a heading row per month, an
original price and a correction row beneath it, footnote markers glued to month
names, and non-breaking spaces inside cells.
"""

from custom_components.smart_rce.deposit.domain.billing_month import BillingMonth
from custom_components.smart_rce.deposit.infrastructure.rcem_reader import PseRcemReader
import pytest

_PAGE = """
<table>
  <tr><td colspan="4">2025</td></tr>
  <tr><td></td><td>cena [zł/MWh]**</td><td>data publikacji</td><td>różnica</td></tr>
  <tr><td><strong>luty</strong></td></tr>
  <tr><td><span>RCEm&nbsp;</span></td><td align="right">442,02</td><td>11.03.2025</td><td>-</td></tr>
  <tr><td>skorygowana RCEm*</td><td align="right">-</td><td>-</td><td>-</td></tr>
  <tr><td bgcolor="#eeeeee"><strong>marzec***&nbsp;</strong></td></tr>
  <tr><td><span>RCEm</span></td><td align="right">182,96</td><td>11.04.2025</td><td>-</td></tr>
  <tr><td>skorygowana RCEm*</td><td align="right">178,84</td><td>11.03.2026</td><td>-2,25</td></tr>
</table>
<table>
  <tr><td colspan="4">2026</td></tr>
  <tr><td><strong>lipiec</strong></td></tr>
  <tr><td>RCEm</td><td align="right">262,88</td><td>11.08.2026</td><td>-</td></tr>
  <tr><td>skorygowana RCEm*</td><td align="right">-</td><td>-</td><td>-</td></tr>
  <tr><td><strong>sierpień</strong></td></tr>
  <tr><td>RCEm</td><td align="right">-</td><td>-</td><td>-</td></tr>
</table>
"""


@pytest.fixture(name="prices")
def prices_fixture():
    return PseRcemReader.parse_prices(_PAGE)


def test_reads_a_published_price(prices):
    assert prices[BillingMonth(2025, 2)] == pytest.approx(442.02)


def test_a_correction_replaces_the_original_price():
    """PSE revises up to a year back, and the revision is what settles the bill."""
    assert PseRcemReader.parse_prices(_PAGE)[BillingMonth(2025, 3)] == pytest.approx(
        178.84
    )


def test_footnote_markers_on_a_month_do_not_hide_it(prices):
    """`marzec***` once cost us two months: March vanished and February took its price."""
    assert BillingMonth(2025, 3) in prices
    assert prices[BillingMonth(2025, 2)] != pytest.approx(178.84)


def test_each_table_carries_its_own_year(prices):
    assert prices[BillingMonth(2026, 7)] == pytest.approx(262.88)


def test_a_month_awaiting_publication_is_absent_not_zero(prices):
    """A dash means "not published yet" — zero would price the export at nothing."""
    assert BillingMonth(2026, 8) not in prices


def test_a_page_that_stopped_looking_like_itself_yields_nothing():
    """Then the report keeps the shipped table instead of inventing prices."""
    assert (
        PseRcemReader.parse_prices("<html><body>Przerwa techniczna</body></html>") == {}
    )
