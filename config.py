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
    REQUIRED_CHAT_ID = REQUIRED_CHAT_ID_RAW

# PostgreSQL (Neon) or SQLite.
# If DATABASE_URL starts with postgres:// or postgresql:// — use asyncpg.
# Otherwise fall back to SQLite file at DB_PATH.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DB_PATH = os.getenv("DB_PATH", "rp_bot.db")


def _parse_admin_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            continue
    return ids


# Админы бота: видят полный список анкет и могут банить/разбанивать.
# По умолчанию — владелец и запасной аккаунт; можно переопределить через
# переменную окружения ADMIN_IDS (через запятую).
DEFAULT_ADMIN_IDS = {6059246800, 743634485}
ADMIN_IDS = _parse_admin_ids(os.getenv("ADMIN_IDS", "")) or DEFAULT_ADMIN_IDS


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS
