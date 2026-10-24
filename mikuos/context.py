"""Prompt loading and context assembly.

Context = persona system prompt (editable file, unchanged)
        + runtime rules + list of tools actually available right now
        + relevant memories from SQLite (only what is stored)
        + relevant character lore (labelled as fiction)
        + permitted current events (time, active window if allowed)
        + recent messages of this conversation, trimmed to the token budget.
The full history is never sent.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .llm import estimate_tokens
from .memory import MemoryStore

log = logging.getLogger("mikuos.context")

FALLBACK_PROMPT = "Ты — Хацунэ Мику."


class PromptLoader:
    """Reads the persona prompt from disk; re-reads when the file changes."""

    def __init__(self, system_prompt_path: Path, rules_path: Path | None = None):
        self.system_prompt_path = Path(system_prompt_path)
        self.rules_path = Path(rules_path) if rules_path else None
        self._cache: dict[Path, tuple[float, str]] = {}

    def _read(self, path: Path | None) -> str:
        if not path or not path.exists():
            return ""
        mtime = path.stat().st_mtime
        cached = self._cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        text = path.read_text(encoding="utf-8").strip()
        self._cache[path] = (mtime, text)
        return text

    @property
    def persona(self) -> str:
        text = self._read(self.system_prompt_path)
        if not text:
            log.error("system prompt not found at %s; using minimal fallback", self.system_prompt_path)
            return FALLBACK_PROMPT
        return text

    @property
    def rules(self) -> str:
        return self._read(self.rules_path)


@dataclass
class BuiltContext:
    messages: list[dict]
    memory_ids: list[int] = field(default_factory=list)
    lore_ids: list[int] = field(default_factory=list)
    dropped_messages: int = 0
    token_estimate: int = 0


class ContextBuilder:
    def __init__(self, prompts: PromptLoader, store: MemoryStore, max_memories: int = 8, max_lore: int = 3,
                 recent_messages: int = 16):
        self.prompts = prompts
        self.store = store
        self.max_memories = max_memories
        self.max_lore = max_lore
        self.recent_messages = recent_messages

    def system_text(self, tool_names: list[str], unavailable: list[str] | None = None) -> str:
        parts = [self.prompts.persona]
        if self.prompts.rules:
            parts.append(self.prompts.rules)
        if tool_names:
            parts.append("Доступные сейчас инструменты: " + ", ".join(tool_names) + ".")
        else:
            parts.append("Сейчас у тебя нет доступных инструментов: ты не видишь экран, не открываешь программы и "
                         "не читаешь файлы.")
        if unavailable:
            parts.append("Недоступно: " + "; ".join(unavailable) + ".")
        return "\n\n".join(parts)

    def build(
        self,
        conversation_id: int,
        query: str,
        tool_names: list[str],
        budget_tokens: int,
        current_events: list[str] | None = None,
        unavailable: list[str] | None = None,
        extra_messages: list[dict] | None = None,
    ) -> BuiltContext:
        system = self.system_text(tool_names, unavailable)

        memories = self.store.search_memories(query, limit=self.max_memories) if query else []
        seen = {m.id for m in memories}
        # always include the most important stable facts (e.g. the user's name)
        for m in self.store.top_memories("semantic", limit=3):
            if m.id not in seen and len(memories) < self.max_memories + 3:
                memories.append(m)
                seen.add(m.id)
        if memories:
            lines = [f"- [{m.memory_type}, {m.created_at[:10]}] {m.content}" for m in memories]
            system += "\n\nПамять (факты из базы MikuOS о пользователе и прошлых разговорах):\n" + "\n".join(lines)
        else:
            system += "\n\nПамять: по этому запросу в базе ничего не найдено."

        summaries = self._recent_summaries(conversation_id)
        if summaries:
            system += "\n\nКраткие итоги прошлых разговоров:\n" + "\n".join(summaries)
        elif not memories and not self._has_past_conversations(conversation_id):
            # strong anchor against invented shared history on a fresh install
            system += ("\n\nВ базе MikuOS нет ни одного прошлого разговора и ни одного воспоминания о пользователе. "
                       "Не говори, что помнишь прошлые разговоры или общие события.")

        lore = self.store.search_lore(query, self.max_lore) if query else []
        if lore:
            system += "\n\nЛор (художественная история Мику, не факты о пользователе):\n" + "\n".join(
                f"- {l['fact']}" for l in lore)

        now = datetime.now().astimezone()
        events = [f"Текущее время: {now.strftime('%Y-%m-%d %H:%M, %A')}"] + list(current_events or [])
        system += "\n\nТекущие события (разрешённые данные наблюдения):\n" + "\n".join(f"- {e}" for e in events)

        history = self.store.recent_messages(conversation_id, self.recent_messages)
        chat: list[dict] = []
        for m in history:
            if m["role"] in ("user", "assistant"):
                chat.append({"role": m["role"], "content": m["content"]})
            elif m["role"] == "event":
                chat.append({"role": "user", "content": f"[Событие MikuOS, не реплика пользователя] {m['content']}"})
        chat.extend(extra_messages or [])
        # Gemma's chat template requires the first non-system turn to be the user's
        # and strictly alternating roles (a turn without a reply leaves user, user)
        while chat and chat[0]["role"] != "user":
            chat.pop(0)
        chat = _merge_same_role(chat)

        sys_tokens = estimate_tokens(system)
        dropped = 0
        while chat and sys_tokens + sum(estimate_tokens(_text(c)) for c in chat) > budget_tokens:
            if len(chat) == 1:
                break  # never drop the current message itself
            chat.pop(0)
            dropped += 1
            while chat and chat[0]["role"] != "user":
                chat.pop(0)
                dropped += 1
        if sys_tokens > budget_tokens:
            # extreme case: cut memories/lore, keep persona intact
            system = system[: budget_tokens * 3]
        messages = [{"role": "system", "content": system}] + chat
        return BuiltContext(
            messages=messages,
            memory_ids=[m.id for m in memories],
            lore_ids=[l["id"] for l in lore],
            dropped_messages=dropped,
            token_estimate=sum(estimate_tokens(_text(m)) for m in messages),
        )

    def _has_past_conversations(self, conversation_id: int) -> bool:
        return self.store.db.one(
            "SELECT 1 FROM messages WHERE conversation_id != ? AND role IN ('user','assistant') LIMIT 1",
            (conversation_id,)) is not None

    def _recent_summaries(self, conversation_id: int, limit: int = 3) -> list[str]:
        rows = self.store.db.query(
            "SELECT started_at, summary FROM conversations WHERE id != ? AND summary IS NOT NULL "
            "ORDER BY id DESC LIMIT ?", (conversation_id, limit))
        return [f"- {r['started_at'][:10]}: {r['summary']}" for r in rows]


def _merge_same_role(chat: list[dict]) -> list[dict]:
    """Join consecutive plain user/user or assistant/assistant turns into one."""
    out: list[dict] = []
    for m in chat:
        prev = out[-1] if out else None
        if (prev and m["role"] == prev["role"] and m["role"] in ("user", "assistant")
                and not m.get("tool_calls") and not prev.get("tool_calls")):
            out[-1] = {**prev, "content": f"{prev.get('content') or ''}\n\n{m.get('content') or ''}".strip()}
        else:
            out.append(dict(m))
    return out


def _text(msg: dict) -> str:
    c = msg.get("content") or ""
    if msg.get("tool_calls"):
        c += json.dumps(msg["tool_calls"], ensure_ascii=False)
    return c
