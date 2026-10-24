"""Async client for the LM Studio OpenAI-compatible server.

Responsibilities: model discovery (no hard-coded model id), health checks,
streaming and non-streaming chat completions, native tool calls, retries,
timeouts and connection-state tracking.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

import httpx

log = logging.getLogger("mikuos.llm")


class LLMError(Exception):
    """Generation failed (LM Studio replied with an error or garbage)."""


class LLMUnavailable(LLMError):
    """LM Studio is not reachable or no usable model is loaded."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string as produced by the model

    def as_message(self) -> dict:
        return {"id": self.id, "type": "function", "function": {"name": self.name, "arguments": self.arguments}}


@dataclass
class ChatResult:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict = field(default_factory=dict)
    model: str = ""


@dataclass
class LLMStatus:
    connected: bool = False
    model: str | None = None
    context_length: int | None = None
    supports_tools: bool = False
    last_error: str | None = None
    last_check: float = 0.0

    def as_dict(self) -> dict:
        return {
            "connected": self.connected,
            "model": self.model,
            "context_length": self.context_length,
            "supports_tools": self.supports_tools,
            "last_error": self.last_error,
            "last_check": self.last_check,
        }


class LLMClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:1234/v1",
        model: str = "auto",
        model_preference: list[str] | None = None,
        allow_jit_load: bool = False,
        request_timeout_s: float = 120,
        connect_timeout_s: float = 5,
        max_retries: int = 2,
        reasoning_effort: str | None = "none",
        temperature: float = 0.7,
        top_p: float = 0.95,
        max_tokens: int = 600,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        # LM Studio's native REST API lives next to /v1 and reports load state
        self.native_url = self.base_url[: -len("/v1")] + "/api/v0" if self.base_url.endswith("/v1") else None
        self.configured_model = model
        self.model_preference = [p.lower() for p in (model_preference or [])]
        self.allow_jit_load = allow_jit_load
        self.max_retries = max_retries
        self.reasoning_effort = reasoning_effort
        self.defaults = {"temperature": temperature, "top_p": top_p, "max_tokens": max_tokens}
        self.timeout = httpx.Timeout(request_timeout_s, connect=connect_timeout_s)
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self.status = LLMStatus()
        self._listeners: list[Callable[[LLMStatus], None]] = []
        # single in-flight generation: the local GPU runs one model instance
        self._gen_lock = asyncio.Lock()

    # ------------------------------------------------------------------ infra
    def on_status_change(self, cb: Callable[[LLMStatus], None]) -> None:
        self._listeners.append(cb)

    def _set_status(self, **kw) -> None:
        before = (self.status.connected, self.status.model)
        for k, v in kw.items():
            setattr(self.status, k, v)
        self.status.last_check = time.time()
        if before != (self.status.connected, self.status.model):
            for cb in self._listeners:
                try:
                    cb(self.status)
                except Exception:  # listener bugs must not break the client
                    log.exception("status listener failed")

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout, transport=self._transport)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, url: str, retries: int | None = None, **kw) -> httpx.Response:
        last: Exception | None = None
        retries = self.max_retries if retries is None else retries
        for attempt in range(retries + 1):
            try:
                resp = await self.client.request(method, url, **kw)
                if resp.status_code >= 500:
                    raise LLMError(f"LM Studio HTTP {resp.status_code}: {resp.text[:300]}")
                return resp
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError, httpx.RemoteProtocolError) as e:
                last = e
            except LLMError as e:
                last = e
            if attempt < retries:
                await asyncio.sleep(min(0.5 * 2**attempt, 4))
        if isinstance(last, LLMError):
            raise last
        raise LLMUnavailable(f"LM Studio unreachable at {self.base_url}: {last!r}")

    # -------------------------------------------------------------- discovery
    async def list_models(self, retries: int | None = None) -> list[dict]:
        """Return LLM models with load state when the native API is available."""
        if self.native_url:
            try:
                r = await self._request("GET", f"{self.native_url}/models", retries=retries)
                if r.status_code == 200:
                    return [m for m in r.json().get("data", []) if m.get("type", "llm") in ("llm", "vlm")]
            except LLMUnavailable:
                raise
            except Exception:
                log.debug("native /api/v0/models unavailable, falling back to /v1/models")
        r = await self._request("GET", f"{self.base_url}/models", retries=retries)
        if r.status_code != 200:
            raise LLMError(f"/models returned {r.status_code}")
        models = r.json().get("data", [])
        return [m for m in models if "embed" not in m.get("id", "").lower()]

    def _pick_model(self, models: list[dict]) -> dict | None:
        if self.configured_model and self.configured_model != "auto":
            for m in models:
                if m.get("id") == self.configured_model:
                    return m
            return {"id": self.configured_model}  # trust explicit config
        has_state = any("state" in m for m in models)
        loaded = [m for m in models if m.get("state") == "loaded"] if has_state else list(models)
        pool = loaded if (loaded or not self.allow_jit_load) else models

        def score(m: dict) -> int:
            mid = m.get("id", "").lower()
            return 1 if any(p in mid for p in self.model_preference) else 0

        if not pool:
            return None
        return sorted(pool, key=score, reverse=True)[0]

    async def connect(self, verify_generation: bool = True) -> LLMStatus:
        """Discover the model and optionally verify that generation works."""
        try:
            models = await self.list_models()
            chosen = self._pick_model(models)
            if not chosen:
                raise LLMUnavailable("LM Studio is running but no LLM is loaded (load the Miku model in LM Studio)")
            ctx = chosen.get("loaded_context_length") or chosen.get("max_context_length")
            self._set_status(
                model=chosen["id"],
                context_length=ctx,
                supports_tools="tool_use" in (chosen.get("capabilities") or []),
            )
            if verify_generation:
                res = await self.chat([{"role": "user", "content": "ping"}], max_tokens=4, temperature=0)
                if res is None:
                    raise LLMError("empty generation")
            self._set_status(connected=True, last_error=None)
        except LLMError as e:
            self._set_status(connected=False, last_error=str(e))
        return self.status

    async def health_check(self) -> bool:
        """Cheap liveness probe (model list only, no generation)."""
        try:
            models = await self.list_models(retries=0)
        except LLMError as e:
            self._set_status(connected=False, last_error=str(e))
            return False
        if self.configured_model in ("auto", "", None) and any("state" in m for m in models):
            if not any(m.get("id") == self.status.model and m.get("state") == "loaded" for m in models):
                self._set_status(connected=False, last_error=f"model '{self.status.model}' is no longer loaded")
                return False
        return True

    async def ensure_connected(self) -> None:
        if not self.status.connected or not self.status.model:
            await self.connect(verify_generation=False)
            if not self.status.model:
                raise LLMUnavailable(self.status.last_error or "LM Studio unavailable")

    # ------------------------------------------------------------- generation
    def _payload(self, messages: list[dict], tools: list[dict] | None, stream: bool, **overrides) -> dict:
        p: dict[str, Any] = {"model": self.status.model, "messages": messages, "stream": stream}
        for k, v in self.defaults.items():
            p[k] = overrides.get(k, v)
        if self.reasoning_effort:
            p["reasoning_effort"] = self.reasoning_effort
        if tools:
            p["tools"] = tools
            p["tool_choice"] = overrides.get("tool_choice", "auto")
        if "response_format" in overrides:
            p["response_format"] = overrides["response_format"]
        return p

    async def chat(self, messages: list[dict], tools: list[dict] | None = None, **overrides) -> ChatResult:
        await self.ensure_connected()
        payload = self._payload(messages, tools, stream=False, **overrides)
        async with self._gen_lock:
            try:
                r = await self._request("POST", f"{self.base_url}/chat/completions", json=payload)
            except LLMUnavailable as e:
                self._set_status(connected=False, last_error=str(e))
                raise
        if r.status_code != 200:
            raise LLMError(f"chat/completions HTTP {r.status_code}: {r.text[:500]}")
        try:
            data = r.json()
            choice = data["choices"][0]
            msg = choice.get("message") or {}
        except (ValueError, KeyError, IndexError) as e:
            raise LLMError(f"malformed LM Studio response: {e}") from e
        self._set_status(connected=True, last_error=None)
        calls = [
            ToolCall(
                id=tc.get("id") or f"call_{i}",
                name=(tc.get("function") or {}).get("name", ""),
                arguments=(tc.get("function") or {}).get("arguments") or "{}",
            )
            for i, tc in enumerate(msg.get("tool_calls") or [])
        ]
        return ChatResult(
            content=msg.get("content") or "",
            tool_calls=calls,
            finish_reason=choice.get("finish_reason"),
            usage=data.get("usage") or {},
            model=data.get("model", ""),
        )

    async def stream_chat(
        self, messages: list[dict], tools: list[dict] | None = None, **overrides
    ) -> AsyncIterator[dict]:
        """Yield {"type":"token","text"} events, then one {"type":"result","result":ChatResult}."""
        await self.ensure_connected()
        payload = self._payload(messages, tools, stream=True, **overrides)
        content: list[str] = []
        calls: dict[int, dict] = {}
        finish = None
        async with self._gen_lock:
            try:
                async with self.client.stream("POST", f"{self.base_url}/chat/completions", json=payload) as r:
                    if r.status_code != 200:
                        body = (await r.aread()).decode("utf-8", "replace")
                        raise LLMError(f"chat/completions HTTP {r.status_code}: {body[:500]}")
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except ValueError:
                            continue
                        if chunk.get("error"):
                            raise LLMError(str(chunk["error"]))
                        for ch in chunk.get("choices", []):
                            delta = ch.get("delta") or {}
                            if delta.get("content"):
                                content.append(delta["content"])
                                yield {"type": "token", "text": delta["content"]}
                            for tc in delta.get("tool_calls") or []:
                                slot = calls.setdefault(tc.get("index", 0), {"id": None, "name": "", "arguments": ""})
                                if tc.get("id"):
                                    slot["id"] = tc["id"]
                                fn = tc.get("function") or {}
                                if fn.get("name"):
                                    slot["name"] += fn["name"]
                                if fn.get("arguments"):
                                    slot["arguments"] += fn["arguments"]
                            if ch.get("finish_reason"):
                                finish = ch["finish_reason"]
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError, httpx.RemoteProtocolError) as e:
                self._set_status(connected=False, last_error=repr(e))
                raise LLMUnavailable(f"stream interrupted: {e!r}") from e
            except httpx.ReadTimeout as e:
                raise LLMError("LM Studio stream timed out") from e
        self._set_status(connected=True, last_error=None)
        result = ChatResult(
            content="".join(content),
            tool_calls=[
                ToolCall(id=v["id"] or f"call_{i}", name=v["name"], arguments=v["arguments"] or "{}")
                for i, v in sorted(calls.items())
            ],
            finish_reason=finish,
            model=self.status.model or "",
        )
        yield {"type": "result", "result": result}

    async def reconnect_loop(self, interval_s: float, stop: asyncio.Event) -> None:
        """Background task: re-discover LM Studio while disconnected."""
        while not stop.is_set():
            if not self.status.connected:
                await self.connect(verify_generation=False)
                if self.status.model and not self.status.connected:
                    # model found but generation not verified yet: mark connected only on success
                    await self.connect(verify_generation=True)
            elif not self._gen_lock.locked():
                await self.health_check()
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
            except asyncio.TimeoutError:
                pass


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (Cyrillic Gemma tokens average ~3 chars)."""
    return max(1, len(text) // 3)
