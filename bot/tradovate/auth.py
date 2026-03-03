import asyncio
import logging
from datetime import datetime, timezone

import aiohttp

from bot.config import TradovateConfig

log = logging.getLogger("orfvg.auth")

REFRESH_INTERVAL = 85 * 60  # refresh token every 85 minutes (expires at 90)


class TradovateAuth:
    def __init__(self, config: TradovateConfig):
        self._config = config
        self._token: str | None = None
        self._expiry: datetime | None = None
        self._user_id: int | None = None
        self._account_id: int | None = None
        self._account_spec: str | None = None
        self._session: aiohttp.ClientSession | None = None
        self._refresh_task: asyncio.Task | None = None

    @property
    def token(self) -> str:
        if self._token is None:
            raise RuntimeError("Not authenticated. Call login() first.")
        return self._token

    @property
    def account_id(self) -> int:
        if self._account_id is None:
            raise RuntimeError("Account not loaded. Call fetch_account_info() first.")
        return self._account_id

    @property
    def account_spec(self) -> str:
        if self._account_spec is None:
            raise RuntimeError("Account not loaded. Call fetch_account_info() first.")
        return self._account_spec

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def login(self) -> str:
        session = await self._get_session()
        url = f"{self._config.rest_url}/auth/accesstokenrequest"
        payload = {
            "name": self._config.username,
            "password": self._config.password,
            "appId": self._config.app_id,
            "appVersion": self._config.app_version,
            "deviceId": self._config.device_id,
            "cid": self._config.cid,
            "sec": self._config.sec,
        }

        async with session.post(url, json=payload) as resp:
            data = await resp.json()
            if resp.status != 200 or "errorText" in data:
                err = data.get("errorText", data)
                raise RuntimeError(f"Auth failed: {err}")

        self._token = data["accessToken"]
        self._user_id = data.get("userId")
        self._expiry = datetime.fromisoformat(
            data["expirationTime"].replace("Z", "+00:00")
        )
        log.info("Authenticated as user %s, token expires %s", self._user_id, self._expiry)
        return self._token

    async def fetch_account_info(self):
        session = await self._get_session()
        url = f"{self._config.rest_url}/account/list"
        headers = {"Authorization": f"Bearer {self._token}"}

        async with session.get(url, headers=headers) as resp:
            accounts = await resp.json()

        if not accounts:
            raise RuntimeError("No accounts found")

        acct = accounts[0]
        self._account_id = acct["id"]
        self._account_spec = acct["name"]
        log.info("Using account: %s (id=%d)", self._account_spec, self._account_id)

    async def refresh_loop(self):
        while True:
            await asyncio.sleep(REFRESH_INTERVAL)
            try:
                session = await self._get_session()
                url = f"{self._config.rest_url}/auth/renewaccesstoken"
                headers = {"Authorization": f"Bearer {self._token}"}
                async with session.post(url, headers=headers) as resp:
                    data = await resp.json()
                    if resp.status == 200 and "accessToken" in data:
                        self._token = data["accessToken"]
                        self._expiry = datetime.fromisoformat(
                            data["expirationTime"].replace("Z", "+00:00")
                        )
                        log.info("Token refreshed, expires %s", self._expiry)
                    else:
                        log.warning("Token refresh failed, re-logging in: %s", data)
                        await self.login()
            except Exception as e:
                log.error("Token refresh error: %s, re-logging in", e)
                try:
                    await self.login()
                except Exception as e2:
                    log.error("Re-login also failed: %s", e2)

    def auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
