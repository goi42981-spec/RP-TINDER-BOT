import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
REQUIRED_CHAT_ID_RAW = os.getenv("REQUIRED_CHAT_ID", "").strip()
REQUIRED_CHAT_LINK = os.getenv("REQUIRED_CHAT_LINK", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан. Создай .env по образцу .env.example")

if not REQUIRED_CHAT_ID_RAW:
    raise RuntimeError("REQUIRED_CHAT_ID не задан. Создай .env по образцу .env.example")

try:
    REQUIRED_CHAT_ID: int | str = int(REQUIRED_CHAT_ID_RAW)
except ValueError:
    # Поддерживаем @username для публичных чатов
    REQUIRED_CHAT_ID = REQUIRED_CHAT_ID_RAW

DB_PATH = os.getenv("DB_PATH", "rp_bot.db")
