"""FastAPI + WebSocket API. Binds to 127.0.0.1 only.

Protection against other local processes and web pages:
  * random bearer token per start, written to data/runtime/token (read by the
    overlay/CLI); required on every HTTP request (X-MikuOS-Token) and WebSocket;
  * Host header must be localhost/127.0.0.1 (blocks DNS-rebinding);
  * requests carrying a browser Origin are rejected unless explicitly allowed;
  * no CORS headers are emitted, so browsers cannot read responses.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict, model_validator

from .app import MikuOS
from .config import Settings
from .events import Event
from .llm import LLMError, LLMUnavailable
from .memory import Candidate

log = logging.getLogger("mikuos.server")

LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


class MemoryIn(BaseModel):
    content: str = Field(min_length=2, max_length=2000)
    memory_type: str = "semantic"
    importance: float = 0.6
    fact_key: str | None = None


class MemoryPatch(BaseModel):
    content: str | None = None
    importance: float | None = None
    confidence: float | None = None
    is_active: bool | None = None
    memory_type: str | None = None


class Decision(BaseModel):
    approve: bool


class PetEvent(BaseModel):
    kind: str = Field(pattern=r"^(click|double_click|drag|pet|hover)$")


class CharacterCommand(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    emotion: Literal['neutral', 'happy', 'excited', 'shy', 'curious', 'confused', 'surprised',
                     'sad', 'sleepy', 'thoughtful'] | None = None
    intensity: float = Field(default=1, ge=0, le=1)
    duration: float = Field(default=2.5, ge=0, le=60)
    animation: Literal['idle', 'wave', 'think', 'look_around', 'happy', 'sad', 'surprised', 'sleep'] | None = None
    action: Literal['idle', 'walk', 'run', 'wave', 'think', 'look_around', 'happy', 'sad', 'surprised', 'sleep', 'sit_on_window'] | None = None
    target_window_id: int | None = Field(default=None, gt=0)

    @model_validator(mode='after')
    def valid_target(self):
        if self.action == 'sit_on_window' and self.target_window_id is None:
            raise ValueError('sit_on_window requires target_window_id')
        if not (self.emotion or self.animation or self.action):
            raise ValueError('An emotion, animation or action is required')
        return self


class VoicePlayback(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    event: Literal['start', 'energy', 'end']
    stream_id: str = Field(min_length=1, max_length=128)
    rms: float = Field(default=0, ge=0, le=1)


def _speak_reply(c, text: str) -> None:
    """Typed chat: also say the reply aloud when voice.speak_replies == "all"."""
    if text and c.voice.available and c.voice.cfg.get("speak_replies", "all") == "all" and c.voice.state == "idle":
        asyncio.create_task(c.voice.speak(text))


def write_token(runtime_dir: Path) -> str:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path = runtime_dir / "token"
    path.write_text(token, encoding="utf-8")
    try:  # owner-only on Windows: drop inherited ACEs, grant current user
        import subprocess
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{os.environ.get('USERNAME', '')}:F"],
                       capture_output=True, timeout=10)
    except Exception:
        pass
    return token


def create_app(settings: Settings, core: MikuOS | None = None, token: str | None = None,
               start_core: bool = True) -> FastAPI:
    state: dict[str, Any] = {"core": core, "token": token, "clients": set()}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if state["core"] is None:
            state["core"] = MikuOS(settings)
        if state["token"] is None:
            state["token"] = write_token(settings.resolve_path("runtime"))
        c: MikuOS = state["core"]
        c.bus.subscribe("*", broadcast)
        if start_core:
            await c.start()
        yield
        if start_core:
            await c.stop()

    app = FastAPI(title="MikuOS", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.mikuos = state

    def core_() -> MikuOS:
        return state["core"]

    def _origin_ok(origin: str | None) -> bool:
        return origin is None or origin in (settings.get("server", "allowed_origins") or [])

    def _host_ok(host: str | None) -> bool:
        if not host:
            return False
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        return name in LOCAL_HOSTS

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if not _host_ok(request.headers.get("host")):
            return JSONResponse({"detail": "bad host"}, status_code=403)
        if not _origin_ok(request.headers.get("origin")):
            return JSONResponse({"detail": "cross-origin requests are not allowed"}, status_code=403)
        if request.url.path != "/health":
            sent = request.headers.get("x-mikuos-token", "")
            if not state["token"] or not secrets.compare_digest(sent, state["token"]):
                return JSONResponse({"detail": "missing or bad token"}, status_code=401)
        return await call_next(request)

    async def broadcast(ev: Event) -> None:
        msg = json.dumps({"type": "event", "event": ev.as_dict()}, ensure_ascii=False, default=str)
        for ws in list(state["clients"]):
            try:
                await ws.send_text(msg)
            except Exception:
                state["clients"].discard(ws)

    # ------------------------------------------------------------------ http
    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/api/status")
    async def status(c: MikuOS = Depends(core_)):
        return c.status()

    @app.post("/api/llm/reconnect")
    async def reconnect(c: MikuOS = Depends(core_)):
        return (await c.llm.connect(verify_generation=True)).as_dict()

    @app.post("/api/chat")
    async def chat(body: ChatIn, c: MikuOS = Depends(core_)):
        try:
            r = await c.chat(body.text)
        except LLMUnavailable as e:
            raise HTTPException(503, f"LM Studio unavailable: {e}")
        except LLMError as e:
            raise HTTPException(502, str(e))
        _speak_reply(c, r.text)
        return {"text": r.text, "tool_events": r.tool_events, "pending_requests": r.pending_requests,
                "executed": r.executed, "claim_corrected": r.claim_corrected, "conversation_id": c.conversation_id}

    @app.post("/api/conversations/new")
    async def new_conv(c: MikuOS = Depends(core_)):
        return {"conversation_id": await c.new_conversation()}

    @app.get("/api/conversations")
    async def conversations(c: MikuOS = Depends(core_)):
        return c.store.list_conversations()

    @app.get("/api/conversations/{cid}/messages")
    async def conv_messages(cid: int, c: MikuOS = Depends(core_)):
        return c.store.messages(cid)

    # memory
    @app.get("/api/memory")
    async def memories(type: str | None = None, include_inactive: bool = False, c: MikuOS = Depends(core_)):
        return [m.as_dict() for m in c.store.list_memories(type, include_inactive)]

    @app.get("/api/memory/search")
    async def memory_search(q: str, c: MikuOS = Depends(core_)):
        return [m.as_dict() for m in c.store.search_memories(q, touch=False)]

    @app.post("/api/memory")
    async def memory_add(body: MemoryIn, c: MikuOS = Depends(core_)):
        out = c.memory.ingest(Candidate(body.content, body.memory_type, body.fact_key, body.importance),
                              user_confirmed=True)
        return out.as_dict()

    @app.patch("/api/memory/{mid}")
    async def memory_patch(mid: int, body: MemoryPatch, c: MikuOS = Depends(core_)):
        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        try:
            return c.store.update_memory(mid, **fields).as_dict()
        except KeyError:
            raise HTTPException(404, "no such memory")
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/memory/{mid}")
    async def memory_delete(mid: int, c: MikuOS = Depends(core_)):
        if not c.store.delete_memory(mid):
            raise HTTPException(404, "no such memory")
        return {"deleted": mid}

    @app.get("/api/memory/pending")
    async def memory_pending(c: MikuOS = Depends(core_)):
        return c.store.list_pending()

    @app.post("/api/memory/pending/{pid}")
    async def memory_pending_resolve(pid: int, body: Decision, c: MikuOS = Depends(core_)):
        try:
            return {"memory_id": c.store.resolve_pending(pid, body.approve)}
        except KeyError:
            raise HTTPException(404, "no such pending memory")

    @app.get("/api/lore")
    async def lore(c: MikuOS = Depends(core_)):
        return c.store.list_lore()

    @app.get("/api/events")
    async def events(limit: int = 50, c: MikuOS = Depends(core_)):
        return c.store.recent_events(min(limit, 500))

    @app.get("/api/audit")
    async def audit(limit: int = 50, c: MikuOS = Depends(core_)):
        return c.store.recent_audit(min(limit, 500))

    # permissions & tools
    @app.get("/api/permissions/requests")
    async def perm_requests(c: MikuOS = Depends(core_)):
        return [r.as_dict() for r in c.perms.list_requests()]

    @app.post("/api/permissions/requests/{rid}")
    async def perm_resolve(rid: str, body: Decision, c: MikuOS = Depends(core_)):
        try:
            return await c.resolve_permission(rid, body.approve)
        except KeyError:
            raise HTTPException(404, "no such request")
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.get("/api/tools")
    async def tools(c: MikuOS = Depends(core_)):
        return [{**t.openai_schema()["function"], "permission": t.permission.value,
                 "policy": c.perms.policy(t.permission).value} for t in c.registry.tools.values()]

    @app.post("/api/tools/{name}")
    async def tool_direct(name: str, request: Request, c: MikuOS = Depends(core_)):
        args = await request.json() if await request.body() else {}
        if not isinstance(args, dict):
            raise HTTPException(400, "arguments must be an object")
        return await c.registry.execute_direct(name, args)

    @app.post("/api/voice/toggle")
    async def voice_toggle(c: MikuOS = Depends(core_)):
        """The user pressed the microphone button: start listening / finish / stop talking."""
        return {"result": c.voice.toggle(), **c.voice.status()}

    @app.post("/api/voice/stop")
    async def voice_stop(c: MikuOS = Depends(core_)):
        c.voice.cancel()
        return c.voice.status()

    @app.get("/api/voice")
    async def voice_status(c: MikuOS = Depends(core_)):
        return c.voice.status()

    @app.post("/api/emergency_stop")
    async def estop(c: MikuOS = Depends(core_)):
        await c.emergency_stop()
        return c.status()

    @app.post("/api/resume")
    async def resume(c: MikuOS = Depends(core_)):
        await c.resume()
        return c.status()

    # settings
    @app.get("/api/settings")
    async def get_settings():
        return settings.as_dict()

    @app.patch("/api/settings")
    async def patch_settings(request: Request, c: MikuOS = Depends(core_)):
        patch = await request.json()
        if not isinstance(patch, dict):
            raise HTTPException(400, "expected object")
        try:
            data = settings.update(patch)
        except ValueError as e:
            raise HTTPException(400, str(e))
        await c.bus.publish("settings_changed", {"sections": sorted(patch)})
        await c.bus.publish("status", c.status())
        return data

    @app.post("/api/character/event")
    async def character_event(body: PetEvent, c: MikuOS = Depends(core_)):
        await c.bus.publish("pet_interaction", {"kind": body.kind})
        return {"ok": True, "initiative": c.initiative.last_decision}

    @app.post('/api/character/command')
    async def character_command(body: CharacterCommand, c: MikuOS = Depends(core_)):
        if c.status()['emergency_stop']:
            raise HTTPException(409, 'emergency stop is active')
        await c.bus.publish('character_command', body.model_dump(exclude_none=True))
        return {'ok': True, 'status': 'dispatched'}

    @app.post('/api/character/voice')
    async def character_voice(body: VoicePlayback, c: MikuOS = Depends(core_)):
        if c.status()['emergency_stop']:
            raise HTTPException(409, 'emergency stop is active')
        await c.bus.publish('voice_playback', body.model_dump())
        return {'ok': True, 'status': 'dispatched'}

    # ------------------------------------------------------------- websocket
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        if not _host_ok(ws.headers.get("host")) or not _origin_ok(ws.headers.get("origin")):
            await ws.close(code=4403)
            return
        sent = ws.query_params.get("token") or ws.headers.get("x-mikuos-token", "")
        if not state["token"] or not secrets.compare_digest(sent, state["token"]):
            await ws.close(code=4401)
            return
        await ws.accept()
        c: MikuOS = state["core"]
        state["clients"].add(ws)
        await ws.send_text(json.dumps({"type": "hello", "status": c.status()}, ensure_ascii=False, default=str))
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                kind = msg.get("type")
                if kind == "chat" and isinstance(msg.get("text"), str) and msg["text"].strip():
                    asyncio.create_task(_ws_chat(ws, c, msg["text"][:8000], msg.get("turn_id")))
                elif kind == "pet_event" and msg.get("kind") in ("click", "double_click", "drag", "pet", "hover"):
                    await c.bus.publish("pet_interaction", {"kind": msg["kind"]})
                elif kind == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))
        except WebSocketDisconnect:
            pass
        finally:
            state["clients"].discard(ws)

    async def _ws_chat(ws: WebSocket, c: MikuOS, text: str, turn_id: Any) -> None:
        async def emit(ev: dict) -> None:
            try:
                await ws.send_text(json.dumps({**ev, "turn_id": turn_id}, ensure_ascii=False, default=str))
            except Exception:
                pass
        try:
            r = await c.chat(text, emit)
            await emit({"type": "done", "text": r.text, "pending_requests": r.pending_requests,
                        "executed": [e["tool"] for e in r.executed], "claim_corrected": r.claim_corrected})
            _speak_reply(c, r.text)
        except LLMUnavailable as e:
            await emit({"type": "error", "code": "llm_unavailable", "message": f"LM Studio недоступен: {e}"})
        except Exception as e:  # noqa: BLE001
            log.exception("chat failed")
            await emit({"type": "error", "code": "failed", "message": str(e)})

    return app
