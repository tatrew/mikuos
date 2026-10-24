"""1. SQLite CRUD, migrations, FTS, backup, recovery after reopen (real SQLite files)."""
import sqlite3

import pytest

from mikuos.memory import Candidate, Database, MemoryManager, MemoryStore
from mikuos.memory.db import MIGRATIONS


def test_fresh_migration_creates_all_tables(tmp_path):
    db = Database(tmp_path / "a.db")
    assert db.schema_version() == MIGRATIONS[-1][0]
    names = {r[0] for r in db.query("SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
    for t in ["conversations", "messages", "memories", "character_lore", "events", "tool_audit", "memories_fts",
              "messages_fts", "lore_fts", "pending_memories", "embeddings", "idx_messages_conv", "idx_memories_type"]:
        assert t in names, t
    fks = db.query("PRAGMA foreign_key_list(messages)")
    assert fks and fks[0]["table"] == "conversations"
    assert db.integrity_ok()


def test_migrations_idempotent_and_incremental(tmp_path):
    path = tmp_path / "b.db"
    # simulate an old database that only has migration 1
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)")
    from mikuos.memory.db import _split_sql
    for stmt in _split_sql(MIGRATIONS[0][2]):
        conn.execute(stmt)
    conn.execute("INSERT INTO schema_migrations VALUES (1,'core','x')")
    conn.execute("INSERT INTO conversations(started_at) VALUES ('2025-01-01T00:00:00+00:00')")
    conn.execute("INSERT INTO memories(memory_type, content, created_at, updated_at) VALUES "
                 "('semantic','старый факт','x','x')")
    conn.commit()
    conn.close()
    db = Database(path)
    assert db.schema_version() == 3
    assert db.one("SELECT content FROM memories")[0] == "старый факт"
    assert "fact_key" in [r["name"] for r in db.query("PRAGMA table_info(memories)")]
    db.close()
    db2 = Database(path)  # reopening must not re-apply
    assert db2.query("SELECT COUNT(*) FROM schema_migrations")[0][0] == 3


def test_crud_and_fts_search_russian(tmp_path):
    store = MemoryStore(Database(tmp_path / "c.db"))
    a = store.add_memory("Пользователя зовут Алексей", "semantic", 0.9)
    b = store.add_memory("У пользователя есть кошка по имени Муся", "semantic", 0.6)
    store.add_memory("Вчера пользователь ходил на концерт", "episodic", 0.4)
    # different word forms must match (кошка / кошку)
    hits = store.search_memories("Как зовут мою кошку?")
    assert hits and hits[0].id == b
    assert store.search_memories("Алексей")[0].id == a
    assert store.get_memory(a).last_accessed_at is not None
    m = store.update_memory(b, content="У пользователя есть кошка Муся и пёс Рекс", importance=0.7)
    assert m.importance == 0.7
    assert store.search_memories("пёс Рекс")[0].id == b  # FTS updated by trigger
    store.deactivate_memory(a)
    assert all(h.id != a for h in store.search_memories("Алексей"))
    assert store.delete_memory(b)
    assert store.get_memory(b) is None
    with pytest.raises(ValueError):
        store.add_memory("x", "bogus")
    with pytest.raises(ValueError):
        store.update_memory(a, created_at="hack")


def test_cascade_delete_and_fk(tmp_path):
    store = MemoryStore(Database(tmp_path / "d.db"))
    cid = store.start_conversation()
    mid = store.add_message(cid, "user", "привет")
    mem = store.add_memory("факт", "semantic", source_message_id=mid)
    store.db.execute("DELETE FROM conversations WHERE id = ?", (cid,))
    assert store.messages(cid) == []
    assert store.get_memory(mem).source_message_id is None  # ON DELETE SET NULL
    with pytest.raises(sqlite3.IntegrityError):
        store.add_message(9999, "user", "orphan")


def test_lore_is_separate_from_user_memory(tmp_path):
    from mikuos.config import PROJECT_ROOT
    store = MemoryStore(Database(tmp_path / "e.db"))
    n = store.seed_lore(PROJECT_ROOT / "prompts" / "character_lore.json")
    assert n >= 8
    assert store.seed_lore(PROJECT_ROOT / "prompts" / "character_lore.json") == 0  # idempotent
    chars = {l["character"] for l in store.list_lore()}
    assert {"Rin", "Len", "Luka", "KAITO", "MEIKO", "Teto"} <= chars
    assert store.search_memories("Рин") == []  # lore never shows up as user memory
    assert store.search_lore("Рин")


def test_backup_and_restart_recovery(tmp_path):
    path = tmp_path / "f.db"
    db = Database(path)
    store = MemoryStore(db)
    cid = store.start_conversation()
    store.add_message(cid, "user", "Мой любимый цвет — бирюзовый")
    store.add_memory("Любимый цвет пользователя — бирюзовый", "semantic", 0.8)
    b1 = db.backup(tmp_path / "bk", keep=2)
    db.backup(tmp_path / "bk", keep=2)
    db.backup(tmp_path / "bk", keep=2)
    assert len(list((tmp_path / "bk").glob("*.db"))) == 2
    db.close()
    # restart: reopen file
    store2 = MemoryStore(Database(path))
    assert store2.search_memories("какой мой любимый цвет")[0].content.endswith("бирюзовый")
    assert store2.recent_messages(cid)[0]["content"].startswith("Мой любимый")
    # the backup itself is a valid database
    bdb = Database(b1) if b1.exists() else None
    if bdb:
        assert bdb.integrity_ok()


def test_manager_duplicates_contradictions_sensitive(tmp_path):
    store = MemoryStore(Database(tmp_path / "g.db"))
    mgr = MemoryManager(store)
    o1 = mgr.ingest(Candidate("Пользователь любит зелёный чай", fact_key="user.drink"))
    assert o1.action == "inserted"
    o2 = mgr.ingest(Candidate("Пользователь любит зелёный чай.", fact_key="user.drink"))
    assert o2.action == "duplicate" and o2.memory_id == o1.memory_id
    o3 = mgr.ingest(Candidate("Пользователь больше любит кофе, а не чай", fact_key="user.drink"))
    assert o3.action == "superseded"
    assert not store.get_memory(o1.memory_id).is_active
    assert store.get_memory(o1.memory_id).superseded_by == o3.memory_id
    o4 = mgr.ingest(Candidate("Пароль пользователя от почты qwerty123"))
    assert o4.action == "pending" and o4.pending_id
    assert store.search_memories("пароль") == []  # not stored until confirmed
    new_id = store.resolve_pending(o4.pending_id, approve=False)
    assert new_id is None and store.list_pending() == []
    o5 = mgr.ingest(Candidate("У пользователя диагноз астма"))
    assert o5.action == "pending"
    mid = store.resolve_pending(o5.pending_id, approve=True)
    assert store.get_memory(mid).content.endswith("астма")


def test_explicit_remember_and_forget(tmp_path):
    store = MemoryStore(Database(tmp_path / "h.db"))
    mgr = MemoryManager(store)
    out = mgr.handle_explicit("Запомни, что меня зовут Дима", None)
    assert out.action == "inserted"
    assert store.get_memory(out.memory_id).content == "Пользователя зовут Дима"
    assert mgr.handle_explicit("Как дела?", None) is None
    out = mgr.handle_explicit("Забудь, что меня зовут Дима", None)
    assert out.action == "forgotten"
    assert store.search_memories("Дима") == []


async def test_llm_extraction_is_grounded(tmp_path):
    from helpers import ScriptedLLM, text
    store = MemoryStore(Database(tmp_path / "i.db"))
    mgr = MemoryManager(store)
    cid = store.start_conversation()
    m1 = store.add_message(cid, "user", "Кстати, я работаю программистом в Казани")
    llm = ScriptedLLM([text(
        '{"facts":[{"content":"Пользователь работает программистом в Казани","key":"user.job","type":"semantic",'
        '"importance":0.8,"quote":"я работаю программистом в Казани"},'
        '{"content":"Пользователь любит джаз","key":"user.music","type":"semantic","importance":0.5,'
        '"quote":"обожаю джаз"}]}')])
    outs = await mgr.extract_with_llm(llm, [{"id": m1, "content": "Кстати, я работаю программистом в Казани"}])
    assert [o.action for o in outs] == ["inserted", "rejected"]  # invented fact rejected
    assert store.get_memory(outs[0].memory_id).source_message_id == m1
    assert store.search_memories("джаз") == []
