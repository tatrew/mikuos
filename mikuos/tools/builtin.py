"""Built-in tools wired to WindowsBridge, MemoryStore and PermissionManager."""
from __future__ import annotations

from pathlib import Path

from ..config import Settings
from ..memory import Candidate, MemoryManager, MemoryStore
from ..permissions import Perm, PermissionManager
from ..windows import WindowsBridge, WindowsBridgeError
from ..windows.steam import Steam
from .registry import Tool, ToolFailure, ToolRegistry

WINDOW_ID = {"type": "integer", "minimum": 1, "description": "id окна из get_active_window или list_windows"}
PATH = {"type": "string", "minLength": 1, "maxLength": 400,
        "description": "имя файла внутри разрешённой папки (например note.txt) или полный путь"}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


def register_builtin_tools(reg: ToolRegistry, bridge: WindowsBridge, settings: Settings, store: MemoryStore,
                           memory: MemoryManager, perms: PermissionManager, steam: Steam | None = None) -> None:
    steam = steam or Steam()
    agent_cfg = settings.get("agent")
    max_read = int(agent_cfg.get("max_file_read_chars", 20000))
    timeout = float(agent_cfg.get("tool_timeout_s", 15))

    def wrap(fn):
        def inner(**kw):
            try:
                return fn(**kw)
            except WindowsBridgeError as e:
                raise ToolFailure(str(e)) from e
        return inner

    def path_or_fail(raw: str, kind: str, must_exist: bool = False) -> Path:
        p, why = perms.check_path(raw, kind, must_exist)
        if not p:
            raise ToolFailure(why, status="denied")
        return p

    def content_window(window_id: int):
        info = bridge.window_info(window_id)
        if not perms.content_allowed_process(info.process):
            raise ToolFailure(f"reading content of '{info.process}' windows is not allowed in settings", "denied")
        return info

    def window_meta(w) -> dict:
        return {"id": w.id, "title": w.title, "process": w.process, "x": w.rect.x, "y": w.rect.y,
                "width": w.rect.width, "height": w.rect.height, "minimized": w.is_minimized, "monitor": w.monitor}

    # --- observation ------------------------------------------------------
    reg.register(Tool("get_system_time", "Текущие дата и время компьютера.", _obj({}), Perm.NONE,
                      lambda: bridge.get_system_time(), timeout))

    def active_window():
        w = bridge.get_active_window()
        return window_meta(w) if w else {"active_window": None}

    reg.register(Tool("get_active_window", "Метаданные активного окна: id, заголовок, процесс, координаты и размер.",
                      _obj({}), Perm.OBSERVE_METADATA, wrap(active_window), timeout, untrusted_output=True))
    reg.register(Tool("list_windows", "Список видимых окон приложений (id, заголовок, процесс, геометрия).",
                      _obj({}), Perm.OBSERVE_METADATA,
                      wrap(lambda: [window_meta(w) for w in bridge.list_windows()][:30]), timeout,
                      untrusted_output=True))
    reg.register(Tool("get_window_geometry", "Положение и размер окна по id (физические пиксели).",
                      _obj({"window_id": WINDOW_ID}, ["window_id"]), Perm.OBSERVE_METADATA,
                      wrap(lambda window_id: bridge.get_window_geometry(window_id)), timeout))
    reg.register(Tool("get_cursor_position", "Координаты курсора мыши.", _obj({}), Perm.OBSERVE_METADATA,
                      wrap(lambda: bridge.get_cursor_position()), timeout))
    reg.register(Tool("get_user_idle_time", "Сколько секунд пользователь не трогал мышь и клавиатуру.", _obj({}),
                      Perm.OBSERVE_METADATA, wrap(lambda: {"idle_seconds": round(bridge.get_user_idle_time(), 1)}),
                      timeout))
    reg.register(Tool("get_running_applications", "Запущенные приложения с видимыми окнами.", _obj({}),
                      Perm.OBSERVE_METADATA, wrap(lambda: bridge.get_running_applications()), timeout))

    # --- applications & windows -------------------------------------------
    apps = settings.get("allowed_apps") or {}
    reg.register(Tool("list_allowed_apps", "Приложения, которые можно открывать.", _obj({}), Perm.NONE,
                      lambda: {k: v.get("title", k) for k, v in (settings.get("allowed_apps") or {}).items()},
                      timeout))

    def open_application(app_id: str):
        app = perms.allowed_app(app_id)
        if not app:
            raise ToolFailure(f"app '{app_id}' is not in the allowed list", "denied")
        res = bridge.open_application(app["path"], app.get("window_process"))
        return {"app_id": app_id, "title": app.get("title", app_id), **res}

    reg.register(Tool("open_application", "Открыть разрешённое приложение по app_id.",
                      _obj({"app_id": {"type": "string", "enum": sorted(apps) or ["none"]}}, ["app_id"]),
                      Perm.APP_CONTROL, wrap(open_application), max(timeout, 10)))
    reg.register(Tool("focus_window", "Переключиться на окно (сделать активным).",
                      _obj({"window_id": WINDOW_ID}, ["window_id"]), Perm.WINDOW_CONTROL,
                      wrap(lambda window_id: bridge.focus_window(window_id)), timeout))
    reg.register(Tool("minimize_window", "Свернуть окно.", _obj({"window_id": WINDOW_ID}, ["window_id"]),
                      Perm.WINDOW_CONTROL, wrap(lambda window_id: bridge.minimize_window(window_id)), timeout))
    reg.register(Tool("restore_window", "Развернуть свёрнутое окно.", _obj({"window_id": WINDOW_ID}, ["window_id"]),
                      Perm.WINDOW_CONTROL, wrap(lambda window_id: bridge.restore_window(window_id)), timeout))

    # --- files ------------------------------------------------------------
    def open_directory(path: str):
        return bridge.open_directory(path_or_fail(path, "read", must_exist=True))

    def open_file(path: str):
        p = path_or_fail(path, "read", must_exist=True)
        why = perms.check_openable_file(p)
        if why:
            raise ToolFailure(why, "denied")
        return bridge.open_file(p)

    def list_directory(path: str = "."):
        p = path_or_fail(path, "read", must_exist=True)
        if not p.is_dir():
            raise ToolFailure(f"not a directory: {p}")
        items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:100]
        return {"path": str(p), "items": [{"name": i.name, "dir": i.is_dir(),
                                           "size": i.stat().st_size if i.is_file() else None} for i in items]}

    def read_text_file(path: str):
        p = path_or_fail(path, "read", must_exist=True)
        why = perms.check_text_file(p)
        if why:
            raise ToolFailure(why, "denied")
        return bridge.read_text_file(p, max_read)

    def create_text_file(path: str, content: str):
        p = path_or_fail(path, "write")
        why = perms.check_text_file(p)
        if why:
            raise ToolFailure(why, "denied")
        return bridge.create_text_file(p, content)

    def write_text_file(path: str, content: str):
        p = path_or_fail(path, "write")
        why = perms.check_text_file(p)
        if why:
            raise ToolFailure(why, "denied")
        return bridge.write_text_file(p, content)

    def overwrites_existing(args: dict) -> Perm | None:
        p, _ = perms.check_path(args.get("path", ""), "write")
        return Perm.DESTRUCTIVE if p and p.exists() else None

    dirs = ", ".join(settings.get("allowed_directories", "read") or [])
    reg.register(Tool("open_directory", f"Открыть папку в Проводнике. Разрешены: {dirs}.",
                      _obj({"path": PATH}, ["path"]), Perm.APP_CONTROL, wrap(open_directory), timeout))
    reg.register(Tool("open_file", "Открыть файл программой по умолчанию (кроме исполняемых).",
                      _obj({"path": PATH}, ["path"]), Perm.APP_CONTROL, wrap(open_file), timeout))
    reg.register(Tool("list_directory", f"Содержимое разрешённой папки. Разрешены: {dirs}.",
                      _obj({"path": PATH}), Perm.FILE_READ, wrap(list_directory), timeout, untrusted_output=True))
    reg.register(Tool("read_text_file", "Прочитать текстовый файл из разрешённой папки.",
                      _obj({"path": PATH}, ["path"]), Perm.FILE_READ, wrap(read_text_file), timeout,
                      untrusted_output=True))
    reg.register(Tool("create_text_file", "Создать новый текстовый файл (не перезаписывает).",
                      _obj({"path": PATH, "content": {"type": "string", "maxLength": 100000}}, ["path", "content"]),
                      Perm.FILE_WRITE, wrap(create_text_file), timeout))
    reg.register(Tool("write_text_file", "Записать текстовый файл (перезаписывает существующий).",
                      _obj({"path": PATH, "content": {"type": "string", "maxLength": 100000}}, ["path", "content"]),
                      Perm.FILE_WRITE, wrap(write_text_file), timeout, escalate=overwrites_existing))

    # --- Steam --------------------------------------------------------------
    def list_steam_games():
        return [g.as_dict() for g in steam.games()]

    def launch_steam_game(game: str):
        return steam.launch(steam.find(game))

    def uninstall_steam_game(game: str):
        return steam.uninstall(steam.find(game))

    GAME = {"type": "string", "minLength": 1, "maxLength": 120,
            "description": "название установленной игры или её app_id из list_steam_games"}
    if steam.available():
        reg.register(Tool("list_steam_games", "Установленные игры Steam: название, app_id, размер.", _obj({}),
                          Perm.OBSERVE_METADATA, wrap(list_steam_games), timeout))
        reg.register(Tool("launch_steam_game", "Запустить установленную игру через Steam.",
                          _obj({"game": GAME}, ["game"]), Perm.APP_CONTROL, wrap(launch_steam_game),
                          max(timeout, 45)))
        reg.register(Tool("uninstall_steam_game",
                          "Удалить игру Steam. Всегда требует подтверждения пользователя.",
                          _obj({"game": GAME}, ["game"]), Perm.DESTRUCTIVE, wrap(uninstall_steam_game), timeout))

    # --- window content (separate permission + per-process allow list) ----
    def capture_window(window_id: int):
        content_window(window_id)
        return bridge.capture_window(window_id)

    def read_window_elements(window_id: int):
        info = content_window(window_id)
        return {"window": info.title, "elements": bridge.list_ui_elements(window_id, 60)}

    def invoke_ui_element(window_id: int, automation_id: str = "", name: str = ""):
        content_window(window_id)
        return bridge.invoke_ui_element(window_id, automation_id, name)

    reg.register(Tool("capture_window", "Сохранить скриншот разрешённого окна в файл.",
                      _obj({"window_id": WINDOW_ID}, ["window_id"]), Perm.OBSERVE_CONTENT, wrap(capture_window),
                      timeout))
    reg.register(Tool("read_window_elements", "Прочитать элементы интерфейса разрешённого окна (UI Automation).",
                      _obj({"window_id": WINDOW_ID}, ["window_id"]), Perm.OBSERVE_CONTENT,
                      wrap(read_window_elements), max(timeout, 20), untrusted_output=True))
    reg.register(Tool("invoke_ui_element", "Нажать элемент интерфейса разрешённого окна по automation_id или name.",
                      _obj({"window_id": WINDOW_ID, "automation_id": {"type": "string", "maxLength": 200},
                            "name": {"type": "string", "maxLength": 200}}, ["window_id"]),
                      Perm.UI_ACTION, wrap(invoke_ui_element), timeout))

    # --- memory -----------------------------------------------------------
    def search_memory(query: str):
        hits = store.search_memories(query, limit=8)
        return {"memories": [{"id": m.id, "type": m.memory_type, "content": m.content, "date": m.created_at[:10]}
                             for m in hits], "found": len(hits)}

    def remember_fact(content: str, key: str = ""):
        out = memory.ingest(Candidate(content, "semantic", key or None, 0.7))
        return out.as_dict()

    reg.register(Tool("search_memory", "Поиск в долговременной памяти MikuOS.",
                      _obj({"query": {"type": "string", "minLength": 2, "maxLength": 200}}, ["query"]), Perm.NONE,
                      search_memory, timeout))
    reg.register(Tool("remember_fact", "Сохранить факт о пользователе, когда он просит запомнить.",
                      _obj({"content": {"type": "string", "minLength": 4, "maxLength": 500},
                            "key": {"type": "string", "maxLength": 64}}, ["content"]),
                      Perm.MEMORY_WRITE, remember_fact, timeout))
