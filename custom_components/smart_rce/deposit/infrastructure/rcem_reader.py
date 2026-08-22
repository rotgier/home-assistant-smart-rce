"""RCEm scraped from the PSE OIRE page, because there is no API for it.

PSE's report API (`api.raporty.pse.pl`) publishes 180 datasets and not one of
them is monthly — RCEm lives only as a table on the OIRE page, rendered
server-side. So it is scraped: one GET of one public page per refresh.

Scraping is fragile by nature, so nothing here is allowed to matter. The prices
returned merge *on top of* the shipped table, which means a changed page layout
or an outage degrades to exactly the behaviour we had before this file existed:
the newest months lack a price and quietly drop out of the regime comparison.

Reading the table needs two quirks that cost nothing to handle and break
everything when missed: the month heading carries footnote markers
(`marzec***`), and a corrected price sits in its own row below the original one
— PSE may revise a published RCEm up to twelve months back, and the correction
is the figure that settles the bill.
"""

from __future__ import annotations

import asyncio
from html.parser import HTMLParser
import logging
import re
from typing import TYPE_CHECKING, Final

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..domain.billing_month import BillingMonth

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.core import HomeAssistant

_URL: Final = (
    "https://www.pse.pl/oire/rcem-rynkowa-miesieczna-cena-energii-elektrycznej"
)
_TIMEOUT: Final = 30
_MONTHS: Final = (
    "styczeń",
    "luty",
    "marzec",
    "kwiecień",
    "maj",
    "czerwiec",
    "lipiec",
    "sierpień",
    "wrzesień",
    "październik",
    "listopad",
    "grudzień",
)
_PRICE_ROW: Final = ("rcem", "skorygowana")
_NUMBER: Final = re.compile(r"-?\d+(\.\d+)?")

_LOGGER = logging.getLogger(__name__)


class PseRcemReader:
    """Fetches the published monthly market price from PSE."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def async_prices(self) -> Mapping[BillingMonth, float]:
        """Return every published RCEm, or nothing at all when the fetch fails."""
        session = async_get_clientsession(self._hass)
        async with asyncio.timeout(_TIMEOUT):
            response = await session.get(_URL)
            response.raise_for_status()
            page = await response.text()
        prices = self.parse_prices(page)
        if not prices:
            _LOGGER.warning(
                "RCEm: PSE page parsed to nothing — layout probably changed (%s)", _URL
            )
        return prices

    @staticmethod
    def parse_prices(page: str) -> dict[BillingMonth, float]:
        """Read the year tables. A corrected price overwrites the original one."""
        prices: dict[BillingMonth, float] = {}
        for rows in _tables(page):
            year: int | None = None
            month: int | None = None
            for cells in rows:
                heading = cells[0] if cells else ""
                if len(cells) == 1 and re.fullmatch(r"\d{4}", heading):
                    year = int(heading)
                elif len(cells) == 1 and _month_number(heading) is not None:
                    month = _month_number(heading)
                elif year and month and heading.lower().startswith(_PRICE_ROW):
                    price = _price(cells[1]) if len(cells) > 1 else None
                    if price is not None:
                        prices[BillingMonth(year, month)] = price
        return prices


def _month_number(heading: str) -> int | None:
    """Month names carry footnote markers, so match on the name alone."""
    name = heading.strip("*\xa0 ").lower()
    return _MONTHS.index(name) + 1 if name in _MONTHS else None


def _price(text: str) -> float | None:
    """`551,96` is a price; `-` is "not published yet" and every other column is noise."""
    cleaned = text.replace("\xa0", "").replace(" ", "").replace(",", ".")
    return float(cleaned) if _NUMBER.fullmatch(cleaned) else None


def _tables(page: str) -> list[list[list[str]]]:
    """Every table as a row x cell matrix — immune to the portal's styling churn."""
    parser = _TableParser()
    parser.feed(page)
    return parser.tables


class _TableParser(HTMLParser):
    """Collects table text, ignoring attributes and nesting."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._rows: list[list[str]] | None = None
        self._cells: list[str] | None = None
        self._text: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._rows = []
        elif tag == "tr" and self._rows is not None:
            self._cells = []
        elif tag in ("td", "th") and self._cells is not None:
            self._text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._rows is not None:
            self.tables.append(self._rows)
            self._rows = None
        elif tag == "tr" and self._cells is not None and self._rows is not None:
            self._rows.append(self._cells)
            self._cells = None
        elif tag in ("td", "th") and self._text is not None and self._cells is not None:
            self._cells.append(" ".join("".join(self._text).split()))
            self._text = None

    def handle_data(self, data: str) -> None:
        if self._text is not None:
            self._text.append(data)
