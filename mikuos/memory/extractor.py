"""Automatic long-term memory.

Pipeline for each candidate fact:
  sensitivity check -> pending queue (needs explicit user confirmation)
  duplicate check   -> bump confidence of the existing memory instead of inserting
  contradiction     -> same fact_key with different content supersedes the old memory
  otherwise insert.

Candidates come from two sources: explicit "запомни ..." commands (deterministic,
no LLM) and periodic LLM extraction over the recent user messages. Only facts the
user stated are kept; the extraction prompt forbids inventing anything and the
result is verified against the source text.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from .store import MemoryStore, normalize, similarity

log = logging.getLogger("mikuos.memory")

SENSITIVE_PATTERNS = [
    (r"парол|password|пин[- ]?код|\bpin\b|cvv|токен|api[ _-]?key|секретн", "credentials"),
    (r"номер карт|карт[аы] \d|\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b|счёт|счет в банке|iban|снилс|инн\b|паспорт",
     "financial/government id"),
    (r"диагноз|болезн|болею|лекарств|таблетк|депресс|психиатр|терапевт|беремен|вич|рак\b|здоровь", "health"),
    (r"адрес|живу на улице|улица|квартир[аеу] \d|\bдом \d", "address"),
    (r"\+?\d[\d\s\-()]{9,}\d", "phone number"),
    (r"религи|вероисповед|политическ|партия|ориентаци|гей|лесби|бисексу", "special category"),
    (r"e-?mail|почта|@[a-z0-9-]+\.[a-z]", "contact"),
]

EXPLICIT_REMEMBER = re.compile(
    r"^\s*(?:мику[,!]?\s*)?(?:пожалуйста[,]?\s*)?(?:запомни|запиши в память|remember)(?:\s*,\s*|\s*:\s*|\s+)(?:что\s+)?(.+)$",
    re.IGNORECASE | re.DOTALL,
)
EXPLICIT_FORGET = re.compile(r"^\s*(?:мику[,!]?\s*)?(?:забудь|удали из памяти|forget)(?:\s*,\s*|\s+)(?:что\s+|про\s+)?(.+)$",
                             re.IGNORECASE | re.DOTALL)

EXTRACTION_PROMPT = """Ты модуль долговременной памяти. Ниже реплики ПОЛЬЗОВАТЕЛЯ из разговора.
Выдели только устойчивые факты о пользователе, которые он сам явно сообщил (имя, предпочтения,
занятия, планы, важные события). Не выдумывай. Не включай факты о Мику, о вымышленных персонажах,
общие знания, вопросы и одноразовые мелочи. Если подходящих фактов нет — верни пустой список.

Ответь ТОЛЬКО JSON без пояснений:
{"facts":[{"content":"Пользователя зовут Алексей","key":"user.name","type":"semantic","importance":0.9,"quote":"меня зовут Алексей"}]}

key — короткий стабильный идентификатор темы (user.name, user.pet, user.favorite_food ...),
type — semantic (устойчивый факт) или episodic (событие с датой/временем),
quote — дословный фрагмент реплики пользователя, подтверждающий факт.

