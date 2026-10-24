"""MikuOS core: wires settings, memory, LLM, tools, observer and initiative together."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path

from .agent import AgentEngine, TurnResult
from .config import Settings
from .context import ContextBuilder, PromptLoader
from .events import EventBus
from .initiative import InitiativeEngine
from .llm import LLMClient, LLMError, LLMUnavailable
from .memory import Database, MemoryManager, MemoryStore
from .observer import ActivityObserver
from .permissions import Origin, Perm, PermissionManager
from .tools import ToolRegistry, register_builtin_tools

log = logging.getLogger("mikuos.app")

INITIATIVE_SUFFIX = (" Скажи пользователю одну-две короткие живые фразы. Упоминай прошлое только если оно есть "
                     "в блоке «Память» или в истории выше.")
SUMMARY_PROMPT = ("Кратко (1–2 предложения, по-русски, в третьем лице) перескажи, о чём был этот разговор "
                  "пользователя с Мику. Только факты из реплик, ничего не придумывай.\n\n")


class MikuOS:
    def __init__(self, settings: Settings, bridge=None, llm: LLMClient | None = None, enable_hotkey: bool = True):
        self.settings = settings
        self.bus = EventBus()
        self.db = Database(settings.resolve_path("database"))
        self.store = MemoryStore(self.db)
        mem_cfg = settings.get("memory")
        self.memory = MemoryManager(self.store, mem_cfg["require_confirmation_for_sensitive"],
                                    mem_cfg["duplicate_threshold"])
        self.perms = PermissionManager(settings)
        if bridge is None:
            from .windows import WindowsBridge
            bridge = WindowsBridge(settings.resolve_path("screenshots"))
        self.bridge = bridge
        lm, gen = settings.get("lmstudio"), settings.get("generation")
        self.llm = llm or LLMClient(
            base_url=lm["base_url"], model=lm["model"], model_preference=lm["model_preference"],
            allow_jit_load=lm["allow_jit_load"], request_timeout_s=lm["request_timeout_s"],
            connect_timeout_s=lm["connect_timeout_s"], max_retries=lm["max_retries"],
            reasoning_effort=lm.get("reasoning_effort"), temperature=gen["temperature"], top_p=gen["top_p"],
            max_tokens=gen["max_tokens"],
        )
        self.llm.on_status_change(self._on_llm_status)
        agent_cfg = settings.get("agent")
        self.registry = ToolRegistry(self.perms, self.store, agent_cfg["duplicate_window_s"],
                                     agent_cfg["tool_timeout_s"])
        register_builtin_tools(self.registry, self.bridge, settings, self.store, self.memory, self.perms)
        self.prompts = PromptLoader(settings.resolve_path("system_prompt"), settings.resolve_path("runtime_rules"))
        self.context = ContextBuilder(self.prompts, self.store, mem_cfg["max_memories_in_context"],
                                      mem_cfg["max_lore_in_context"], mem_cfg["recent_messages"])
        self.observer = ActivityObserver(self.bridge, self.bus, settings, self.perms, self.store)
        self.agent = AgentEngine(
            self.llm, self.registry, self.context, self.store, agent_cfg["max_tool_iterations"],
            agent_cfg["max_consecutive_errors"], lm["tool_mode"],
            gen["context_length"] or gen["fallback_context_length"], gen["max_tokens"],
            current_events=self.observer.current_context_lines,
        )
        self.initiative = InitiativeEngine(self.bus, settings, self.store, self.perms, self._speak_initiative,
                                           is_fullscreen=self._is_fullscreen,
                                           llm_available=lambda: self.llm.status.connected)
        self.perms.on_request(self._on_permission_request)
        self.conversation_id: int | None = None
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._chat_lock = asyncio.Lock()
        self._extract_lock = asyncio.Lock()
        self.enable_hotkey = enable_hotkey
        self.hotkey = None
        self.voice_hotkey = None
        from .voice import VoiceService
        self.voice = VoiceService(settings, self.bus, self._voice_chat, settings.resolve_path("models"),
                                  blocked=lambda: self.perms.emergency_stopped)
        self.bus.subscribe("initiative_message", self._speak_event)
        self.bus.subscribe("assistant_message", self._speak_event)
        self.started_at = datetime.now().astimezone()

    # ----------------------------------------------------------- lifecycle
    async def start(self, connect_llm: bool = True) -> None:
        loop = asyncio.get_running_loop()
        self.bus.bind_loop(loop)
        try:
            self.db.backup(self.settings.resolve_path("backups"), self.settings.get("memory", "backup_keep"))
        except Exception:
            log.exception("database backup failed")
        self.store.seed_lore(self.settings.resolve_path("lore_seed"))
        for d in self.settings.resolve_dir_list("write"):
            d.mkdir(parents=True, exist_ok=True)
        self._recover_conversation()
        if connect_llm:
            st = await self.llm.connect(verify_generation=True)
            log.info("LM Studio: connected=%s model=%s ctx=%s err=%s", st.connected, st.model, st.context_length,
                     st.last_error)
        self._tasks.append(asyncio.create_task(
            self.llm.reconnect_loop(self.settings.get("lmstudio", "reconnect_interval_s"), self._stop),
            name="llm-reconnect"))
        self._tasks.append(asyncio.create_task(self._maintenance_loop(), name="maintenance"))
        self.observer.start()
        if self.enable_hotkey:
            self._start_hotkey(loop)
            if self.voice.available:
                self._start_voice_hotkey(loop)
        if self.voice.available and self.voice.cfg.get("preload", True):
            self._tasks.append(asyncio.create_task(self._preload_voice(), name="voice-preload"))
        self.store.add_event("app_started", {"model": self.llm.status.model})
        await self.bus.publish("status", self.status())

    async def stop(self) -> None:
        self._stop.set()
        await self.observer.stop()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.hotkey:
            self.hotkey.stop()
        if self.voice_hotkey:
            self.voice_hotkey.stop()
        self.voice.cancel()
        self.store.add_event("app_stopped", {})
        await self.llm.aclose()
        self.db.close()

    def _start_hotkey(self, loop: asyncio.AbstractEventLoop) -> None:
        from .hotkey import HotkeyListener
        spec = self.settings.get("hotkey", "emergency_stop") or "ctrl+alt+m"
        self.hotkey = HotkeyListener(spec, lambda: asyncio.run_coroutine_threadsafe(self.emergency_stop(), loop),
                                     fallbacks=self.settings.get("hotkey", "fallbacks") or [])
        if not self.hotkey.start():
            log.warning("emergency hotkey not registered: %s", self.hotkey.error)

    def _start_voice_hotkey(self, loop: asyncio.AbstractEventLoop) -> None:
        from .hotkey import HotkeyListener
        cfg = self.voice.cfg
        try:  # optional: a bad setting must not stop the backend
            self.voice_hotkey = HotkeyListener(cfg.get("hotkey") or "ctrl+alt+space",
                                               lambda: loop.call_soon_threadsafe(self.voice.toggle),
                                               hotkey_id=2, fallbacks=cfg.get("hotkey_fallbacks") or [],
                                               purpose="voice")
        except ValueError as e:
            log.warning("voice hotkey disabled: %s", e)
            return
        if not self.voice_hotkey.start():
            log.warning("voice hotkey not registered: %s", self.voice_hotkey.error)

    async def _preload_voice(self) -> None:
        try:
            await asyncio.to_thread(self.voice.stt.load)
            await asyncio.to_thread(self.voice.tts.load)
            log.info("voice models loaded")
            await self.bus.publish("voice_state", {"state": self.voice.state, "ready": True})
        except Exception as e:  # noqa: BLE001 - voice is optional, the rest keeps working
            self.voice.last_error = str(e)
            log.exception("voice models failed to load")

    async def _voice_chat(self, text: str) -> str:
        return (await self.chat(text)).text

    async def _speak_event(self, ev) -> None:
        """Say Miku's own lines aloud: initiative messages and action reports
        (voice turns speak themselves; typed-chat replies are spoken by the server)."""
        d = ev.data
        if not d.get("text") or d.get("kind") == "voice_reply" or self.voice.cfg.get("speak_replies", "all") == "off":
            return
        if ev.type == "initiative_message" and not self.voice.cfg.get("speak_initiative", True):
            return
        if self.voice.available and self.voice.state == "idle":
            asyncio.create_task(self.voice.speak(d["text"]))

    def _recover_conversation(self) -> None:
        """Resume the last open conversation if it is recent, close stale ones."""
        timeout = timedelta(minutes=self.settings.get("memory", "conversation_idle_timeout_min"))
        now = datetime.now().astimezone()
        for c in self.store.open_conversations():
            last = self.store.last_activity(c["id"])
            if last and now - last < timeout and self.conversation_id is None:
                self.conversation_id = c["id"]
                log.info("resumed conversation %d", c["id"])
            elif c["id"] != self.conversation_id:
                self.store.end_conversation(c["id"])
        if self.conversation_id is None:
            self.conversation_id = self.store.start_conversation()

    async def _maintenance_loop(self) -> None:
        """Summarize finished conversations and rotate idle ones (needs the LLM)."""
        while not self._stop.is_set():
            try:
                await self._rotate_idle_conversation()
                if self.llm.status.connected and self.settings.get("memory", "summarize_conversations"):
                    await self.summarize_pending()
            except Exception:
                log.exception("maintenance failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

    async def _rotate_idle_conversation(self) -> None:
        if self.conversation_id is None or self._chat_lock.locked():
            return
        timeout = timedelta(minutes=self.settings.get("memory", "conversation_idle_timeout_min"))
        last = self.store.last_activity(self.conversation_id)
        if last and datetime.now().astimezone() - last > timeout and self.store.count_messages(self.conversation_id):
            await self.new_conversation()

    async def new_conversation(self) -> int:
        old = self.conversation_id
        if old is not None:
            await self.run_memory_extraction(old, force=True)
            self.store.end_conversation(old)
        self.conversation_id = self.store.start_conversation()
        await self.bus.publish("conversation_started", {"conversation_id": self.conversation_id})
        return self.conversation_id

    async def summarize_pending(self, limit: int = 3) -> int:
        rows = self.db.query(
            "SELECT id FROM conversations WHERE ended_at IS NOT NULL AND summary IS NULL ORDER BY id DESC LIMIT ?",
            (limit,))
        done = 0
        for r in rows:
            msgs = [m for m in self.store.messages(r["id"]) if m["role"] in ("user", "assistant")]
            if len(msgs) < 2:
                self.store.end_conversation(r["id"], summary="")
                continue
            transcript = "\n".join(f"{'Пользователь' if m['role'] == 'user' else 'Мику'}: {m['content'][:400]}"
                                   for m in msgs[-30:])
            try:
                res = await self.llm.chat([{"role": "user", "content": SUMMARY_PROMPT + transcript}],
                                          temperature=0.2, max_tokens=150)
            except LLMError:
                break
            summary = res.content.strip()
            if summary:
                self.store.end_conversation(r["id"], summary=summary)
                date = (self.store.get_conversation(r["id"]) or {}).get("started_at", "")[:10]
                self.store.add_memory(f"Разговор {date}: {summary}", "episodic", 0.4, 0.7, msgs[-1]["id"])
                done += 1
        return done

    # ----------------------------------------------------------------- chat
    async def chat(self, text: str, emit=None) -> TurnResult:
        text = (text or "").strip()
        if not text:
            raise ValueError("empty message")
        async with self._chat_lock:
            if self.conversation_id is None:
                self.conversation_id = self.store.start_conversation()
            cid = self.conversation_id
            msg_id = self.store.add_message(cid, "user", text)
            if self.settings.get("memory", "auto_memory"):
                outcome = self.memory.handle_explicit(text, msg_id)
                if outcome:
                    await self.bus.publish("memory_changed", outcome.as_dict())
            try:
                result = await self.agent.run_turn(cid, text, Origin.USER_CHAT, emit)
            except LLMUnavailable as e:
                self.store.add_message(cid, "event", "LM Studio недоступен, ответ не сгенерирован", {"error": str(e)})
                raise
            reply = result.text or "…"
            self.store.add_message(cid, "assistant", reply,
                                   {"tools": [e["name"] for e in result.tool_events],
                                    "pending": result.pending_requests} if result.tool_events else None)
        if self.settings.get("memory", "auto_memory"):
            asyncio.create_task(self.run_memory_extraction(cid))
        return result

    async def run_memory_extraction(self, conversation_id: int, force: bool = False) -> list:
        """LLM extraction over user messages not yet processed."""
        if not self.settings.get("memory", "auto_memory") or not self.llm.status.connected:
            return []
        every = int(self.settings.get("memory", "extract_every_user_messages") or 4)
        async with self._extract_lock:
            last = self._last_extracted(conversation_id)
            pending = [m for m in self.store.messages(conversation_id) if m["role"] == "user" and m["id"] > last]
            if not pending or (len(pending) < every and not force):
                return []
            try:
                outcomes = await self.memory.extract_with_llm(self.llm, pending[-12:])
            except LLMError as e:
                log.warning("memory extraction failed: %s", e)
                return []
            self.store.add_event("memory_extraction", {"conversation_id": conversation_id,
                                                       "last_message_id": pending[-1]["id"],
                                                       "outcomes": [o.action for o in outcomes]})
            if any(o.action in ("inserted", "superseded", "pending") for o in outcomes):
                await self.bus.publish("memory_changed", {"outcomes": [o.as_dict() for o in outcomes]})
            return outcomes

    def _last_extracted(self, conversation_id: int) -> int:
        for ev in self.store.recent_events(200, "memory_extraction"):
            if ev["event_data"].get("conversation_id") == conversation_id:
                return int(ev["event_data"].get("last_message_id", 0))
        return 0

    # ---------------------------------------------------------- permissions
    def _on_permission_request(self, kind: str, req) -> None:
        if kind == "created":
            try:
                self.bus.publish_threadsafe("permission_request", req.as_dict())
            except RuntimeError:
                pass

    async def resolve_permission(self, req_id: str, approve: bool) -> dict:
        req = self.perms.resolve_request(req_id, approve)
        if not approve:
            res = {"status": "denied", "error": "user denied"}
            self.store.audit(req.tool, req.arguments, res, "denied:user")
        else:
            res = await self.registry.execute_approved(req)
        cid = req.conversation_id or self.conversation_id
        verdict = "подтвердил" if approve else "отклонил"
        note = f"Пользователь {verdict} действие {req.tool}. Результат: {res.get('status')}"
        if res.get("error"):
            note += f" ({res['error']})"
        self.store.add_message(cid, "event", note, {"request_id": req_id, "result": res})
        await self.bus.publish("permission_resolved", {"request_id": req_id, "approved": approve, "result": res})
        if self.llm.status.connected:
            asyncio.create_task(self._report_action(cid, note))
        return res

    async def _report_action(self, cid: int, note: str) -> None:
        """Let Miku tell the user the real outcome (no tools in this turn)."""
        async with self._chat_lock:
            try:
                r = await self.agent.run_turn(cid, note, Origin.AUTONOMOUS, allow_tools=False)
            except LLMError:
                return
            if r.text:
                self.store.add_message(cid, "assistant", r.text, {"report": True})
                await self.bus.publish("assistant_message", {"text": r.text, "kind": "action_report"})

    # ------------------------------------------------------------ initiative
    async def _speak_initiative(self, trigger: str, description: str, data: dict) -> str | None:
        if self._chat_lock.locked():
            return None
        async with self._chat_lock:
            cid = self.conversation_id or self.store.start_conversation()
            self.store.add_message(cid, "event", description + INITIATIVE_SUFFIX, {"trigger": trigger})
            r = await self.agent.run_turn(cid, description, Origin.AUTONOMOUS, allow_tools=False)
            if r.text:
                self.store.add_message(cid, "assistant", r.text, {"initiative": trigger})
            return r.text or None

    def _is_fullscreen(self) -> bool:
        try:
            return bool(self.bridge.is_fullscreen_foreground())
        except Exception:
            return False

    # ------------------------------------------------------------- control
    async def emergency_stop(self) -> None:
        self.voice.cancel()
        self.perms.emergency_stop()
        self.settings.update({"permissions": {"cursor_control": "deny"}})
        self.store.add_event("emergency_stop", {})
        log.warning("EMERGENCY STOP: tools, observation, initiative and cursor interaction disabled")
        await self.bus.publish("emergency_stop", {"active": True})
        await self.bus.publish("status", self.status())

    async def resume(self) -> None:
        self.perms.resume()
        self.store.add_event("emergency_resume", {})
        await self.bus.publish("emergency_stop", {"active": False})
        await self.bus.publish("status", self.status())

    def _on_llm_status(self, st) -> None:
        try:
            self.bus.publish_threadsafe("llm_status", st.as_dict())
        except RuntimeError:
            pass

    def status(self) -> dict:
        return {
            "llm": self.llm.status.as_dict(),
            "conversation_id": self.conversation_id,
            "observation": {"enabled": self.observer.enabled, "active": self.observer.active,
                            "content_reading": self.perms.policy(Perm.OBSERVE_CONTENT).value},
            "initiative": {"mode": self.initiative.mode, "used_today": self.initiative.used_today(),
                           "daily_limit": self.settings.get("initiative", "daily_llm_limit"),
                           "last_decision": self.initiative.last_decision},
            "emergency_stop": self.perms.emergency_stopped,
            "pending_permissions": len(self.perms.list_requests()),
            "pending_memories": len(self.store.list_pending()),
            "hotkey": {"spec": self.settings.get("hotkey", "emergency_stop"),
                       "active": self.hotkey.active_spec if self.hotkey else None,
                       "registered": bool(self.hotkey and self.hotkey.registered),
                       "error": self.hotkey.error if self.hotkey else None},
            "voice": {**self.voice.status(),
                      "hotkey_active": self.voice_hotkey.active_spec if self.voice_hotkey else None},
            "tools": [t.name for t in self.registry.available()],
            "system_prompt": str(self.prompts.system_prompt_path),
            "started_at": self.started_at.isoformat(timespec="seconds"),
        }
