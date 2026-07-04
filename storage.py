import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.getenv("DB_PATH", "starters_bot.db")


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS posted_games (
                game_pk INTEGER PRIMARY KEY,
                posted_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)


def is_game_posted(game_pk: int) -> bool:
    with _conn() as c:
        return c.execute(
            "SELECT 1 FROM posted_games WHERE game_pk = ?", (game_pk,)
        ).fetchone() is not None


def mark_game_posted(game_pk: int):
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO posted_games (game_pk) VALUES (?)", (game_pk,))


def set_config(key: str, value: str):
    with _conn() as c:
        c.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_config(key: str):
    with _conn() as c:
        row = c.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None
