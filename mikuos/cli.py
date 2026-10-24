"""Terminal chat client for a running backend:  python -m mikuos.cli

Commands: /status /memory /forget <id> /pending /approve <id> /deny <id>
          /mode chatty|normal|quiet|dnd /new /stop /resume /quit
"""
from __future__ import annotations

import asyncio
import json
import sys

import httpx
import websockets

from .config import Settings


def _base(settings: Settings) -> tuple[str, str]:
    port = settings.get("server", "port")
    token = (settings.resolve_path("runtime") / "token").read_text(encoding="utf-8").strip()
    return f"http://127.0.0.1:{port}", token


async def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    settings = Settings()
    try:
        base, token = _base(settings)
    except FileNotFoundError:
        print("Backend не запущен (нет data/runtime/token). Запустите run_mikuos.ps1 или python -m mikuos.")
        return
    http = httpx.AsyncClient(base_url=base, headers={"X-MikuOS-Token": token}, timeout=180)
    ws_url = base.replace("http", "ws") + f"/ws?token={token}"
    async with websockets.connect(ws_url, max_size=1 << 20) as ws:
        hello = json.loads(await ws.recv())
        llm = hello["status"]["llm"]
        print(f"MikuOS: LM Studio {'подключена' if llm['connected'] else 'НЕДОСТУПНА'} ({llm['model']})")
        print("Введите сообщение или /help.")
        done = asyncio.Event()

        async def reader():
            async for raw in ws:
                m = json.loads(raw)
                t = m.get("type")
                if t == "token":
                    print(m["text"], end="", flush=True)
                elif t == "reset":
                    print("\r\033[K", end="")
                elif t == "tool_call":
                    print(f"\n  ⚙ {m['name']}({m['arguments']})", flush=True)
                elif t == "tool_result":
                    r = m["result"]
                    print(f"  → {r.get('status')} {r.get('error', '')}".rstrip(), flush=True)
                elif t == "done":
                    if m.get("pending_requests"):
                        print(f"\n  ⏳ ждёт подтверждения: {', '.join(m['pending_requests'])} (/approve <id>)")
                    print()
                    done.set()
                elif t == "error":
                    print(f"\n[ошибка] {m['message']}")
                    done.set()
                elif t == "event":
                    ev = m["event"]
                    if ev["type"] in ("initiative_message", "assistant_message"):
                        print(f"\nМику: {ev['data'].get('text')}\n")
                    elif ev["type"] == "permission_request":
                        d = ev["data"]
                        print(f"\n  ⏳ запрос разрешения {d['id']}: {d['tool']} {d['arguments']} — {d['reason']}")
                    elif ev["type"] == "emergency_stop":
                        print(f"\n  ⛔ аварийная остановка: {'включена' if ev['data']['active'] else 'снята'}")

        task = asyncio.create_task(reader())
        loop = asyncio.get_running_loop()
        while True:
            line = (await loop.run_in_executor(None, lambda: input("> "))).strip()
            if not line:
                continue
            if line.startswith("/"):
                cmd, *rest = line[1:].split(maxsplit=1)
                arg = rest[0] if rest else ""
                if cmd in ("quit", "exit", "q"):
                    break
                r = None
                if cmd == "status":
                    r = await http.get("/api/status")
                elif cmd == "memory":
                    r = await http.get("/api/memory")
                elif cmd == "forget" and arg.isdigit():
                    r = await http.patch(f"/api/memory/{arg}", json={"is_active": False})
                elif cmd == "pending":
                    r = await http.get("/api/permissions/requests")
                    print(json.dumps(r.json(), ensure_ascii=False, indent=1))
                    r = await http.get("/api/memory/pending")
                elif cmd in ("approve", "deny") and arg:
                    path = f"/api/memory/pending/{arg}" if arg.isdigit() else f"/api/permissions/requests/{arg}"
                    r = await http.post(path, json={"approve": cmd == "approve"})
                elif cmd == "mode" and arg:
                    r = await http.patch("/api/settings", json={"initiative": {"mode": arg}})
                    r = await http.get("/api/status")
                elif cmd == "new":
                    r = await http.post("/api/conversations/new")
                elif cmd == "stop":
                    r = await http.post("/api/emergency_stop")
                elif cmd == "resume":
                    r = await http.post("/api/resume")
                else:
                    print(__doc__)
                    continue
                print(json.dumps(r.json(), ensure_ascii=False, indent=1))
                continue
            done.clear()
            await ws.send(json.dumps({"type": "chat", "text": line}))
            print("Мику: ", end="", flush=True)
            await done.wait()
        task.cancel()
    await http.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, EOFError):
        pass
