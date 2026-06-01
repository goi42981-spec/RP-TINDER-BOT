"""Database layer with SQLite and PostgreSQL (Neon) backends.

When ``DATABASE_URL`` starts with ``postgres://`` or ``postgresql://``, the
module uses **asyncpg** for persistent storage on Neon (or any hosted
Postgres).  Otherwise it falls back to a local SQLite file via
**aiosqlite** — handy for development but ephemeral on Render's free tier.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

import aiosqlite
import asyncpg  # type: ignore[import-untyped]

from config import DATABASE_URL, DB_PATH

logger = logging.getLogger(__name__)

ANY_LABEL = "Не важно"

# ── Schemas ──────────────────────────────────────────────────────────────────

SQLITE_SCHEMA = """\
CREATE TABLE IF NOT EXISTS profiles (
    user_id          INTEGER PRIMARY KEY,
    char_gender      TEXT NOT NULL,
    preferred_gender TEXT NOT NULL,
    username         TEXT NOT NULL,
    profile_link     TEXT NOT NULL,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS swipes (
    from_user_id INTEGER NOT NULL,
    to_user_id   INTEGER NOT NULL,
    action       TEXT NOT NULL CHECK(action IN ('like', 'pass')),
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (from_user_id, to_user_id)
);

CREATE TABLE IF NOT EXISTS banned (
    user_id    INTEGER PRIMARY KEY,
    reason     TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

POSTGRES_SCHEMA = """\
CREATE TABLE IF NOT EXISTS profiles (
    user_id          BIGINT PRIMARY KEY,
    char_gender      TEXT NOT NULL,
    preferred_gender TEXT NOT NULL,
    username         TEXT NOT NULL,
    profile_link     TEXT NOT NULL,
    created_at       TIMESTAMPTZ DEFAULT now(),
    updated_at       TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS swipes (
    from_user_id BIGINT NOT NULL,
    to_user_id   BIGINT NOT NULL,
    action       TEXT NOT NULL CHECK(action IN ('like', 'pass')),
    created_at   TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (from_user_id, to_user_id)
);

CREATE TABLE IF NOT EXISTS banned (
    user_id    BIGINT PRIMARY KEY,
    reason     TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);
"""


# ── Abstract interface ───────────────────────────────────────────────────────

class Database(ABC):
    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def upsert_profile(
        self, user_id: int, char_gender: str, preferred_gender: str,
        username: str, profile_link: str,
    ) -> None: ...

    @abstractmethod
    async def get_profile(self, user_id: int) -> dict[str, Any] | None: ...

    @abstractmethod
    async def find_next_candidate(self, user_id: int) -> dict[str, Any] | None: ...

    @abstractmethod
    async def find_next_pending_like(self, user_id: int) -> dict[str, Any] | None: ...

    @abstractmethod
    async def count_pending_likes(self, user_id: int) -> int: ...

    @abstractmethod
    async def record_swipe(self, from_user_id: int, to_user_id: int, action: str) -> None: ...

    @abstractmethod
    async def get_swipe(self, from_user_id: int, to_user_id: int) -> str | None: ...

    @abstractmethod
    async def has_liked_me(self, other_user_id: int, me_user_id: int) -> bool: ...

    @abstractmethod
    async def list_all_profiles(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def find_profile_by_username(self, username: str) -> dict[str, Any] | None: ...

    @abstractmethod
    async def delete_profile(self, user_id: int) -> bool: ...

    @abstractmethod
    async def ban_user(self, user_id: int, reason: str | None = None) -> None: ...

    @abstractmethod
    async def unban_user(self, user_id: int) -> bool: ...

    @abstractmethod
    async def is_banned(self, user_id: int) -> bool: ...


# ── SQLite backend ───────────────────────────────────────────────────────────

class SqliteDatabase(Database):
    def __init__(self, path: str) -> None:
        self._path = path

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SQLITE_SCHEMA)
        await self._conn.commit()
        logger.info("SQLite database ready: %s", self._path)

    async def close(self) -> None:
        await self._conn.close()

    async def upsert_profile(
        self, user_id: int, char_gender: str, preferred_gender: str,
        username: str, profile_link: str,
    ) -> None:
        await self._conn.execute(
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
        await self._conn.commit()

    async def get_profile(self, user_id: int) -> dict[str, Any] | None:
        async with self._conn.execute(
            "SELECT * FROM profiles WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def find_next_candidate(self, user_id: int) -> dict[str, Any] | None:
        me = await self.get_profile(user_id)
        if not me:
            return None
        async with self._conn.execute(
            """
            SELECT p.* FROM profiles p
            WHERE p.user_id != :me
              AND p.user_id NOT IN (
                  SELECT to_user_id FROM swipes WHERE from_user_id = :me
              )
              AND p.user_id NOT IN (
                  SELECT from_user_id FROM swipes WHERE to_user_id = :me AND action = 'like'
              )
              AND p.user_id NOT IN (SELECT user_id FROM banned)
              AND (:my_pref = :any_label OR :my_pref = p.char_gender)
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

    async def find_next_pending_like(self, user_id: int) -> dict[str, Any] | None:
        async with self._conn.execute(
            """
            SELECT p.* FROM profiles p
            JOIN swipes s ON s.from_user_id = p.user_id
            WHERE s.to_user_id = :me
              AND s.action = 'like'
              AND p.user_id NOT IN (
                  SELECT to_user_id FROM swipes WHERE from_user_id = :me
              )
              AND p.user_id NOT IN (SELECT user_id FROM banned)
            ORDER BY s.created_at ASC
            LIMIT 1;
            """,
            {"me": user_id},
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def count_pending_likes(self, user_id: int) -> int:
        async with self._conn.execute(
            """
            SELECT COUNT(*) FROM swipes s
            WHERE s.to_user_id = ?
              AND s.action = 'like'
              AND s.from_user_id NOT IN (
                  SELECT to_user_id FROM swipes WHERE from_user_id = ?
              )
              AND s.from_user_id NOT IN (SELECT user_id FROM banned);
            """,
            (user_id, user_id),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0

    async def record_swipe(self, from_user_id: int, to_user_id: int, action: str) -> None:
        await self._conn.execute(
            """
            INSERT INTO swipes (from_user_id, to_user_id, action)
            VALUES (?, ?, ?)
            ON CONFLICT(from_user_id, to_user_id) DO UPDATE SET
                action     = excluded.action,
                created_at = CURRENT_TIMESTAMP;
            """,
            (from_user_id, to_user_id, action),
        )
        await self._conn.commit()

    async def get_swipe(self, from_user_id: int, to_user_id: int) -> str | None:
        async with self._conn.execute(
            "SELECT action FROM swipes WHERE from_user_id = ? AND to_user_id = ?",
            (from_user_id, to_user_id),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

    async def has_liked_me(self, other_user_id: int, me_user_id: int) -> bool:
        async with self._conn.execute(
            """
            SELECT 1 FROM swipes
            WHERE from_user_id = ? AND to_user_id = ? AND action = 'like'
            LIMIT 1;
            """,
            (other_user_id, me_user_id),
        ) as cur:
            return await cur.fetchone() is not None

    async def list_all_profiles(self) -> list[dict[str, Any]]:
        async with self._conn.execute(
            """
            SELECT p.*, (b.user_id IS NOT NULL) AS banned
            FROM profiles p
            LEFT JOIN banned b ON b.user_id = p.user_id
            ORDER BY p.created_at ASC;
            """,
        ) as cur:
            rows = await cur.fetchall()
            return [dict(row) for row in rows]

    async def find_profile_by_username(self, username: str) -> dict[str, Any] | None:
        async with self._conn.execute(
            "SELECT * FROM profiles WHERE LOWER(username) = LOWER(?)",
            (username,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def delete_profile(self, user_id: int) -> bool:
        cur = await self._conn.execute(
            "DELETE FROM profiles WHERE user_id = ?", (user_id,)
        )
        await self._conn.execute(
            "DELETE FROM swipes WHERE from_user_id = ? OR to_user_id = ?",
            (user_id, user_id),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def ban_user(self, user_id: int, reason: str | None = None) -> None:
        await self._conn.execute(
            """
            INSERT INTO banned (user_id, reason) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET reason = excluded.reason;
            """,
            (user_id, reason),
        )
        await self._conn.commit()

    async def unban_user(self, user_id: int) -> bool:
        cur = await self._conn.execute(
            "DELETE FROM banned WHERE user_id = ?", (user_id,)
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def is_banned(self, user_id: int) -> bool:
        async with self._conn.execute(
            "SELECT 1 FROM banned WHERE user_id = ? LIMIT 1;", (user_id,)
        ) as cur:
            return await cur.fetchone() is not None


# ── PostgreSQL (Neon) backend ────────────────────────────────────────────────

class PostgresDatabase(Database):
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
        async with self._pool.acquire() as conn:
            await conn.execute(POSTGRES_SCHEMA)
        logger.info("PostgreSQL pool ready (Neon)")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database not connected")
        return self._pool

    async def upsert_profile(
        self, user_id: int, char_gender: str, preferred_gender: str,
        username: str, profile_link: str,
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO profiles (user_id, char_gender, preferred_gender, username, profile_link)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT(user_id) DO UPDATE SET
                char_gender      = EXCLUDED.char_gender,
                preferred_gender = EXCLUDED.preferred_gender,
                username         = EXCLUDED.username,
                profile_link     = EXCLUDED.profile_link,
                updated_at       = now();
            """,
            user_id, char_gender, preferred_gender, username, profile_link,
        )

    async def get_profile(self, user_id: int) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM profiles WHERE user_id = $1", user_id,
        )
        return dict(row) if row else None

    async def find_next_candidate(self, user_id: int) -> dict[str, Any] | None:
        me = await self.get_profile(user_id)
        if not me:
            return None
        row = await self.pool.fetchrow(
            """
            SELECT p.* FROM profiles p
            WHERE p.user_id != $1
              AND p.user_id NOT IN (
                  SELECT to_user_id FROM swipes WHERE from_user_id = $1
              )
              AND p.user_id NOT IN (
                  SELECT from_user_id FROM swipes WHERE to_user_id = $1 AND action = 'like'
              )
              AND p.user_id NOT IN (SELECT user_id FROM banned)
              AND ($2 = $4 OR $2 = p.char_gender)
              AND (p.preferred_gender = $4 OR p.preferred_gender = $3)
            ORDER BY RANDOM()
            LIMIT 1;
            """,
            user_id, me["preferred_gender"], me["char_gender"], ANY_LABEL,
        )
        return dict(row) if row else None

    async def find_next_pending_like(self, user_id: int) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            """
            SELECT p.* FROM profiles p
            JOIN swipes s ON s.from_user_id = p.user_id
            WHERE s.to_user_id = $1
              AND s.action = 'like'
              AND p.user_id NOT IN (
                  SELECT to_user_id FROM swipes WHERE from_user_id = $1
              )
              AND p.user_id NOT IN (SELECT user_id FROM banned)
            ORDER BY s.created_at ASC
            LIMIT 1;
            """,
            user_id,
        )
        return dict(row) if row else None

    async def count_pending_likes(self, user_id: int) -> int:
        row = await self.pool.fetchrow(
            """
            SELECT COUNT(*) AS cnt FROM swipes s
            WHERE s.to_user_id = $1
              AND s.action = 'like'
              AND s.from_user_id NOT IN (
                  SELECT to_user_id FROM swipes WHERE from_user_id = $1
              )
              AND s.from_user_id NOT IN (SELECT user_id FROM banned);
            """,
            user_id,
        )
        return row["cnt"] if row else 0

    async def record_swipe(self, from_user_id: int, to_user_id: int, action: str) -> None:
        await self.pool.execute(
            """
            INSERT INTO swipes (from_user_id, to_user_id, action)
            VALUES ($1, $2, $3)
            ON CONFLICT(from_user_id, to_user_id) DO UPDATE SET
                action     = EXCLUDED.action,
                created_at = now();
            """,
            from_user_id, to_user_id, action,
        )

    async def get_swipe(self, from_user_id: int, to_user_id: int) -> str | None:
        row = await self.pool.fetchrow(
            "SELECT action FROM swipes WHERE from_user_id = $1 AND to_user_id = $2",
            from_user_id, to_user_id,
        )
        return row["action"] if row else None

    async def has_liked_me(self, other_user_id: int, me_user_id: int) -> bool:
        row = await self.pool.fetchrow(
            """
            SELECT 1 FROM swipes
            WHERE from_user_id = $1 AND to_user_id = $2 AND action = 'like'
            LIMIT 1;
            """,
            other_user_id, me_user_id,
        )
        return row is not None

    async def list_all_profiles(self) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT p.*, (b.user_id IS NOT NULL) AS banned
            FROM profiles p
            LEFT JOIN banned b ON b.user_id = p.user_id
            ORDER BY p.created_at ASC;
            """,
        )
        return [dict(row) for row in rows]

    async def find_profile_by_username(self, username: str) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM profiles WHERE LOWER(username) = LOWER($1)",
            username,
        )
        return dict(row) if row else None

    async def delete_profile(self, user_id: int) -> bool:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                result = await conn.execute(
                    "DELETE FROM profiles WHERE user_id = $1", user_id,
                )
                await conn.execute(
                    "DELETE FROM swipes WHERE from_user_id = $1 OR to_user_id = $1",
                    user_id,
                )
        # asyncpg returns a status string like "DELETE 1"
        return result.rsplit(" ", 1)[-1] != "0"

    async def ban_user(self, user_id: int, reason: str | None = None) -> None:
        await self.pool.execute(
            """
            INSERT INTO banned (user_id, reason) VALUES ($1, $2)
            ON CONFLICT(user_id) DO UPDATE SET reason = EXCLUDED.reason;
            """,
            user_id, reason,
        )

    async def unban_user(self, user_id: int) -> bool:
        result = await self.pool.execute(
            "DELETE FROM banned WHERE user_id = $1", user_id,
        )
        # asyncpg returns a status string like "DELETE 1"
        return result.rsplit(" ", 1)[-1] != "0"

    async def is_banned(self, user_id: int) -> bool:
        row = await self.pool.fetchrow(
            "SELECT 1 FROM banned WHERE user_id = $1 LIMIT 1;", user_id,
        )
        return row is not None


# ── Factory ──────────────────────────────────────────────────────────────────

_db: Database | None = None


def _is_postgres(url: str) -> bool:
    return url.startswith("postgres://") or url.startswith("postgresql://")


def create_database() -> Database:
    if _is_postgres(DATABASE_URL):
        return PostgresDatabase(DATABASE_URL)
    return SqliteDatabase(DB_PATH)


async def get_db() -> Database:
    """Return the singleton database instance, creating it on first call."""
    global _db  # noqa: PLW0603
    if _db is None:
        _db = create_database()
        await _db.connect()
    return _db


# ── Legacy module-level helpers (used by bot.py) ─────────────────────────────

async def init_db() -> None:
    await get_db()


async def upsert_profile(
    user_id: int, char_gender: str, preferred_gender: str,
    username: str, profile_link: str,
) -> None:
    db = await get_db()
    await db.upsert_profile(user_id, char_gender, preferred_gender, username, profile_link)


async def get_profile(user_id: int) -> dict[str, Any] | None:
    db = await get_db()
    return await db.get_profile(user_id)


async def find_next_candidate(user_id: int) -> dict[str, Any] | None:
    db = await get_db()
    return await db.find_next_candidate(user_id)


async def find_next_pending_like(user_id: int) -> dict[str, Any] | None:
    db = await get_db()
    return await db.find_next_pending_like(user_id)


async def count_pending_likes(user_id: int) -> int:
    db = await get_db()
    return await db.count_pending_likes(user_id)


async def record_swipe(from_user_id: int, to_user_id: int, action: str) -> None:
    db = await get_db()
    await db.record_swipe(from_user_id, to_user_id, action)


async def get_swipe(from_user_id: int, to_user_id: int) -> str | None:
    db = await get_db()
    return await db.get_swipe(from_user_id, to_user_id)


async def has_liked_me(other_user_id: int, me_user_id: int) -> bool:
    db = await get_db()
    return await db.has_liked_me(other_user_id, me_user_id)


async def list_all_profiles() -> list[dict[str, Any]]:
    db = await get_db()
    return await db.list_all_profiles()


async def find_profile_by_username(username: str) -> dict[str, Any] | None:
    db = await get_db()
    return await db.find_profile_by_username(username)


async def delete_profile(user_id: int) -> bool:
    db = await get_db()
    return await db.delete_profile(user_id)


async def ban_user(user_id: int, reason: str | None = None) -> None:
    db = await get_db()
    await db.ban_user(user_id, reason)


async def unban_user(user_id: int) -> bool:
    db = await get_db()
    return await db.unban_user(user_id)


async def is_banned(user_id: int) -> bool:
    db = await get_db()
    return await db.is_banned(user_id)


async def close_db() -> None:
    global _db  # noqa: PLW0603
    if _db is not None:
        await _db.close()
        _db = None
