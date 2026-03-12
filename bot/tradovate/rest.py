from __future__ import annotations
import logging

import aiohttp

from bot.config import TradovateConfig
from bot.tradovate.auth import TradovateAuth

log = logging.getLogger("orfvg.rest")


class TradovateREST:
    def __init__(self, config: TradovateConfig, auth: TradovateAuth):
        self._base = config.rest_url
        self._auth = auth
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _get(self, path: str) -> dict | list:
        session = await self._get_session()
        url = f"{self._base}{path}"
        async with session.get(url, headers=self._auth.auth_headers()) as resp:
            data = await resp.json()
            if resp.status != 200:
                log.error("GET %s failed: %s", path, data)
            return data

    async def _post(self, path: str, payload: dict = None) -> dict:
        session = await self._get_session()
        url = f"{self._base}{path}"
        async with session.post(url, json=payload or {}, headers=self._auth.auth_headers()) as resp:
            data = await resp.json()
            if resp.status != 200:
                log.error("POST %s failed: %s", path, data)
            return data

    async def get_accounts(self) -> list:
        return await self._get("/account/list")

    async def get_positions(self) -> list:
        return await self._get("/position/list")

    async def get_cash_balance(self) -> float:
        data = await self._get(
            f"/cashBalance/getCashBalanceSnapshot?accountId={self._auth.account_id}"
        )
        if isinstance(data, dict):
            return float(data.get("totalCashValue", 0.0))
        return 0.0

    async def get_contract_by_name(self, symbol: str) -> dict | None:
        data = await self._get(f"/contract/find?name={symbol}")
        if isinstance(data, dict) and "id" in data:
            return data
        return None

    async def cancel_order(self, order_id: int) -> dict:
        return await self._post("/order/cancelorder", {"orderId": order_id})

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
