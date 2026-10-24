"""PermissionManager: every tool call passes through `check()` before execution.

Policy values per permission: allow | ask | deny.
Additional hard rules that settings cannot relax:
  * paths must resolve inside allowed directories (symlinks / '..' resolved first);
  * executables and scripts can never be opened as files;
  * actions requested by the model after it has seen untrusted content (window /
    file / web text) in the same turn are downgraded from allow to ask;
  * actions from autonomous turns (initiative, events) that change anything are denied;
  * the emergency stop denies everything except reads of MikuOS's own state.
Pending "ask" requests are stored here until the user approves or denies them.
"""
from __future__ import annotations

import ipaddress
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .config import Settings


class Perm(str, Enum):
    OBSERVE_METADATA = "observe_metadata"
    OBSERVE_CONTENT = "observe_content"
    APP_CONTROL = "app_control"
    WINDOW_CONTROL = "window_control"
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    MEMORY_WRITE = "memory_write"
    UI_ACTION = "ui_action"
    CURSOR_CONTROL = "cursor_control"
    DESTRUCTIVE = "destructive"  # deleting / uninstalling / overwriting: never runs without the user's OK
    WEB = "web"  # open a web page / search in the default browser
    MEDIA = "media"  # play / pause / switch tracks in Spotify
    NONE = "none"  # internal, always allowed (e.g. search own memory)


SIDE_EFFECT_PERMS = {Perm.APP_CONTROL, Perm.WINDOW_CONTROL, Perm.FILE_WRITE, Perm.UI_ACTION, Perm.CURSOR_CONTROL,
                     Perm.MEMORY_WRITE, Perm.DESTRUCTIVE, Perm.WEB, Perm.MEDIA}

BLOCKED_OPEN_EXTENSIONS = {
    ".exe", ".com", ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".msi", ".msp",
    ".scr", ".pif", ".lnk", ".url", ".hta", ".cpl", ".reg", ".dll", ".jar", ".py", ".pyw", ".appref-ms", ".application",
    ".gadget", ".inf", ".sys", ".msc", ".settingcontent-ms", ".library-ms", ".search-ms",
}
TEXT_EXTENSIONS = {".txt", ".md", ".log", ".csv", ".json", ".ini", ".yaml", ".yml", ".toml", ".xml", ".html", ".css",
                   ".rst", ".srt"}


