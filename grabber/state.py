import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    source  TEXT NOT NULL,
    item_id TEXT NOT NULL,
    url     TEXT,
    status  TEXT NOT NULL,  -- seen | rejected | drafted
    score   INTEGER,
    ts      TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (source, item_id)
);
"""


class State:
    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute(SCHEMA)
        self.conn.commit()

    def is_known(self, source: str, item_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM items WHERE source = ? AND item_id = ?", (source, item_id)
        ).fetchone()
        return row is not None

    def mark(self, source: str, item_id: str, url: str, status: str, score: int | None = None):
        self.conn.execute(
            "INSERT INTO items (source, item_id, url, status, score) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (source, item_id) DO UPDATE SET status = excluded.status, score = excluded.score",
            (source, item_id, url, status, score),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()
