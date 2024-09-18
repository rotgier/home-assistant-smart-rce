"""API for fetching RCE prices."""

from datetime import datetime
from http import HTTPStatus
from typing import Any,Final

from aiohttp import ClientSession

from ..domain.rce import RceDayPrices

HTTP_HEADERS: dict[str, str] = {
    "Accept-Encoding": "gzip",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.0.0 Safari/537.36",
}
ENDPOINT: Final[str] = (
    "https://api.raporty.pse.pl/api/rce-pln?$select=udtczas,source_datetime,rce_pln&$filter=doba eq '{}'"
)


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
        data = await self._async_get_prices_raw(day)
        return RceDayPrices.create_from_json(data)
    async def _async_get_prices_raw(self, day: datetime) -> dict[Any, Any]:
        """Fetch RCE prices for given day."""
        url = ENDPOINT.format(day.strftime("%Y-%m-%d"))
        async with self._session.get(
            url, headers=HTTP_HEADERS, allow_redirects=False
        ) as resp:
            if resp.status != HTTPStatus.OK.value:
                text = await resp.text()
                raise ApiError(f"Invalid response from RCE API: {resp.status} {text}")
            return await resp.json()

