import aiosqlite

from config import DB_PATH


PROFILES_SQL = """
CREATE TABLE IF NOT EXISTS profiles (
    user_id          INTEGER PRIMARY KEY,
    char_gender      TEXT NOT NULL,
    preferred_gender TEXT NOT NULL,
    username         TEXT NOT NULL,
    profile_link     TEXT NOT NULL,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

SWIPES_SQL = """
CREATE TABLE IF NOT EXISTS swipes (
    from_user_id INTEGER NOT NULL,
    to_user_id   INTEGER NOT NULL,
    action       TEXT NOT NULL CHECK(action IN ('like', 'pass')),
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (from_user_id, to_user_id)
);
"""

ANY_LABEL = "Не важно"


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(PROFILES_SQL)
        await conn.execute(SWIPES_SQL)
        await conn.commit()


async def upsert_profile(
    user_id: int,
    char_gender: str,
    preferred_gender: str,
    username: str,
    profile_link: str,
) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            INSERT INTO profiles (user_id, char_gender, preferred_gender, username, profile_link)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                char_gender      = excluded.char_gender,
                preferred_gender = excluded.preferred_gender,
                username         = excluded.username,
                profile_link     = excluded.profile_link,
                updated_at       = CURRENT_TIMESTAMP;
            """,
            (user_id, char_gender, preferred_gender, username, profile_link),
        )
        await conn.commit()


async def get_profile(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM profiles WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def find_next_candidate(user_id: int) -> dict | None:
    """Найти следующую подходящую анкету в общем фиде (без тех, кто уже лайкнул меня).

    Те, кто лайкнул меня, показываются отдельно (через уведомления / /likes), чтобы
    одна анкета не показывалась дважды."""
    me = await get_profile(user_id)
    if not me:
        return None

    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT p.* FROM profiles p
            WHERE p.user_id != :me
              AND p.user_id NOT IN (
                  SELECT to_user_id FROM swipes WHERE from_user_id = :me
              )
              -- исключаем тех, кто уже лайкнул меня — они в "входящих лайках"
              AND p.user_id NOT IN (
                  SELECT from_user_id FROM swipes WHERE to_user_id = :me AND action = 'like'
              )
              -- мои предпочтения подходят к их char_gender
              AND (:my_pref = :any_label OR :my_pref = p.char_gender)
              -- их предпочтения подходят к моему char_gender (взаимность)
              AND (p.preferred_gender = :any_label OR p.preferred_gender = :my_char)
            ORDER BY RANDOM()
            LIMIT 1;
            """,
            {
                "me": user_id,
                "my_pref": me["preferred_gender"],
                "my_char": me["char_gender"],
                "any_label": ANY_LABEL,
            },
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def find_next_pending_like(user_id: int) -> dict | None:
    """Найти анкету того, кто лайкнул меня, но на кого я ещё не ответила."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT p.* FROM profiles p
            JOIN swipes s ON s.from_user_id = p.user_id
            WHERE s.to_user_id = :me
              AND s.action = 'like'
              AND p.user_id NOT IN (
                  SELECT to_user_id FROM swipes WHERE from_user_id = :me
              )
            ORDER BY s.created_at ASC
            LIMIT 1;
            """,
            {"me": user_id},
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def count_pending_likes(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            """
            SELECT COUNT(*) FROM swipes s
            WHERE s.to_user_id = ?
              AND s.action = 'like'
              AND s.from_user_id NOT IN (
                  SELECT to_user_id FROM swipes WHERE from_user_id = ?
              );
            """,
            (user_id, user_id),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def record_swipe(from_user_id: int, to_user_id: int, action: str) -> None:
    assert action in ("like", "pass")
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            INSERT INTO swipes (from_user_id, to_user_id, action)
            VALUES (?, ?, ?)
            ON CONFLICT(from_user_id, to_user_id) DO UPDATE SET
                action     = excluded.action,
                created_at = CURRENT_TIMESTAMP;
            """,
            (from_user_id, to_user_id, action),
        )
        await conn.commit()


async def get_swipe(from_user_id: int, to_user_id: int) -> str | None:
    """Вернёт 'like' / 'pass' если свайп уже был, иначе None."""
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT action FROM swipes WHERE from_user_id = ? AND to_user_id = ?",
            (from_user_id, to_user_id),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def has_liked_me(other_user_id: int, me_user_id: int) -> bool:
    """Лайкал ли other_user меня раньше?"""
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            """
            SELECT 1 FROM swipes
            WHERE from_user_id = ? AND to_user_id = ? AND action = 'like'
            LIMIT 1;
            """,
            (other_user_id, me_user_id),
        ) as cur:
            return await cur.fetchone() is not None
