"""Memory store: conversations, messages, long-term memories, lore, events, audit.

Character lore lives in its own table and is never mixed with facts about the user.
Search uses FTS5 with light prefix-stemming (Russian morphology is rich, so we
match on word stems). `EmbeddingProvider` is a hook for future vector search; no
embedding model is installed or required.
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Protocol

from .db import Database, now_iso

MEMORY_TYPES = ("episodic", "semantic", "activity")
_WORD = re.compile(r"[\w\-]+", re.UNICODE)
_VOWELS = set("аеёиоуыэюяy")
_STOP = {
    # tiny stop list: words too common to help retrieval
    "это", "как", "что", "мне", "меня", "мой", "моя", "мое", "моё", "мои", "тебя", "тебе", "ты", "я", "он", "она",
    "они", "мы", "вы", "the", "and", "for", "you", "ещё", "еще", "для", "так", "вот", "там", "тут", "был", "была",
    "есть", "нет", "да", "привет", "пожалуйста", "помнишь", "скажи", "знаешь", "про", "или", "если", "его", "её",
    "кто", "где", "когда", "какой", "какая", "какое", "какие", "зовут",
}


class EmbeddingProvider(Protocol):
    """Interface for an optional embedding backend (e.g. LM Studio /v1/embeddings)."""

    model: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass
class Memory:
    id: int
    memory_type: str
    content: str
    importance: float
    confidence: float
    source_message_id: int | None
    created_at: str
    updated_at: str
    last_accessed_at: str | None
    is_active: bool
    fact_key: str | None = None
    superseded_by: int | None = None

    @classmethod
    def from_row(cls, r) -> "Memory":
        return cls(
            id=r["id"], memory_type=r["memory_type"], content=r["content"], importance=r["importance"],
            confidence=r["confidence"], source_message_id=r["source_message_id"], created_at=r["created_at"],
            updated_at=r["updated_at"], last_accessed_at=r["last_accessed_at"], is_active=bool(r["is_active"]),
            fact_key=r["fact_key"], superseded_by=r["superseded_by"],
        )

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def normalize(text: str) -> str:
    return " ".join(w.lower() for w in _WORD.findall(text))


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def fts_query(text: str, max_terms: int = 12) -> str | None:
    """Build an OR-ed FTS5 prefix query from free text; None if nothing useful."""
    terms: list[str] = []
    for w in _WORD.findall(text.lower()):
        if len(w) < 3 or w in _STOP or w.isdigit() and len(w) < 2:
            continue
        # crude stemming for Russian endings: кошка/кошку/кошкой -> кошк*, Алексей -> алекс*
        if len(w) > 4:
            stem = w[:max(4, len(w) - 2)]
        elif len(w) == 4 and w[-1] in _VOWELS:
            stem = w[:3]  # кота / коту -> кот*
        else:
            stem = w
        stem = stem.replace('"', "")
        if stem and stem not in terms:
            terms.append(stem)
        if len(terms) >= max_terms:
            break
    if not terms:
        return None
    return " OR ".join(f'"{t}"*' for t in terms)


class MemoryStore:
    def __init__(self, db: Database):
        self.db = db

    # ---------------------------------------------------------- conversations
    def start_conversation(self) -> int:
        cur = self.db.execute("INSERT INTO conversations(started_at) VALUES (?)", (now_iso(),))
        return cur.lastrowid

    def end_conversation(self, conversation_id: int, summary: str | None = None) -> None:
        self.db.execute(
            "UPDATE conversations SET ended_at = COALESCE(ended_at, ?), summary = COALESCE(?, summary) WHERE id = ?",
            (now_iso(), summary, conversation_id),
        )

    def open_conversations(self) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT * FROM conversations WHERE ended_at IS NULL ORDER BY id")]

    def get_conversation(self, conversation_id: int) -> dict | None:
        r = self.db.one("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        return dict(r) if r else None

    def list_conversations(self, limit: int = 50) -> list[dict]:
        rows = self.db.query(
            "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count "
            "FROM conversations c ORDER BY c.id DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def last_activity(self, conversation_id: int) -> datetime | None:
        r = self.db.one("SELECT MAX(created_at) FROM messages WHERE conversation_id = ?", (conversation_id,))
        if r and r[0]:
            return datetime.fromisoformat(r[0])
        c = self.get_conversation(conversation_id)
        return datetime.fromisoformat(c["started_at"]) if c else None

    # --------------------------------------------------------------- messages
    def add_message(self, conversation_id: int, role: str, content: str, meta: dict | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO messages(conversation_id, role, content, created_at, meta) VALUES (?,?,?,?,?)",
            (conversation_id, role, content, now_iso(), json.dumps(meta, ensure_ascii=False) if meta else None),
        )
        return cur.lastrowid

    def recent_messages(self, conversation_id: int, limit: int = 20) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM (SELECT * FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id",
            (conversation_id, limit),
        )
        return [dict(r) for r in rows]

    def messages(self, conversation_id: int) -> list[dict]:
        return [dict(r) for r in self.db.query(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id", (conversation_id,))]

    def count_messages(self, conversation_id: int, role: str | None = None) -> int:
        if role:
            return self.db.one(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND role = ?", (conversation_id, role))[0]
        return self.db.one("SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (conversation_id,))[0]

    def search_messages(self, text: str, limit: int = 5, exclude_conversation: int | None = None) -> list[dict]:
        q = fts_query(text)
        if not q:
            return []
        rows = self.db.query(
            "SELECT m.* FROM messages_fts f JOIN messages m ON m.id = f.rowid "
            "WHERE messages_fts MATCH ? AND m.role IN ('user','assistant') AND (? IS NULL OR m.conversation_id != ?) "
            "ORDER BY bm25(messages_fts) LIMIT ?",
            (q, exclude_conversation, exclude_conversation, limit),
        )
        return [dict(r) for r in rows]

    # --------------------------------------------------------------- memories
    def add_memory(
        self,
        content: str,
        memory_type: str = "semantic",
        importance: float = 0.5,
        confidence: float = 0.8,
        source_message_id: int | None = None,
        fact_key: str | None = None,
    ) -> int:
        if memory_type not in MEMORY_TYPES:
            raise ValueError(f"memory_type must be one of {MEMORY_TYPES}")
        content = content.strip()
        if not content:
            raise ValueError("empty memory")
        ts = now_iso()
        cur = self.db.execute(
            "INSERT INTO memories(memory_type, content, importance, confidence, source_message_id, created_at, "
            "updated_at, is_active, fact_key) VALUES (?,?,?,?,?,?,?,1,?)",
            (memory_type, content, _clamp(importance), _clamp(confidence), source_message_id, ts, ts, fact_key),
        )
        return cur.lastrowid

    def get_memory(self, memory_id: int) -> Memory | None:
        r = self.db.one("SELECT * FROM memories WHERE id = ?", (memory_id,))
        return Memory.from_row(r) if r else None

    def list_memories(self, memory_type: str | None = None, include_inactive: bool = False, limit: int = 200) -> list[Memory]:
        sql = "SELECT * FROM memories WHERE 1=1"
        params: list[Any] = []
        if memory_type:
            sql += " AND memory_type = ?"
            params.append(memory_type)
        if not include_inactive:
            sql += " AND is_active = 1"
        sql += " ORDER BY importance DESC, updated_at DESC LIMIT ?"
        params.append(limit)
        return [Memory.from_row(r) for r in self.db.query(sql, tuple(params))]

    def update_memory(self, memory_id: int, **fields) -> Memory:
        allowed = {"content", "importance", "confidence", "is_active", "memory_type", "fact_key"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot update fields {sorted(bad)}")
        if "memory_type" in fields and fields["memory_type"] not in MEMORY_TYPES:
            raise ValueError("bad memory_type")
        if not fields:
            m = self.get_memory(memory_id)
            if not m:
                raise KeyError(memory_id)
            return m
        sets = ", ".join(f"{k} = ?" for k in fields) + ", updated_at = ?"
        params = [int(v) if k == "is_active" else v for k, v in fields.items()] + [now_iso(), memory_id]
        cur = self.db.execute(f"UPDATE memories SET {sets} WHERE id = ?", tuple(params))
        if cur.rowcount == 0:
            raise KeyError(memory_id)
        return self.get_memory(memory_id)

    def deactivate_memory(self, memory_id: int, superseded_by: int | None = None) -> None:
        self.db.execute(
            "UPDATE memories SET is_active = 0, superseded_by = ?, updated_at = ? WHERE id = ?",
            (superseded_by, now_iso(), memory_id),
        )

    def delete_memory(self, memory_id: int) -> bool:
        return self.db.execute("DELETE FROM memories WHERE id = ?", (memory_id,)).rowcount > 0

    def touch(self, ids: Iterable[int]) -> None:
        ids = list(ids)
        if ids:
            ts = now_iso()
            self.db.execute(
                f"UPDATE memories SET last_accessed_at = ? WHERE id IN ({','.join('?' * len(ids))})", (ts, *ids))

    def search_memories(
        self, text: str, limit: int = 8, types: Iterable[str] | None = None, touch: bool = True
    ) -> list[Memory]:
        q = fts_query(text)
        if not q:
            return []
        types = list(types or MEMORY_TYPES)
        rows = self.db.query(
            "SELECT m.*, bm25(memories_fts) AS rank FROM memories_fts f JOIN memories m ON m.id = f.rowid "
            f"WHERE memories_fts MATCH ? AND m.is_active = 1 AND m.memory_type IN ({','.join('?' * len(types))}) "
            "ORDER BY rank - m.importance * 2 LIMIT ?",
            (q, *types, limit),
        )
        out = [Memory.from_row(r) for r in rows]
        if touch:
            self.touch(m.id for m in out)
        return out

    def find_duplicate(self, content: str, memory_type: str, threshold: float = 0.85) -> Memory | None:
        candidates = self.search_memories(content, limit=10, types=[memory_type], touch=False)
        best, best_score = None, 0.0
        for m in candidates:
            s = similarity(content, m.content)
            if s > best_score:
                best, best_score = m, s
        return best if best and best_score >= threshold else None

    def active_by_key(self, fact_key: str) -> list[Memory]:
        return [Memory.from_row(r) for r in self.db.query(
            "SELECT * FROM memories WHERE fact_key = ? AND is_active = 1 ORDER BY id", (fact_key,))]

    def top_memories(self, memory_type: str, limit: int = 5) -> list[Memory]:
        return [Memory.from_row(r) for r in self.db.query(
            "SELECT * FROM memories WHERE memory_type = ? AND is_active = 1 "
            "ORDER BY importance DESC, updated_at DESC LIMIT ?", (memory_type, limit))]

    # -------------------------------------------------------- pending (sensitive)
    def add_pending(self, content: str, memory_type: str, reason: str, importance: float = 0.5,
                    fact_key: str | None = None, source_message_id: int | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO pending_memories(memory_type, content, fact_key, importance, reason, source_message_id, "
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (memory_type, content, fact_key, importance, reason, source_message_id, now_iso()),
        )
        return cur.lastrowid

    def list_pending(self) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT * FROM pending_memories ORDER BY id")]

    def resolve_pending(self, pending_id: int, approve: bool) -> int | None:
        r = self.db.one("SELECT * FROM pending_memories WHERE id = ?", (pending_id,))
        if not r:
            raise KeyError(pending_id)
        new_id = None
        if approve:
            new_id = self.add_memory(r["content"], r["memory_type"], r["importance"], 0.9,
                                     r["source_message_id"], r["fact_key"])
        self.db.execute("DELETE FROM pending_memories WHERE id = ?", (pending_id,))
        return new_id

    # ------------------------------------------------------------------- lore
    def add_lore(self, character: str, fact: str, source_type: str = "mikuos_authorial") -> int | None:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO character_lore(character, fact, source_type, created_at) VALUES (?,?,?,?)",
            (character, fact, source_type, now_iso()),
        )
        return cur.lastrowid if cur.rowcount else None

    def seed_lore(self, path: Path) -> int:
        if not path.exists():
            return 0
        added = 0
        for item in json.loads(path.read_text(encoding="utf-8")):
            if self.add_lore(item["character"], item["fact"], item.get("source_type", "mikuos_authorial")):
                added += 1
        return added

    def list_lore(self, character: str | None = None) -> list[dict]:
        if character:
            rows = self.db.query("SELECT * FROM character_lore WHERE character = ? ORDER BY id", (character,))
        else:
            rows = self.db.query("SELECT * FROM character_lore ORDER BY id")
        return [dict(r) for r in rows]

    def search_lore(self, text: str, limit: int = 3) -> list[dict]:
        q = fts_query(text)
        if not q:
            return []
        rows = self.db.query(
            "SELECT l.* FROM lore_fts f JOIN character_lore l ON l.id = f.rowid WHERE lore_fts MATCH ? "
            "ORDER BY bm25(lore_fts) LIMIT ?", (q, limit))
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------- events
    def add_event(self, event_type: str, data: dict | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO events(event_type, event_data, timestamp) VALUES (?,?,?)",
            (event_type, json.dumps(data or {}, ensure_ascii=False), now_iso()),
        )
        return cur.lastrowid

    def recent_events(self, limit: int = 20, event_type: str | None = None) -> list[dict]:
        if event_type:
            rows = self.db.query(
                "SELECT * FROM events WHERE event_type = ? ORDER BY id DESC LIMIT ?", (event_type, limit))
        else:
            rows = self.db.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        return [{**dict(r), "event_data": json.loads(r["event_data"] or "{}")} for r in rows]

    def count_events_since(self, event_type: str, since: datetime) -> int:
        return self.db.one(
            "SELECT COUNT(*) FROM events WHERE event_type = ? AND timestamp >= ?",
            (event_type, since.astimezone().isoformat(timespec="seconds")),
        )[0]

    def prune_events(self, older_than_days: int = 30) -> int:
        cutoff = (datetime.now().astimezone() - timedelta(days=older_than_days)).isoformat(timespec="seconds")
        return self.db.execute("DELETE FROM events WHERE timestamp < ?", (cutoff,)).rowcount

    # ------------------------------------------------------------------ audit
    def audit(self, tool_name: str, arguments: Any, result: Any, status: str) -> int:
        cur = self.db.execute(
            "INSERT INTO tool_audit(tool_name, arguments_json, result_json, execution_status, timestamp) "
            "VALUES (?,?,?,?,?)",
            (tool_name, json.dumps(arguments, ensure_ascii=False, default=str),
             json.dumps(result, ensure_ascii=False, default=str)[:20000], status, now_iso()),
        )
        return cur.lastrowid

    def recent_audit(self, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT * FROM tool_audit ORDER BY id DESC LIMIT ?", (limit,))]


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, float(x)))
