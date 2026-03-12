import asyncio
import logging
from ipaddress import ip_address, ip_network

from fastapi import FastAPI, Request, HTTPException

from bot.config import WebhookConfig
from bot.models import WebhookPayload

log = logging.getLogger("orfvg.webhook")

# TradingView's known webhook source IPs
TRADINGVIEW_IPS = {
    "52.89.214.238",
    "34.212.75.30",
    "54.218.53.128",
    "52.32.178.7",
}

# Also allow private ranges for local testing / tunnels
PRIVATE_NETWORKS = [
    ip_network("127.0.0.0/8"),
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
]


def is_allowed_ip(client_ip: str) -> bool:
    if client_ip in TRADINGVIEW_IPS:
        return True
    try:
        addr = ip_address(client_ip)
        return any(addr in net for net in PRIVATE_NETWORKS)
    except ValueError:
        return False


def create_app(config: WebhookConfig, queue: asyncio.Queue) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post(f"/{config.secret_path}")
    async def receive_webhook(request: Request):
        # IP check — passphrase is the real security, IP is just extra layer
        client_ip = request.client.host
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            client_ip = forwarded_for.split(",")[0].strip()

        if not is_allowed_ip(client_ip):
            log.info("Webhook from non-whitelisted IP: %s (passphrase will be checked)", client_ip)

        # Parse body
        try:
            body = await request.json()
        except Exception:
            log.warning("Invalid JSON in webhook body")
            raise HTTPException(status_code=400, detail="Invalid JSON")

        # Validate as WebhookPayload
        try:
            payload = WebhookPayload(**body)
        except Exception as e:
            log.warning("Invalid webhook payload: %s", e)
            raise HTTPException(status_code=400, detail="Invalid payload")

        # Check passphrase
        if config.passphrase and payload.passphrase != config.passphrase:
            log.warning("Invalid passphrase in webhook")
            raise HTTPException(status_code=403, detail="Invalid passphrase")

        log.info("Webhook received: action=%s from %s", payload.action, client_ip)
        await queue.put(payload)

        return {"status": "ok"}

    @app.get("/health")
    async def health():
        return {"status": "healthy"}

    return app
