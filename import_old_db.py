"""Optional one-time import from the earlier local support.db into Neon.

Usage (after configuring DATABASE_URL in the environment):
    python import_old_db.py path/to/support.db

The SQLite source file is read-only and never modified.
"""

import asyncio
import os
import sqlite3
import ssl
import sys
from pathlib import Path

import asyncpg
from bot import clean_database_url


def table_present(db: sqlite3.Connection, table: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


async def main() -> None:
    if len(sys.argv) != 2 or not Path(sys.argv[1]).is_file():
        raise SystemExit("Укажи путь к существующему файлу support.db")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("Сначала укажи переменную DATABASE_URL (строка подключения Neon)")

    old = sqlite3.connect(Path(sys.argv[1]).resolve().as_uri() + "?mode=ro", uri=True)
    old.row_factory = sqlite3.Row
    conn = await asyncpg.connect(
        dsn=clean_database_url(database_url), ssl=ssl.create_default_context()
    )
    try:
        # Tables need to exist first. Deploy the bot once before import.
        check = await conn.fetchval("SELECT to_regclass('public.tickets')")
        if check is None:
            raise RuntimeError("Сначала запусти бота на Render, чтобы он создал таблицы")
        async with conn.transaction():
            count_tickets = 0
            count_links = 0
            if table_present(old, "tickets"):
                for row in old.execute("SELECT * FROM tickets"):
                    await conn.execute("""
                        INSERT INTO tickets (
                            id, player_id, category, original_text, status,
                            player_message_id, team_header_id, created_at
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::timestamptz)
                        ON CONFLICT DO NOTHING
                    """, row["ticket_id"], row["player_id"], row["category"],
                         row["original_text"], row["status"],
                         row["player_message_id"], row["team_header_id"],
                         row["created_at"])
                    count_tickets += 1
            await conn.execute("""
                SELECT setval(
                    pg_get_serial_sequence('tickets', 'id'),
                    GREATEST(COALESCE((SELECT MAX(id) FROM tickets), 1), 1),
                    EXISTS (SELECT 1 FROM tickets)
                )
            """)
            if table_present(old, "pending"):
                for row in old.execute("SELECT * FROM pending"):
                    await conn.execute("""
                        INSERT INTO drafts (player_id, category)
                        VALUES ($1, $2)
                        ON CONFLICT (player_id) DO NOTHING
                    """, row["player_id"], row["category"])
            if table_present(old, "message_links"):
                names = {item[1] for item in old.execute("PRAGMA table_info(message_links)")}
                if "ticket_id" in names:
                    for row in old.execute(
                        "SELECT team_message_id, ticket_id FROM message_links WHERE ticket_id IS NOT NULL"
                    ):
                        exists = await conn.fetchval(
                            "SELECT 1 FROM tickets WHERE id = $1", row["ticket_id"]
                        )
                        if exists:
                            await conn.execute("""
                                INSERT INTO message_links (team_message_id, ticket_id)
                                VALUES ($1, $2) ON CONFLICT DO NOTHING
                            """, row["team_message_id"], row["ticket_id"])
                            count_links += 1
        print(f"Готово. Обработано старых обращений: {count_tickets}; связей: {count_links}.")
        print("Файл support.db не изменён. Повторный запуск не создаст дубликаты.")
    finally:
        await conn.close()
        old.close()


if __name__ == "__main__":
    asyncio.run(main())
