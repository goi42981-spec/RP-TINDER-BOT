"""FastAPI webhook application for Render deployment.

Exposes ``/webhook`` for Telegram updates and ``/healthz`` for UptimeRobot
keep-alive pings.  On startup the app registers the webhook with Telegram;
on shutdown it deletes it.
"""

from __future__ import annotations

import logging
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request

from config import BOT_TOKEN, REQUIRED_CHAT_ID, REQUIRED_CHAT_LINK
from db import close_db, get_db
from bot import router

logger = logging.getLogger(__name__)

WEBHOOK_PATH = "/webhook"


def _resolve_webhook_url() -> str:
    for var in ("WEBHOOK_URL", "RENDER_EXTERNAL_URL"):
        url = os.environ.get(var, "").strip()
        if url:
            return url.rstrip("/")

    render_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "").strip()
    if render_host:
        return f"https://{render_host}"

    raise RuntimeError(
        "Cannot determine webhook URL. Set WEBHOOK_URL or deploy on Render "
        "(which sets RENDER_EXTERNAL_URL automatically)."
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    db = await get_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    webhook_url = _resolve_webhook_url()
    webhook_secret = os.environ.get("WEBHOOK_SECRET", "").strip() or secrets.token_urlsafe(32)

    me = await bot.get_me()
    logger.info("Bot @%s (id=%s) starting in webhook mode", me.username, me.id)

    await bot.set_webhook(
        url=f"{webhook_url}{WEBHOOK_PATH}",
        secret_token=webhook_secret,
        allowed_updates=dp.resolve_used_update_types(),
        drop_pending_updates=True,
    )
    logger.info("Webhook registered: %s%s", webhook_url, WEBHOOK_PATH)

    app.state.bot = bot
    app.state.dp = dp
    app.state.db = db
    app.state.webhook_secret = webhook_secret
    app.state.bot_username = me.username

    try:
        yield
    finally:
        try:
            await bot.delete_webhook()
        except Exception:
            logger.exception("Failed to delete webhook during shutdown")
        await bot.session.close()
        await close_db()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def root() -> dict[str, str]:
    bot_username = getattr(app.state, "bot_username", None)
    return {"status": "ok", "bot": f"@{bot_username}" if bot_username else "starting"}


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return await root()


@app.post(WEBHOOK_PATH)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Annotated[str | None, Header()] = None,
) -> dict[str, bool]:
    if x_telegram_bot_api_secret_token != app.state.webhook_secret:
        raise HTTPException(status_code=403, detail="Invalid secret token")

    data = await request.json()
    update = Update.model_validate(data, context={"bot": app.state.bot})
    await app.state.dp.feed_update(bot=app.state.bot, update=update)
    return {"ok": True}
