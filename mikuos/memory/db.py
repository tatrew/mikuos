"""SQLite storage: connection, versioned migrations, FTS5 indexes, backups."""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("mikuos.db")

# Each migration runs once, in order, inside a transaction. Never edit an applied
# migration; append a new one instead.
MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "core tables",
        """
        CREATE TABLE conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            summary TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK (role IN ('system','user','assistant','tool','event')),
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX idx_messages_conv ON messages(conversation_id, id);
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_type TEXT NOT NULL CHECK (memory_type IN ('episodic','semantic','activity')),
            content TEXT NOT NULL,
            importance REAL NOT NULL DEFAULT 0.5,
            confidence REAL NOT NULL DEFAULT 0.8,
            source_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_accessed_at TEXT,
            is_active INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX idx_memories_type ON memories(memory_type, is_active);
        CREATE TABLE character_lore (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            character TEXT NOT NULL,
            fact TEXT NOT NULL,
            source_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(character, fact)
        );
        CREATE INDEX idx_lore_character ON character_lore(character);
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            event_data TEXT,
            timestamp TEXT NOT NULL
        );
        CREATE INDEX idx_events_type_ts ON events(event_type, timestamp);
        CREATE TABLE tool_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tool_name TEXT NOT NULL,
            arguments_json TEXT,
            result_json TEXT,
            execution_status TEXT NOT NULL,
            timestamp TEXT NOT NULL
        );
        CREATE INDEX idx_tool_audit_ts ON tool_audit(timestamp);
        """,
    ),
    (
        2,
        "fts5 indexes",
        """
        CREATE VIRTUAL TABLE memories_fts USING fts5(content, content='memories', content_rowid='id',
            tokenize='unicode61 remove_diacritics 2');
        CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
        END;
        CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
        END;
        CREATE TRIGGER memories_au AFTER UPDATE OF content ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
            INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
        END;

        CREATE VIRTUAL TABLE messages_fts USING fts5(content, content='messages', content_rowid='id',
            tokenize='unicode61 remove_diacritics 2');
        CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
        END;
        CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
        END;

        CREATE VIRTUAL TABLE lore_fts USING fts5(fact, content='character_lore', content_rowid='id',
            tokenize='unicode61 remove_diacritics 2');
        CREATE TRIGGER lore_ai AFTER INSERT ON character_lore BEGIN
            INSERT INTO lore_fts(rowid, fact) VALUES (new.id, new.fact);
        END;
        CREATE TRIGGER lore_ad AFTER DELETE ON character_lore BEGIN
            INSERT INTO lore_fts(lore_fts, rowid, fact) VALUES ('delete', old.id, old.fact);
        END;
        """,
    ),
    (
        3,
        "memory keys, sensitivity queue, message metadata",
        """
        ALTER TABLE memories ADD COLUMN fact_key TEXT;
        ALTER TABLE memories ADD COLUMN superseded_by INTEGER REFERENCES memories(id) ON DELETE SET NULL;
        CREATE INDEX idx_memories_key ON memories(fact_key, is_active);
        ALTER TABLE messages ADD COLUMN meta TEXT;
        CREATE TABLE pending_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_type TEXT NOT NULL,
            content TEXT NOT NULL,
            fact_key TEXT,
            importance REAL NOT NULL DEFAULT 0.5,
            reason TEXT,
            source_message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE embeddings (
            memory_id INTEGER PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
            model TEXT NOT NULL,
            vector BLOB NOT NULL
        );
        """,
    ),
]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Database:
    """One shared connection guarded by a lock (SQLite + small local load)."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if str(path) != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.migrate()

    # ----------------------------------------------------------- migrations
    def schema_version(self) -> int:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)"
        )
        row = self.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return row[0] or 0

    def migrate(self) -> int:
        with self.lock:
            current = self.schema_version()
            for version, name, sql in MIGRATIONS:
                if version <= current:
                    continue
                log.info("applying migration %d: %s", version, name)
                self.conn.execute("BEGIN")
                try:
                    for stmt in _split_sql(sql):
                        self.conn.execute(stmt)
                    self.conn.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?,?,?)",
                        (version, name, now_iso()),
                    )
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise
            return self.schema_version()

    # ---------------------------------------------------------------- helpers
    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def transaction(self):
        return _Tx(self)

    def integrity_ok(self) -> bool:
        return self.one("PRAGMA integrity_check")[0] == "ok"

    def backup(self, backup_dir: Path | str, keep: int = 10) -> Path | None:
        """Online backup via the sqlite backup API; prunes old copies."""
        if str(self.path) == ":memory:":
            return None
        backup_dir = Path(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        target = backup_dir / f"mikuos-{time.strftime('%Y%m%d-%H%M%S')}.db"
        n = 1
        while target.exists():
            target = backup_dir / f"mikuos-{time.strftime('%Y%m%d-%H%M%S')}-{n}.db"
            n += 1
        with self.lock:
            dst = sqlite3.connect(str(target))
            try:
                self.conn.backup(dst)
            finally:
                dst.close()
        backups = sorted(backup_dir.glob("mikuos-*.db"), key=lambda p: p.stat().st_mtime)
        for old in backups[:-keep] if keep > 0 else []:
            try:
                old.unlink()
            except OSError:
                pass
        return target

    def close(self) -> None:
        with self.lock:
            self.conn.close()


class _Tx:
    def __init__(self, db: Database):
        self.db = db

    def __enter__(self):
        self.db.lock.acquire()
        self.db.conn.execute("BEGIN")
        return self.db.conn

    def __exit__(self, exc_type, exc, tb):
        try:
            self.db.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.db.lock.release()
        return False


def _split_sql(sql: str) -> list[str]:
    """Split a migration script on ';' while keeping trigger bodies intact."""
    out, buf, depth = [], [], 0
    for line in sql.splitlines():
        stripped = line.strip().upper()
        if stripped.startswith("CREATE TRIGGER"):
            depth += 1
        buf.append(line)
        if stripped.endswith("END;") and depth:
            depth -= 1
            out.append("\n".join(buf).strip().rstrip(";"))
            buf = []
        elif stripped.endswith(";") and not depth:
            out.append("\n".join(buf).strip().rstrip(";"))
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        out.append(tail)
    return [s for s in out if s]
