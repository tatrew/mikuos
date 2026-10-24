"""Test helpers.

ScriptedLLM is a MOCK of LLMClient used only where a real model cannot give a
deterministic answer (agent loop limits, malformed tool calls, honesty guard).
Every scenario it covers is also exercised against real LM Studio in
tests/integration when the server is available.
"""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mikuos.llm import ChatResult, LLMStatus, ToolCall


class ScriptedLLM:
    def __init__(self, script=None, connected=True, supports_tools=True):
        self.script = list(script or [])
        self.status = LLMStatus(connected=connected, model="scripted" if connected else None, context_length=8192,
                                supports_tools=supports_tools)
        self.calls: list[dict] = []
        self._listeners = []

    def on_status_change(self, cb):
        self._listeners.append(cb)

    def _next(self, messages, tools):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        if not self.status.connected:
            from mikuos.llm import LLMUnavailable
            raise LLMUnavailable("scripted: offline")
        item = self.script.pop(0) if self.script else ChatResult(content="(нет ответа)")
        return item(messages, tools) if callable(item) else item

    async def chat(self, messages, tools=None, **kw):
        return self._next(messages, tools)

    async def stream_chat(self, messages, tools=None, **kw):
        res = self._next(messages, tools)
        if res.content:
            yield {"type": "token", "text": res.content}
        yield {"type": "result", "result": res}

    async def connect(self, verify_generation=True):
        return self.status

    async def reconnect_loop(self, interval_s, stop):
        await stop.wait()

    async def aclose(self):
        pass


def text(content: str) -> ChatResult:
    return ChatResult(content=content)


def call(name: str, args: dict | str | None = None, cid: str = "c1") -> ChatResult:
    raw = args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False)
    return ChatResult(content="", tool_calls=[ToolCall(cid, name, raw)], finish_reason="tool_calls")


class FakeBridge:
    """Minimal in-memory bridge for tests that must not touch the real desktop."""

    def __init__(self):
        self.opened = []
        self.fullscreen = False

    def get_system_time(self):
        return {"iso": "2026-01-01T00:00:00"}

    def open_application(self, path, window_process=None, wait_s=0):
        self.opened.append(path)
        return {"started": True, "pid": 1, "window_found": True}

    def is_fullscreen_foreground(self):
        return self.fullscreen

    def list_windows(self):
        return []

    def get_active_window(self):
        return None

    def get_user_idle_time(self):
        return 0.0

    @staticmethod
    def read_text_file(path, max_chars=20000):
        from mikuos.windows.bridge import WindowsBridge
        return WindowsBridge.read_text_file(path, max_chars)

    def __getattr__(self, item):
        raise AttributeError(item)


class StubLMStudio:
    """Tiny real HTTP server imitating LM Studio's REST surface (a stub, not the
    real server). Lets us test retries / reconnection deterministically."""

    def __init__(self, models=None, fail_first=0, reply="Привет! Я Мику.", tool_call=None, native=True, port=0):
        self.models = models if models is not None else [
            {"id": "other-model", "type": "llm", "state": "not-loaded", "capabilities": ["tool_use"]},
            {"id": "gemma miku test", "type": "llm", "state": "loaded", "loaded_context_length": 4096,
             "capabilities": ["tool_use"]},
            {"id": "embed", "type": "embeddings", "state": "loaded"},
        ]
        self.fail_first = fail_first
        self.reply = reply
        self.tool_call = tool_call
        self.native = native
        self.requests: list[dict] = []
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                stub.requests.append({"path": self.path})
                if stub.fail_first > 0:
                    stub.fail_first -= 1
                    return self._send(500, {"error": "boom"})
                if self.path == "/api/v0/models":
                    if not stub.native:
                        return self._send(404, {})
                    return self._send(200, {"data": stub.models})
                if self.path == "/v1/models":
                    return self._send(200, {"data": [{"id": m["id"]} for m in stub.models]})
                self._send(404, {})

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                stub.requests.append({"path": self.path, "json": payload})
                if stub.fail_first > 0:
                    stub.fail_first -= 1
                    return self._send(500, {"error": "boom"})
                msg = {"role": "assistant", "content": stub.reply}
                if stub.tool_call and payload.get("tools"):
                    msg = {"role": "assistant", "content": "", "tool_calls": [
                        {"id": "t1", "type": "function", "function": stub.tool_call}]}
                if payload.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    if msg.get("tool_calls"):
                        fn = stub.tool_call
                        half = len(fn["arguments"]) // 2
                        chunks = [
                            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "t1", "function": {
                                "name": fn["name"], "arguments": fn["arguments"][:half]}}]}}]},
                            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {
                                "arguments": fn["arguments"][half:]}}]}, "finish_reason": "tool_calls"}]},
                        ]
                    else:
                        words = stub.reply.split(" ")
                        chunks = [{"choices": [{"delta": {"content": w + (" " if i < len(words) - 1 else "")}}]}
                                  for i, w in enumerate(words)]
                        chunks[-1]["choices"][0]["finish_reason"] = "stop"
                    for ch in chunks:
                        self.wfile.write(f"data: {json.dumps(ch)}\n\n".encode())
                        self.wfile.flush()
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                self._send(200, {"model": payload.get("model"), "choices": [
                    {"message": msg, "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop"}],
                    "usage": {"total_tokens": 5}})

        self.server = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()
        self.server.server_close()


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)
