"""
Persistencia del historial de mensajes del canal #lobby en SQLite.
"""
import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent.parent / "orquestador.db"


@dataclass
class Message:
    id: int
    timestamp: str
    author_kind: str
    author_name: str
    author_id: str
    content: str


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                author_kind TEXT NOT NULL,
                author_name TEXT NOT NULL,
                author_id TEXT NOT NULL,
                content TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_id ON messages(id DESC)"
        )
    logger.info(f"BD inicializada en {DB_PATH}")


def save_message(
    author_kind: str,
    author_name: str,
    author_id: str,
    content: str,
) -> int:
    assert author_kind in ("human", "agent"), f"author_kind inválido: {author_kind}"
    ts = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO messages (timestamp, author_kind, author_name, author_id, content)
            VALUES (?, ?, ?, ?, ?)
            """,
            (ts, author_kind, author_name, author_id, content),
        )
        return cur.lastrowid


def get_recent_messages(limit: int = 20) -> list[Message]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, timestamp, author_kind, author_name, author_id, content
            FROM messages
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    msgs = [Message(**dict(row)) for row in rows]
    msgs.reverse()
    return msgs


def format_context(messages: list[Message]) -> str:
    lines = []
    for m in messages:
        lines.append(f"[{m.author_name}]: {m.content}")
    return "\n".join(lines)


def clear_all() -> int:
    """Borra todos los mensajes de la BD. Devuelve el nº de filas borradas."""
    with _connect() as conn:
        cur = conn.execute("DELETE FROM messages")
        deleted = cur.rowcount
        conn.execute("DELETE FROM sqlite_sequence WHERE name='messages'")
    logger.info(f"BD limpiada: {deleted} mensajes borrados")
    return deleted


def count_messages() -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()
    return int(row["n"]) if row else 0
