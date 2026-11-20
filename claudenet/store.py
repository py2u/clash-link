"""配置与历史报告的本地持久化。"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

APP_DIR_NAME = "ClashLink"
LEGACY_DIR_NAMES = ("AIClash", "AIReady", "ClaudeNetGuard")

DEFAULT_SETTINGS: Dict[str, Any] = {
    "auto_follow": False,
    "watch_interval": 120,
    "last_scan": 0,
    "active_sub": "",
    "active_node": "",
    "api_token": "",
    "tun": False,
    "node_favorites": [],
    "sub_auto_hours": 0,
    "ai_routes": {},
    "ai_clients": [
        {"id": "claude-desktop", "name": "Claude 桌面", "kind": "desktop", "target": "Claude"},
        {"id": "claude-cli", "name": "Claude Code CLI", "kind": "cli", "target": "claude"},
        {"id": "chatgpt-app", "name": "ChatGPT 桌面", "kind": "desktop", "target": "/Applications/ChatGPT.app"},
        {"id": "chatgpt", "name": "ChatGPT 网页", "kind": "url", "target": "https://chatgpt.com/"},
        {"id": "antigravity", "name": "Antigravity", "kind": "desktop", "target": "/Applications/Antigravity.app"},
        {"id": "gemini", "name": "Gemini 网页", "kind": "url", "target": "https://gemini.google.com/"},
    ],
    "allow_lan": False,
    "ipv6": False,
    "log_level": "warning",
    "tun_stack": "gvisor",
    "custom_rules": [],
    "custom_providers": [],
    "dns_enabled": False,
    "dns_mode": "fake-ip",
    "dns_nameservers": ["https://dns.alidns.com/dns-query", "https://doh.pub/dns-query"],
    "dns_fallback": [],
    "dns_fakeip_filter": ["*.lan", "*.local", "localhost.ptlogin2.qq.com"],
    "project_dir": "",
    "mixed_port": 7899,
    "launch_mode": "desktop",
    "claude_app_path": "",
    "claude_cli_path": "",
    "locale": "zh-CN",
    "browser": "auto",
    "disable_ipv6": False,
    "follow_timezone": True,
    "strict_precheck": True,
    "tz_backup": "",
    "theme": "light",
    "lang": "zh",
    "menu_bar": True,
    "app_update_url": "",
    "proxy_backup": None,
    "dns_backup": None,
}

_lock = threading.Lock()


def data_dir() -> Path:
    if os.uname().sysname == "Darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    base = root / APP_DIR_NAME
    if not base.exists():
        for legacy_name in LEGACY_DIR_NAMES:
            legacy = root / legacy_name
            if legacy.exists():
                try:
                    shutil.copytree(legacy, base)
                    break
                except OSError:
                    pass
    base.mkdir(parents=True, exist_ok=True)
    return base


def _atomic_write(path: Path, payload: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def ensure_default_clients(settings: Dict[str, Any]) -> Dict[str, Any]:
    """把后来新增的默认客户端补进老配置（按 id 去重）。"""
    clients = list(settings.get("ai_clients") or [])
    for c in clients:
        if str(c.get("id")) == "chatgpt" and str(c.get("name")) == "ChatGPT" and str(c.get("kind")) == "url":
            c["name"] = "ChatGPT 网页"
    have = {str(c.get("id")) for c in clients}
    for item in DEFAULT_SETTINGS["ai_clients"]:
        if item["id"] not in have:
            clients.append(dict(item))
    order = {str(item["id"]): i for i, item in enumerate(DEFAULT_SETTINGS["ai_clients"])}
    clients.sort(key=lambda c: order.get(str(c.get("id")), 99))
    settings["ai_clients"] = clients
    return settings


def load_settings() -> Dict[str, Any]:
    settings = dict(DEFAULT_SETTINGS)
    path = data_dir() / "settings.json"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        if isinstance(saved, dict):
            settings.update({k: saved[k] for k in DEFAULT_SETTINGS if k in saved})
    except (OSError, ValueError):
        pass
    return ensure_default_clients(settings)


def save_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(DEFAULT_SETTINGS)
    merged.update({k: settings[k] for k in DEFAULT_SETTINGS if k in settings})
    merged["watch_interval"] = max(30, min(3600, int(merged.get("watch_interval") or 120)))
    with _lock:
        _atomic_write(data_dir() / "settings.json", json.dumps(merged, ensure_ascii=False, indent=2))
    return merged


HISTORY_LIMIT = 30


def history_path() -> Path:
    return data_dir() / "history.json"


def node_history_path() -> Path:
    return data_dir() / "node-history.json"


def load_node_history() -> Dict[str, Any]:
    try:
        data = json.loads(node_history_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def append_node_delay(name: str, delay: Any, limit: int = 20) -> None:
    if not name:
        return
    data = load_node_history()
    samples = data.get(name) if isinstance(data.get(name), list) else []
    samples.append([int(time.time()), int(delay) if isinstance(delay, (int, float)) else None])
    data[name] = samples[-limit:]
    try:
        _atomic_write(node_history_path(), json.dumps(data, ensure_ascii=False))
    except OSError:
        pass


def usage_path() -> Path:
    return data_dir() / "usage.json"


_usage_written = 0.0


def load_usage() -> Dict[str, Any]:
    try:
        data = json.loads(usage_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record_usage(down: int, up: int) -> None:
    global _usage_written
    if down <= 0 and up <= 0:
        return
    data = load_usage()
    today = datetime.now().strftime("%Y-%m-%d")
    day = data.get(today) if isinstance(data.get(today), dict) else {"down": 0, "up": 0}
    day["down"] = int(day.get("down", 0)) + max(0, int(down))
    day["up"] = int(day.get("up", 0)) + max(0, int(up))
    data[today] = day
    if len(data) > 400:
        for key in sorted(data)[:-400]:
            data.pop(key, None)
    now = time.time()
    if now - _usage_written < 20:
        return
    _usage_written = now
    try:
        _atomic_write(usage_path(), json.dumps(data, ensure_ascii=False))
    except OSError:
        pass


def load_history() -> list:
    path = history_path()
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return items if isinstance(items, list) else []


def append_history(report: Dict[str, Any]) -> list:
    exit_info = report.get("exit") or {}
    entry = {
        "at": report.get("generated"),
        "score": report.get("score"),
        "verdict": report.get("verdict"),
        "headline": report.get("headline"),
        "duration_ms": report.get("duration_ms"),
        "country_code": exit_info.get("country_code"),
        "country": exit_info.get("country"),
        "city": exit_info.get("city"),
        "flag": exit_info.get("flag"),
        "ip": exit_info.get("ip"),
        "issues": len(report.get("issues") or []),
        "warnings": len(report.get("warnings") or []),
    }
    items = load_history()
    items.insert(0, entry)
    items = items[:HISTORY_LIMIT]
    with _lock:
        _atomic_write(history_path(), json.dumps(items, ensure_ascii=False, indent=2))
    return items


def save_report(report: Dict[str, Any]) -> None:
    with _lock:
        _atomic_write(
            data_dir() / "last-report.json",
            json.dumps({"saved": report.get("generated"), "report": report}, ensure_ascii=False),
        )


def load_report() -> Optional[Dict[str, Any]]:
    path = data_dir() / "last-report.json"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        report = data.get("report")
        return report if isinstance(report, dict) else None
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- 会话记录

EVENT_LIMIT = 40


def session_log_path() -> Path:
    return data_dir() / "session-log.json"


def load_events() -> list:
    try:
        data = json.loads(session_log_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def append_event(kind: str, text: str, extra: Optional[Dict[str, Any]] = None) -> None:
    import time

    with _lock:
        events = load_events()
        events.append({"at": time.time(), "kind": kind, "text": text, "extra": extra or {}})
        events = events[-EVENT_LIMIT:]
        _atomic_write(session_log_path(), json.dumps(events, ensure_ascii=False, indent=2))


def clear_events() -> None:
    with _lock:
        _atomic_write(session_log_path(), "[]")
