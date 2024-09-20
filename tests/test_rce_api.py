"""Tests for accuweather package."""

from datetime import datetime

import aiofiles
from aiohttp import ClientSession
from custom_components.smart_rce.infrastructure.rce_api import RceApi
import orjson
import pytest
from pytest_socket import _remove_restrictions, socket_allow_hosts


@pytest.mark.asyncio
@pytest.mark.enable_socket
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_get_prices_raw() -> None:
    """Test get prices."""
    socket_allow_hosts("api.raporty.pse.pl")
    _remove_restrictions()

    async with ClientSession() as session:
        rce_api = RceApi(session)

        first_september = datetime(year=2024, month=9, day=1)
        raw_data = await rce_api._async_get_prices_raw(first_september)

        path = "tests/fixtures/raw/rce_2024_09_01.json"
        async with aiofiles.open(path, encoding="utf-8") as file:
            expected = orjson.loads(await file.read())

        assert raw_data == expected


@pytest.mark.skip
@pytest.mark.asyncio
@pytest.mark.enable_socket
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_collect_raw_prices() -> None:
    """Test get prices."""
    socket_allow_hosts("api.raporty.pse.pl")
    _remove_restrictions()

    session = ClientSession()

    rce_api = RceApi(session)

    month = 9
    for day in range(1, 21):
        day_date = datetime(year=2024, month=month, day=day)
        raw_data = await rce_api._async_get_prices_raw(day_date)
        raw_json = orjson.dumps(raw_data, option=orjson.OPT_INDENT_2)
        path = f"tests/fixtures/raw/rce_2024_{month:02}_{day:02}.json"
        async with aiofiles.open(path, mode="x", encoding="utf-8") as file:
            await file.write(raw_json.decode("utf-8"))

    await session.close()
