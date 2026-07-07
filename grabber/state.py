import sqlite3
from pathlib import Path

# base table; the norm_url/simhash/primary_ref columns and their indexes are added by
# _migrate() so an existing pre-dedup DB upgrades in place
SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    source   TEXT NOT NULL,
    item_id  TEXT NOT NULL,
    url      TEXT,
    status   TEXT NOT NULL,  -- seen | filtered | rejected | drafted | duplicate
    score    INTEGER,
    ts       TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (source, item_id)
);
-- extracted full-page article text, keyed by normalized URL so items sharing a URL reuse
-- one fetch and a failed-then-retried draft doesn't re-fetch next run
CREATE TABLE IF NOT EXISTS content (
    norm_url TEXT PRIMARY KEY,
    content  TEXT NOT NULL,
    ts       TEXT DEFAULT (datetime('now'))
);
"""

# columns added after the original release; ADD COLUMN isn't idempotent so we guard on PRAGMA
_ADDED_COLUMNS = {
    "norm_url": "TEXT",
    "simhash": "INTEGER",
    "primary_ref": "TEXT",
}


def _to_signed(u: int | None) -> int | None:
    """SQLite INTEGER is signed 64-bit; simhashes are unsigned 64-bit. Reinterpret the bit
    pattern as signed for storage without losing any bits."""
    if not u:
        return None
    return u - (1 << 64) if u >= (1 << 63) else u


def _to_unsigned(s: int | None) -> int:
    """Inverse of _to_signed; 0 means 'no simhash'."""
    if not s:
        return 0
    return s + (1 << 64) if s < 0 else s


class State:
    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(db_path)
        self.conn.executescript(SCHEMA)  # SCHEMA holds multiple statements
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(items)")}
        for col, coltype in _ADDED_COLUMNS.items():
            if col not in existing:
                self.conn.execute(f"ALTER TABLE items ADD COLUMN {col} {coltype}")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_items_norm_url ON items(norm_url)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_items_ts ON items(ts)")

    def is_known(self, source: str, item_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM items WHERE source = ? AND item_id = ?", (source, item_id)
        ).fetchone()
        return row is not None

    def mark(
        self,
        source: str,
        item_id: str,
        url: str,
        status: str,
        score: int | None = None,
        norm_url: str | None = None,
        simhash: int | None = None,
        primary_ref: str | None = None,
    ):
        self.conn.execute(
            "INSERT INTO items (source, item_id, url, status, score, norm_url, simhash, primary_ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (source, item_id) DO UPDATE SET "
            "status = excluded.status, score = excluded.score, norm_url = excluded.norm_url, "
            "simhash = excluded.simhash, primary_ref = excluded.primary_ref",
            (source, item_id, url, status, score, norm_url, _to_signed(simhash), primary_ref),
        )
        self.conn.commit()

    def recent_content_keys(self, cutoff: str) -> list[tuple]:
        """Content keys of items classified since `cutoff` (an ISO string), for cross-run
        dedup. Includes rejected items too, so a later reshare of a rejected story is
        suppressed without re-classifying it. Small volume — the simhash Hamming compare
        is an in-memory scan; norm_url matching is index-backed by the caller if needed."""
        rows = self.conn.execute(
            "SELECT source, item_id, url, status, norm_url, simhash FROM items "
            "WHERE ts >= ? AND (simhash IS NOT NULL OR (norm_url IS NOT NULL AND norm_url != ''))",
            (cutoff,),
        ).fetchall()
        # hand back unsigned simhashes so callers can hamming() them directly
        return [(s, i, u, st, nu, _to_unsigned(sh)) for (s, i, u, st, nu, sh) in rows]

    def get_content(self, norm_url: str) -> str | None:
        """Cached extracted article text for `norm_url`, or None if not fetched yet."""
        if not norm_url:
            return None
        row = self.conn.execute(
            "SELECT content FROM content WHERE norm_url = ?", (norm_url,)
        ).fetchone()
        return row[0] if row else None

    def set_content(self, norm_url: str, content: str):
        """Cache extracted article text keyed by normalized URL (no-op without a key)."""
        if not norm_url or not content:
            return
        self.conn.execute(
            "INSERT INTO content (norm_url, content) VALUES (?, ?) "
            "ON CONFLICT (norm_url) DO UPDATE SET content = excluded.content, ts = datetime('now')",
            (norm_url, content),
        )
        self.conn.commit()

    def prune(self, cutoff: str):
        """Drop rows older than `cutoff` to bound the in-memory dedup scan. Callers pass a
        conservative cutoff (older than the fetch lookback) so is_known still suppresses
        old-but-still-listed feed entries. Stale cached article text is dropped on the same
        cutoff."""
        self.conn.execute("DELETE FROM items WHERE ts < ?", (cutoff,))
        self.conn.execute("DELETE FROM content WHERE ts < ?", (cutoff,))
        self.conn.commit()

    def close(self):
        self.conn.close()
