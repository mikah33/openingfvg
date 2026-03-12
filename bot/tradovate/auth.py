from __future__ import annotations
import asyncio
import json
import logging
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
from aiohttp import web

from bot.config import TradovateConfig

log = logging.getLogger("orfvg.auth")

REFRESH_INTERVAL = 85 * 60  # refresh token every 85 minutes (expires at 90)
TOKEN_FILE = Path(__file__).resolve().parent.parent.parent / ".oauth_token.json"


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

    def _save_token(self, data: dict):
        TOKEN_FILE.write_text(json.dumps(data))
        log.info("Token saved to %s", TOKEN_FILE)

    def _load_token(self) -> dict | None:
        if TOKEN_FILE.exists():
            try:
                return json.loads(TOKEN_FILE.read_text())
            except Exception:
                return None
        return None

    async def login(self) -> str:
        # Try loading saved token first
        saved = self._load_token()
        if saved and "accessToken" in saved:
            self._token = saved["accessToken"]
            exp_str = saved.get("expirationTime", "")
            if exp_str:
                try:
                    self._expiry = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
                    if self._expiry > datetime.now(timezone.utc):
                        self._user_id = saved.get("userId")
                        log.info("Loaded saved token, expires %s", self._expiry)
                        return self._token
                    else:
                        log.info("Saved token expired, need to re-authenticate")
                except Exception:
                    pass

        # No valid saved token — do OAuth flow
        return await self._oauth_login()

    async def _oauth_login(self) -> str:
        auth_code_future = asyncio.get_event_loop().create_future()

        # Temporary HTTP server to catch the OAuth callback
        async def handle_callback(request):
            code = request.query.get("code")
            if code:
                if not auth_code_future.done():
                    auth_code_future.set_result(code)
                return web.Response(
                    text="<html><body><h2>Authentication successful!</h2>"
                         "<p>You can close this tab and return to the bot.</p>"
                         "</body></html>",
                    content_type="text/html",
                )
            else:
                error = request.query.get("error", "unknown")
                if not auth_code_future.done():
                    auth_code_future.set_exception(RuntimeError(f"OAuth error: {error}"))
                return web.Response(text=f"OAuth error: {error}", status=400)

        # Start callback server on port 8081 (separate from webhook on 8080)
        callback_app = web.Application()
        callback_app.router.add_get("/oauth/callback", handle_callback)
        runner = web.AppRunner(callback_app)
        await runner.setup()
        site = web.TCPSite(runner, "localhost", 8080)
        await site.start()

        # Build OAuth URL and open browser
        params = urlencode({
            "client_id": self._config.cid,
            "redirect_uri": "http://localhost:8080/oauth/callback",
            "response_type": "code",
        })
        oauth_url = f"https://trader.tradovate.com/oauth?{params}"

        log.info("Opening browser for Tradovate login...")
        log.info("If browser doesn't open, visit: %s", oauth_url)
        webbrowser.open(oauth_url)

        try:
            # Wait for the callback (timeout 120 seconds)
            code = await asyncio.wait_for(auth_code_future, timeout=120)
            log.info("Got authorization code, exchanging for token...")
        finally:
            await runner.cleanup()

        # Exchange code for token
        session = await self._get_session()
        base = "https://live.tradovateapi.com" if self._config.mode == "live" else "https://demo.tradovateapi.com"
        token_url = f"{base}/auth/oauthtoken"
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": str(self._config.cid),
            "client_secret": self._config.sec,
            "redirect_uri": "http://localhost:8080/oauth/callback",
        }

        async with session.post(token_url, json=payload) as resp:
            data = await resp.json()
            if resp.status != 200:
                err = data.get("errorText", data)
                raise RuntimeError(f"Token exchange failed: {err}")

        # OAuth returns access_token (snake_case), direct auth returns accessToken (camelCase)
        self._token = data.get("accessToken") or data.get("access_token")
        if not self._token:
            raise RuntimeError(f"No token in response: {data}")

        self._user_id = data.get("userId") or data.get("sub")
        expires_in = data.get("expires_in")
        if "expirationTime" in data:
            self._expiry = datetime.fromisoformat(
                data["expirationTime"].replace("Z", "+00:00")
            )
        elif expires_in:
            self._expiry = datetime.now(timezone.utc) + __import__("datetime").timedelta(seconds=expires_in)

        # Save token for reuse
        save_data = dict(data)
        save_data["accessToken"] = self._token
        if self._expiry:
            save_data["expirationTime"] = self._expiry.isoformat()
        self._save_token(save_data)

        log.info("Authenticated via OAuth as user %s, token expires %s", self._user_id, self._expiry)
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
                        self._save_token(data)
                        log.info("Token refreshed, expires %s", self._expiry)
                    else:
                        log.warning("Token refresh failed, re-authenticating: %s", data)
                        await self._oauth_login()
            except Exception as e:
                log.error("Token refresh error: %s", e)

    def auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