Реплики пользователя:
"""


@dataclass
class Candidate:
    content: str
    memory_type: str = "semantic"
    fact_key: str | None = None
    importance: float = 0.6
    quote: str | None = None
    source_message_id: int | None = None


@dataclass
class Outcome:
    action: str  # inserted | duplicate | superseded | pending | rejected
    memory_id: int | None = None
    pending_id: int | None = None
    reason: str | None = None

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def sensitivity(text: str) -> str | None:
    low = text.lower()
    for pattern, label in SENSITIVE_PATTERNS:
        if re.search(pattern, low):
            return label
    return None


class MemoryManager:
    def __init__(self, store: MemoryStore, require_confirmation_for_sensitive: bool = True,
                 duplicate_threshold: float = 0.85):
        self.store = store
        self.require_confirmation = require_confirmation_for_sensitive
        self.duplicate_threshold = duplicate_threshold

    def ingest(self, c: Candidate, user_confirmed: bool = False) -> Outcome:
        content = c.content.strip()
        if len(content) < 4:
            return Outcome("rejected", reason="too short")
        if c.memory_type not in ("semantic", "episodic", "activity"):
            c.memory_type = "semantic"
        label = sensitivity(content)
        if label and self.require_confirmation and not user_confirmed:
            pid = self.store.add_pending(content, c.memory_type, f"sensitive: {label}", c.importance,
                                         c.fact_key, c.source_message_id)
            return Outcome("pending", pending_id=pid, reason=label)
        dup = self.store.find_duplicate(content, c.memory_type, self.duplicate_threshold)
        if dup:
            self.store.update_memory(dup.id, confidence=min(1.0, dup.confidence + 0.1),
                                     importance=max(dup.importance, c.importance))
            return Outcome("duplicate", memory_id=dup.id)
        old = self.store.active_by_key(c.fact_key) if c.fact_key else []
        new_id = self.store.add_memory(content, c.memory_type, c.importance, 0.9 if user_confirmed else 0.75,
                                       c.source_message_id, c.fact_key)
        if old:
            for m in old:
                self.store.deactivate_memory(m.id, superseded_by=new_id)
            self.store.add_event("memory_superseded", {"old": [m.id for m in old], "new": new_id, "key": c.fact_key})
            return Outcome("superseded", memory_id=new_id, reason=f"replaced {[m.id for m in old]}")
        return Outcome("inserted", memory_id=new_id)

    # ------------------------------------------------------------- explicit
    def handle_explicit(self, user_text: str, source_message_id: int | None) -> Outcome | None:
        """'Запомни, что ...' / 'Забудь ...' handled without the LLM."""
        m = EXPLICIT_REMEMBER.match(user_text)
        if m:
            fact = _to_third_person(m.group(1).strip().rstrip(".!"))
            return self.ingest(Candidate(fact, "semantic", None, 0.8, source_message_id=source_message_id),
                               user_confirmed=False)
        m = EXPLICIT_FORGET.match(user_text)
        if m:
            target = m.group(1).strip().rstrip(".!")
            hits = self.store.search_memories(target, limit=3, touch=False)
            hits = [h for h in hits if similarity(target, h.content) > 0.35 or normalize(target) in normalize(h.content)]
            if not hits:
                return Outcome("rejected", reason="nothing matching to forget")
            for h in hits:
                self.store.deactivate_memory(h.id)
            self.store.add_event("memory_forgotten", {"ids": [h.id for h in hits], "query": target})
            return Outcome("forgotten", memory_id=hits[0].id, reason=f"deactivated {[h.id for h in hits]}")
        return None

    # ------------------------------------------------------------------ LLM
    async def extract_with_llm(self, llm, user_messages: list[dict]) -> list[Outcome]:
        """Ask the model for facts in recent user messages; verify each against the text."""
        if not user_messages:
            return []
        joined = "\n".join(f"[{m['id']}] {m['content']}" for m in user_messages)
        res = await llm.chat(
            [{"role": "user", "content": EXTRACTION_PROMPT + joined}],
            temperature=0.0, max_tokens=500,
        )
        facts = parse_facts(res.content)
        outcomes = []
        source_text = normalize(" ".join(m["content"] for m in user_messages))
        for f in facts:
            quote = normalize(f.get("quote") or "")
            # grounding check: the quoted evidence must actually be in the user's words
            if not quote or quote not in source_text:
                outcomes.append(Outcome("rejected", reason="not grounded in user text"))
                continue
            src_id = next((m["id"] for m in user_messages if quote in normalize(m["content"])), None)
            outcomes.append(self.ingest(Candidate(
                content=str(f.get("content", ""))[:500],
                memory_type=f.get("type", "semantic"),
                fact_key=(str(f["key"])[:64] if f.get("key") else None),
                importance=float(f.get("importance", 0.6) or 0.6),
                quote=f.get("quote"),
                source_message_id=src_id,
            )))
        return outcomes


def parse_facts(text: str) -> list[dict]:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    facts = data.get("facts") if isinstance(data, dict) else None
    return [f for f in facts if isinstance(f, dict) and f.get("content")] if isinstance(facts, list) else []


_FIRST_PERSON = [
    (r"\bменя зовут\b", "пользователя зовут"),
    (r"\bя люблю\b", "пользователь любит"),
    (r"\bя не люблю\b", "пользователь не любит"),
    (r"\bу меня есть\b", "у пользователя есть"),
    (r"^у меня\b", "у пользователя"),
]


def _to_third_person(fact: str) -> str:
    """Rephrase simple first-person facts; anything else is quoted verbatim."""
    for pat, rep in _FIRST_PERSON:
        new = re.sub(pat, rep, fact, count=1, flags=re.IGNORECASE)
        if new != fact:
            return new[:1].upper() + new[1:]
    return f"Пользователь сообщил: «{fact}»"