class Decision(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class Origin(str, Enum):
    USER_CHAT = "user_chat"  # model turn started by a message the user typed
    USER_DIRECT = "user_direct"  # the user pressed a button / called the API directly
    AUTONOMOUS = "autonomous"  # initiative / event-driven model turn


@dataclass
class CheckResult:
    decision: Decision
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision == Decision.ALLOW


@dataclass
class PendingRequest:
    id: str
    tool: str
    arguments: dict
    permission: str
    reason: str
    created_at: float
    conversation_id: int | None = None
    status: str = "pending"  # pending | approved | denied | expired | executed
    result: Any = None
    meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


class PermissionManager:
    def __init__(self, settings: Settings, request_ttl_s: float = 600):
        self.settings = settings
        self.request_ttl_s = request_ttl_s
        self._lock = threading.RLock()
        self._pending: dict[str, PendingRequest] = {}
        self._emergency = False
        self._listeners: list[Callable[[str, PendingRequest], None]] = []

    # ------------------------------------------------------------- emergency
    @property
    def emergency_stopped(self) -> bool:
        return self._emergency

    def emergency_stop(self) -> None:
        with self._lock:
            self._emergency = True
            for r in self._pending.values():
                if r.status == "pending":
                    r.status = "denied"

    def resume(self) -> None:
        self._emergency = False

    # ---------------------------------------------------------------- policy
    def policy(self, perm: Perm) -> Decision:
        if perm == Perm.NONE:
            return Decision.ALLOW
        raw = self.settings.get("permissions", perm.value, "deny")
        try:
            return Decision(raw)
        except ValueError:
            return Decision.DENY

    def check(self, perm: Perm, origin: Origin = Origin.USER_CHAT, tainted: bool = False) -> CheckResult:
        if self._emergency and perm != Perm.NONE:
            return CheckResult(Decision.DENY, "emergency stop is active")
        base = self.policy(perm)
        if base == Decision.DENY:
            return CheckResult(Decision.DENY, f"permission '{perm.value}' is disabled in settings")
        if origin == Origin.AUTONOMOUS and perm in SIDE_EFFECT_PERMS:
            return CheckResult(Decision.DENY, "autonomous turns may not change anything")
        if origin == Origin.USER_DIRECT:
            # the user is acting directly: their click is the confirmation
            return CheckResult(Decision.ALLOW, "direct user action")
        if perm == Perm.DESTRUCTIVE:
            # "allow" in settings is ignored: every deletion is confirmed by the user
            return CheckResult(Decision.ASK, "deleting / overwriting always needs the user's confirmation")
        if base == Decision.ALLOW and tainted and perm in SIDE_EFFECT_PERMS:
            return CheckResult(Decision.ASK, "requested after reading untrusted content; needs confirmation")
        return CheckResult(base, "policy")

    # ----------------------------------------------------------------- paths
    def _roots(self, kind: str) -> list[Path]:
        roots = []
        for p in self.settings.resolve_dir_list(kind):
            try:
                roots.append(Path(os.path.realpath(p)))
            except OSError:
                continue
        return roots

    def check_path(self, raw: str, kind: str, must_exist: bool = False) -> tuple[Path | None, str]:
        """Resolve a model-supplied path. Relative paths are taken relative to the
        first allowed directory. Returns (path, "") or (None, reason)."""
        if not raw or "\x00" in raw:
            return None, "empty or invalid path"
        if raw.startswith("\\\\") or raw.startswith("//"):
            return None, "network / device paths are not allowed"
        roots = self._roots(kind)
        if not roots:
            return None, f"no directories are allowed for {kind}"
        p = Path(os.path.expanduser(raw))
        if not p.is_absolute():
            p = roots[0] / p
        try:
            resolved = Path(os.path.realpath(p))
        except OSError as e:
            return None, f"cannot resolve path: {e}"
        if ":" in resolved.name:  # NTFS alternate data streams
            return None, "alternate data streams are not allowed"
        for root in roots:
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            if must_exist and not resolved.exists():
                return None, f"path does not exist: {resolved}"
            return resolved, ""
        return None, f"path is outside allowed {kind} directories: {resolved}"

    @staticmethod
    def check_url(raw: str) -> tuple[str | None, str]:
        """Validate a model-supplied web address. Only public http(s) sites: no other
        schemes, no credentials in the URL, no localhost / IP literals / local names
        (the browser must not be pointed at MikuOS's own API or the home network)."""
        raw = (raw or "").strip()
        if not raw or len(raw) > 2000 or any(ch.isspace() or ord(ch) < 32 for ch in raw):
            return None, "empty, too long or contains spaces/control characters"
        if "://" not in raw:
            raw = "https://" + raw
        try:
            u = urlsplit(raw)
            host = (u.hostname or "").lower().rstrip(".")
            u.port  # noqa: B018 - raises ValueError for a malformed port
        except ValueError as e:
            return None, f"malformed URL: {e}"
        if u.scheme not in ("http", "https"):
            return None, f"only http/https links can be opened (got '{u.scheme}')"
        if u.username or u.password:
            return None, "links with a login/password inside are not allowed"
        if not host or "." not in host:
            return None, "a public site name is required"
        try:
            ipaddress.ip_address(host.strip("[]"))
            return None, "links to IP addresses are not allowed, use a site name"
        except ValueError:
            pass
        if host == "localhost" or host.endswith((".localhost", ".local", ".lan", ".home", ".internal", ".arpa")):
            return None, "local network addresses are not allowed"
        return u.geturl(), ""

    @staticmethod
    def check_openable_file(path: Path) -> str:
        if path.suffix.lower() in BLOCKED_OPEN_EXTENSIONS:
            return f"opening '{path.suffix}' files is blocked (could execute code)"
        return ""

    @staticmethod
    def check_text_file(path: Path) -> str:
        if path.suffix.lower() not in TEXT_EXTENSIONS:
            return f"only text files are supported ({', '.join(sorted(TEXT_EXTENSIONS))})"
        return ""

    def allowed_app(self, app_id: str) -> dict | None:
        apps = self.settings.get("allowed_apps") or {}
        return apps.get(app_id)

    def content_allowed_process(self, process: str) -> bool:
        allowed = {p.lower() for p in self.settings.get("content_allowed_processes") or []}
        return process.lower() in allowed

    # -------------------------------------------------------------- requests
    def on_request(self, cb: Callable[[str, PendingRequest], None]) -> None:
        self._listeners.append(cb)

    def _emit(self, kind: str, req: PendingRequest) -> None:
        for cb in self._listeners:
            try:
                cb(kind, req)
            except Exception:
                pass

    def create_request(self, tool: str, arguments: dict, perm: Perm, reason: str,
                       conversation_id: int | None = None, meta: dict | None = None) -> PendingRequest:
        req = PendingRequest(id=uuid.uuid4().hex[:12], tool=tool, arguments=arguments, permission=perm.value,
                             reason=reason, created_at=time.time(), conversation_id=conversation_id, meta=meta or {})
        with self._lock:
            self._pending[req.id] = req
        self._emit("created", req)
        return req

    def get_request(self, req_id: str) -> PendingRequest | None:
        with self._lock:
            req = self._pending.get(req_id)
            if req and req.status == "pending" and time.time() - req.created_at > self.request_ttl_s:
                req.status = "expired"
            return req

    def list_requests(self, only_pending: bool = True) -> list[PendingRequest]:
        with self._lock:
            reqs = [self.get_request(i) for i in list(self._pending)]
        return [r for r in reqs if r and (r.status == "pending" or not only_pending)]

    def resolve_request(self, req_id: str, approve: bool) -> PendingRequest:
        with self._lock:
            req = self.get_request(req_id)
            if not req:
                raise KeyError(req_id)
            if req.status != "pending":
                raise ValueError(f"request already {req.status}")
            if approve and self._emergency:
                raise ValueError("emergency stop is active")
            req.status = "approved" if approve else "denied"
        self._emit(req.status, req)
        return req
