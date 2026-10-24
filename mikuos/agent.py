"""Agent engine: User/Event -> Context -> LM Studio -> tool calls -> permissions ->
WindowsBridge -> tool results -> final answer.

Safety properties enforced here (not left to the model):
  * bounded tool iterations and consecutive-error budget;
  * duplicate calls inside a turn are not re-executed (registry);
  * JSON fallback accepts only a strict {"tool": ..., "arguments": {...}} object
    naming a registered tool — anything else is plain text;
  * the final answer may not claim an action ("открыла", "создала"...) unless a
    side-effect tool actually returned status "ok" in this turn.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .context import ContextBuilder
from .llm import ChatResult, LLMClient, ToolCall
from .memory import MemoryStore
from .permissions import Origin
from .tools import ToolRegistry, TurnState

log = logging.getLogger("mikuos.agent")

ACTION_CLAIM = re.compile(
    r"\b(открыла|запустила|создала|записала|сохранила|свернула|развернула|переключила(?:сь)?|закрыла|удалила|"
    r"нажала|переместила|i opened|i launched|i created|"
    # "doing it right now" phrasing: the model narrates an action instead of calling the tool
    r"запускаю|открываю|удаляю|создаю|закрываю|сворачиваю|разворачиваю)\b",
    re.IGNORECASE,
)
# "I sent the command/request": true once a tool was actually called (ok or awaiting confirmation)
SEND_CLAIM = re.compile(r"\b(?:отправ(?:ила|ляю)|передала|передаю)\s+(?:\w+\s+){0,2}?(?:команд|запрос)"
                        r"|\b(?:команда|запрос)\s+(?:\w+\s+){0,2}?отправлен", re.IGNORECASE)
# claims about reading/observing need any successful tool call, not a side effect
READ_CLAIM = re.compile(r"\b(прочитала|прочитал|сделала скриншот|посмотрела (?:на )?(?:экран|окно))\b", re.IGNORECASE)
JSON_TOOL_HINT = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```|^\s*(\{\s*\"tool\".*\})\s*$", re.DOTALL)

UNAVAILABLE_FEATURES = [
    "OCR (распознавание текста на экране) не подключён",
    "распознавание и синтез речи не подключены",
    "произвольные команды терминала/PowerShell/Python запрещены",
]

FALLBACK_INSTRUCTIONS = """Чтобы вызвать инструмент, ответь ТОЛЬКО одним JSON-объектом без другого текста:
{"tool": "<имя>", "arguments": {...}}
После этого ты получишь результат и ответишь пользователю. Инструменты:
"""

Emit = Callable[[dict], Awaitable[None]]


@dataclass
class TurnResult:
    text: str
    tool_events: list[dict] = field(default_factory=list)
    executed: list[dict] = field(default_factory=list)
    pending_requests: list[str] = field(default_factory=list)
    iterations: int = 0
    claim_corrected: bool = False
    used_fallback: bool = False
    memory_ids: list[int] = field(default_factory=list)


class AgentEngine:
    def __init__(self, llm: LLMClient, registry: ToolRegistry, context: ContextBuilder, store: MemoryStore,
                 max_tool_iterations: int = 5, max_consecutive_errors: int = 2, tool_mode: str = "auto",
                 context_length: int = 8192, max_tokens: int = 600,
                 current_events: Callable[[], list[str]] | None = None):
        self.llm = llm
        self.registry = registry
        self.context = context
        self.store = store
        self.max_tool_iterations = max_tool_iterations
        self.max_consecutive_errors = max_consecutive_errors
        self.tool_mode = tool_mode
        self.context_length = context_length
        self.max_tokens = max_tokens
        self.current_events = current_events or (lambda: [])

    def _budget(self) -> int:
        ctx = self.llm.status.context_length or self.context_length
        return max(1024, int(ctx) - self.max_tokens - 256)

    async def run_turn(self, conversation_id: int, query: str, origin: Origin = Origin.USER_CHAT,
                       emit: Emit | None = None, extra_messages: list[dict] | None = None,
                       allow_tools: bool = True) -> TurnResult:
        emit = emit or _noop
        tools = self.registry.available(origin) if allow_tools else []
        native = self.tool_mode == "native" or (self.tool_mode == "auto" and self.llm.status.supports_tools is not False)
        built = self.context.build(conversation_id, query, [t.name for t in tools], self._budget(),
                                   self.current_events(), UNAVAILABLE_FEATURES, extra_messages)
        messages = built.messages
        schemas = [t.openai_schema() for t in tools]
        if tools and (self.tool_mode == "json_fallback"):
            native = False
            messages[0]["content"] += "\n\n" + FALLBACK_INSTRUCTIONS + json.dumps(
                [s["function"] for s in schemas], ensure_ascii=False)
        turn = TurnState(origin=origin, conversation_id=conversation_id)
        result = TurnResult(text="", memory_ids=built.memory_ids)
        errors = 0
        nudges = 0
        offer_tools = bool(tools)

        for iteration in range(self.max_tool_iterations + 1):
            result.iterations = iteration + 1
            last_round = iteration == self.max_tool_iterations or not offer_tools
            gen = await self._generate(messages, schemas if (native and not last_round) else None, emit)
            calls = gen.tool_calls if not last_round else []
            if not calls and not last_round and tools:
                fb = parse_json_tool_call(gen.content, set(self.registry.tools))
                if fb and self.tool_mode in ("auto", "json_fallback"):
                    calls = [fb]
                    result.used_fallback = True
                    await emit({"type": "reset"})  # retract streamed JSON
            if not calls and not last_round and nudges < 2 and (
                    mentions_tool(gen.content, self.registry.tools)
                    or (not result.tool_events and claims_action(gen.content))):
                # small models sometimes describe a tool call in prose ("запускаю игру") instead of making it
                nudges += 1
                await emit({"type": "reset"})
                messages = messages + [
                    {"role": "assistant", "content": gen.content},
                    {"role": "user", "content": "[MikuOS — не реплика пользователя] Ты описала действие словами, "
                                                "но не вызвала инструмент, поэтому ничего не произошло. Если действие "
                                                "нужно, вызови инструмент сейчас; иначе ответь пользователю, не "
                                                "утверждая, что что-то делаешь."}]
                continue
            if not calls:
                result.text = gen.content.strip()
                break
            if native and gen.tool_calls:
                messages.append({"role": "assistant", "content": gen.content or "",
                                 "tool_calls": [c.as_message() for c in calls]})
            else:
                messages.append({"role": "assistant", "content": gen.content})
            for call in calls:
                await emit({"type": "tool_call", "name": call.name, "arguments": call.arguments})
                res = await self.registry.execute(call.name, call.arguments, turn)
                ev = {"type": "tool_result", "name": call.name, "arguments": call.arguments, "result": res}
                result.tool_events.append(ev)
                await emit(ev)
                if res.get("status") == "pending_confirmation":
                    result.pending_requests.append(res["request_id"])
                errors = errors + 1 if res.get("status") not in ("ok", "pending_confirmation") else 0
                payload = json.dumps(res, ensure_ascii=False, default=str)[:6000]
                if native and gen.tool_calls:
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": payload})
                else:
                    messages.append({"role": "user",
                                     "content": f"[Результат инструмента {call.name}] {payload}"})
                self.store.add_message(conversation_id, "tool", payload,
                                       {"tool": call.name, "arguments": call.arguments})
            if errors >= self.max_consecutive_errors:
                log.warning("stopping tool loop after %d consecutive errors", errors)
                offer_tools = False
            if any(e["result"].get("status") == "pending_confirmation" for e in result.tool_events):
                offer_tools = False  # wait for the user; do not try alternatives

        result.executed = turn.executed
        result.text = await self._enforce_honesty(result, messages, emit)
        return result

    async def _generate(self, messages: list[dict], tools: list[dict] | None, emit: Emit) -> ChatResult:
        final: ChatResult | None = None
        async for ev in self.llm.stream_chat(messages, tools=tools):
            if ev["type"] == "token":
                await emit(ev)
            elif ev["type"] == "result":
                final = ev["result"]
        return final or ChatResult(content="")

    async def _enforce_honesty(self, result: TurnResult, messages: list[dict], emit: Emit) -> str:
        text = result.text
        any_ok = any(e["result"].get("status") == "ok" for e in result.tool_events)
        false_action = bool(ACTION_CLAIM.search(text)) and not result.executed
        false_read = bool(READ_CLAIM.search(text)) and not any_ok
        sent = any(e["result"].get("status") in ("ok", "pending_confirmation") for e in result.tool_events)
        false_action = false_action or (bool(SEND_CLAIM.search(text)) and not sent)
        if not (false_action or false_read):
            return text
        statuses = ", ".join(f"{e['name']}={e['result'].get('status')}" for e in result.tool_events) or "нет вызовов"
        log.warning("model claimed an action without a successful tool call; regenerating (%s): %r",
                    statuses, text[:200])
        await emit({"type": "reset"})
        # The note goes into the system prompt and the false reply is dropped: appended as a chat
        # message, the small model tends to answer the note ("Поняла замечание") instead of the user.
        note = ("\n\n[Проверка MikuOS для этого ответа] Ни одно действие в этом ходе не выполнено успешно "
                f"(инструменты: {statuses}). Отвечая пользователю, не говори, что что-то открыла, запустила, "
                "создала, удалила, прочитала, отправила или делаешь сейчас. Если действие ждёт подтверждения — "
                "так и скажи. Не упоминай эту проверку.")
        messages = [{**messages[0], "content": messages[0]["content"] + note}] + messages[1:]
        gen = await self._generate(messages, None, emit)
        result.claim_corrected = True
        fixed = gen.content.strip()
        if (false_action and claims_action(fixed)) or (false_read and READ_CLAIM.search(fixed)):
            fixed += "\n\n(MikuOS: действие на самом деле не выполнялось.)"
            await emit({"type": "token", "text": "\n\n(MikuOS: действие на самом деле не выполнялось.)"})
        return fixed


def claims_action(content: str) -> bool:
    return bool(content) and bool(ACTION_CLAIM.search(content) or SEND_CLAIM.search(content))


def mentions_tool(content: str, tools) -> bool:
    return bool(content) and any(re.search(rf"`?\b{re.escape(name)}\b`?", content) for name in tools)


def parse_json_tool_call(content: str, known_tools: set[str]) -> ToolCall | None:
    """Strict fallback parser: exactly one JSON object naming a known tool."""
    if not content or "tool" not in content:
        return None
    m = JSON_TOOL_HINT.search(content.strip())
    if not m:
        return None
    raw = m.group(1) or m.group(2)
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(obj, dict) or set(obj) - {"tool", "arguments"}:
        return None
    name, args = obj.get("tool"), obj.get("arguments", {})
    if name not in known_tools or not isinstance(args, dict):
        return None
    return ToolCall(id="fallback_0", name=name, arguments=json.dumps(args, ensure_ascii=False))


async def _noop(_: dict) -> None:
    return None
