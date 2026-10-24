"""2. Context assembly: persona file unchanged, relevant memory only, budget trimming."""
from mikuos.config import PROJECT_ROOT
from mikuos.context import ContextBuilder, PromptLoader
from mikuos.memory import Database, MemoryStore

PERSONA = PROJECT_ROOT / "miku_data" / "miku_persona_system_v4.txt"


def make(tmp_path):
    store = MemoryStore(Database(tmp_path / "ctx.db"))
    store.seed_lore(PROJECT_ROOT / "prompts" / "character_lore.json")
    loader = PromptLoader(PERSONA, PROJECT_ROOT / "prompts" / "runtime_rules.txt")
    return store, ContextBuilder(loader, store, max_memories=5, max_lore=2, recent_messages=10)


def test_persona_loaded_verbatim_and_tools_listed(tmp_path):
    store, cb = make(tmp_path)
    cid = store.start_conversation()
    store.add_message(cid, "user", "Привет")
    ctx = cb.build(cid, "Привет", ["open_application"], 4000)
    system = ctx.messages[0]["content"]
    assert system.startswith(PERSONA.read_text(encoding="utf-8").strip())
    assert "open_application" in system
    assert "Память: по этому запросу в базе ничего не найдено" in system
    assert ctx.messages[-1] == {"role": "user", "content": "Привет"}


def test_no_tools_means_explicit_no_capabilities(tmp_path):
    store, cb = make(tmp_path)
    cid = store.start_conversation()
    store.add_message(cid, "user", "Что у меня на экране?")
    system = cb.build(cid, "Что у меня на экране?", [], 4000).messages[0]["content"]
    assert "нет доступных инструментов" in system and "не видишь экран" in system


def test_relevant_memory_and_lore_labelled(tmp_path):
    store, cb = make(tmp_path)
    store.add_memory("У пользователя есть собака Бублик", "semantic", 0.6)
    store.add_memory("Пользователь не любит дождь", "semantic", 0.2)
    cid = store.start_conversation()
    store.add_message(cid, "user", "Помнишь мою собаку? И как там Рин?")
    ctx = cb.build(cid, "Помнишь мою собаку? И как там Рин?", [], 4000)
    s = ctx.messages[0]["content"]
    assert "собака Бублик" in s
    assert "Лор (художественная история Мику, не факты о пользователе)" in s and "Рин" in s
    assert ctx.memory_ids and ctx.lore_ids


def test_history_trimmed_to_budget_and_starts_with_user(tmp_path):
    store, cb = make(tmp_path)
    cb.recent_messages = 200
    cid = store.start_conversation()
    for i in range(60):
        store.add_message(cid, "user", f"сообщение {i} " + "x" * 300)
        store.add_message(cid, "assistant", f"ответ {i} " + "y" * 300)
    store.add_message(cid, "user", "последний вопрос")
    ctx = cb.build(cid, "последний вопрос", [], budget_tokens=2500)
    assert ctx.dropped_messages > 0
    assert ctx.token_estimate <= 2500 + 50
    assert ctx.messages[1]["role"] == "user"
    assert ctx.messages[-1]["content"] == "последний вопрос"
    # full history is never sent
    assert len(ctx.messages) < 121


def test_events_rendered_as_non_user_turns(tmp_path):
    store, cb = make(tmp_path)
    cid = store.start_conversation()
    store.add_message(cid, "user", "привет")
    store.add_message(cid, "assistant", "привет!")
    store.add_message(cid, "event", "Пользователь вернулся")
    ctx = cb.build(cid, "", [], 4000, current_events=["Активное приложение пользователя: notepad.exe"])
    assert "[Событие MikuOS, не реплика пользователя]" in ctx.messages[-1]["content"]
    assert "notepad.exe" in ctx.messages[0]["content"]


def test_prompt_reloads_when_file_changes(tmp_path):
    p = tmp_path / "persona.txt"
    p.write_text("Версия 1", encoding="utf-8")
    loader = PromptLoader(p)
    assert loader.persona == "Версия 1"
    import os
    import time
    p.write_text("Версия 2", encoding="utf-8")
    os.utime(p, (time.time() + 5, time.time() + 5))
    assert loader.persona == "Версия 2"
    assert PromptLoader(tmp_path / "missing.txt").persona  # fallback, no crash


def test_roles_alternate_after_unanswered_turns(tmp_path):
    # a failed reply / unanswered desktop event used to leave user,user,... and
    # LM Studio rejected every later request ("roles must alternate")
    store, cb = make(tmp_path)
    cid = store.start_conversation()
    store.add_message(cid, "event", "click")
    store.add_message(cid, "assistant", "hi")
    store.add_message(cid, "user", "hello")
    store.add_message(cid, "event", "LM Studio unavailable")
    store.add_message(cid, "event", "drag")
    store.add_message(cid, "user", "how are you")
    store.add_message(cid, "user", "hey")
    chat = cb.build(cid, "hey", [], 4000).messages[1:]
    roles = [m["role"] for m in chat]
    assert roles == ["user", "assistant", "user"]
    assert chat[-1]["content"].startswith("hello") and chat[-1]["content"].endswith("hey")
    assert "how are you" in chat[-1]["content"]
