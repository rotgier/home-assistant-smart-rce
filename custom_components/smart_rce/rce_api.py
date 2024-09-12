"""API for fetching RCE prices."""

from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from typing import Final
from zoneinfo import ZoneInfo

from aiohttp import ClientSession

HTTP_HEADERS: dict[str, str] = {
    "Accept-Encoding": "gzip",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.0.0 Safari/537.36",
}
ENDPOINT: Final[str] = (
    "https://api.raporty.pse.pl/api/rce-pln?$select=udtczas,source_datetime,rce_pln&$filter=doba eq '{}'"
)
TIMEZONE: Final = ZoneInfo("Europe/Warsaw")


@dataclass
class RceDayPrices:
    """RCE prices of given day."""

    published_at: datetime
    prices: list[dict[str, float | datetime]]


class ApiError(Exception):
    """Raised when RCE API request ended in error."""

    def __init__(self, status: str) -> None:
        """Initialize."""
        super().__init__(status)
        self.status = status


class RceApi:
    """Main class to fetch RCE prices."""

    def __init__(self, session: ClientSession) -> None:  # noqa: D107
        self._session = session

    async def async_get_prices(self, day: datetime) -> RceDayPrices:
        """Fetch RCE prices for given day."""
        url = ENDPOINT.format(day.strftime("%Y-%m-%d"))
        async with self._session.get(
            url, headers=HTTP_HEADERS, allow_redirects=False
        ) as resp:
            if resp.status != HTTPStatus.OK.value:
                text = await resp.text()
                raise ApiError(f"Invalid response from RCE API: {resp.status} {text}")

            data = await resp.json()
            prices = []
            published_at = None
            for price in data["value"]:
                published_at = price["source_datetime"]
                date_hour = datetime.fromisoformat(price["udtczas"])
                date_hour = date_hour.replace(tzinfo=TIMEZONE)
                if date_hour.minute == 15:
                    prices.append(
                        {
                            "datetime": date_hour.replace(minute=0),
                            "price": price["rce_pln"],
                        }
                    )

            return RceDayPrices(published_at, prices) if published_at else None
