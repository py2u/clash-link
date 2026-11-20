"""本地 HTTP 控制台：静态页面 + JSON API + 后台自动巡检。"""
from __future__ import annotations

import hmac
import json
import mimetypes
import os
import re
import socket
import shlex
import subprocess
import socketserver
import signal
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from collections import deque
from urllib.parse import parse_qs, urlparse

from . import __display_name__, __version__, clash, core, kernel, subscription
from .checks import ScanJob, quick_timezone_probe, run_scan
from .store import (
    data_dir,
    load_usage,
    record_usage,
    load_report,
    append_history,
    append_event,
    load_events,
    load_history,
    load_node_history,
    append_node_delay,
    load_settings,
    save_report,
    save_settings,
)
from .ruletest import test as test_rule
from .timezone import (
    apply_timezone,
    compare_timezones,
    has_passwordless,
    install_passwordless,
    system_timezone,
    valid_timezone,
)

WEB_DIR = Path(__file__).resolve().parent / "web"
DEFAULT_PORT = 8788
MAX_BODY = 64 * 1024

API_TOKEN_FILE = "api_token"


def api_token() -> str:
    return str(load_settings().get("api_token") or "")


def require_token() -> bool:
    return os.environ.get("CLASHLINK_REQUIRE_TOKEN", "0") == "1"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
URL_ALLOWED_HOSTS = {"claude.ai", "anthropic.com", "claude.com"}



def remember_local_timezone(settings: Dict[str, Any]) -> None:
    """第一次改时区前，记住系统原来的时区，供「断开」时恢复。"""
    if settings.get("tz_backup"):
        return
    current = system_timezone()
    if not current:
        return
    settings["tz_backup"] = current
    save_settings(settings)


def restore_local_timezone(settings: Dict[str, Any]) -> Optional[str]:
    """断开节点时把系统时区还原成改之前那个；没有记录就什么也不做。"""
    backup = str(settings.get("tz_backup") or "")
    if not backup:
        return None
    if system_timezone() == backup:
        settings["tz_backup"] = ""
        save_settings(settings)
        return None
    result = apply_timezone(backup)
    if not result.ok:
        return None
    settings["tz_backup"] = ""
    save_settings(settings)
    append_event("timezone", f"已恢复本地时区 {backup}")
    return backup

class Watcher(threading.Thread):
    """后台自动巡检：出口时区变了就自动改系统时区。"""

    def __init__(self, settings: Dict[str, Any]) -> None:
        super().__init__(daemon=True, name="claudenet-watcher")
        self.settings = settings
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.last_probe: Optional[Dict[str, Any]] = None
        self.last_action: Optional[Dict[str, Any]] = None
        self.failures = 0
        self.cooldown_until = 0.0

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "enabled": bool(self.settings.get("auto_follow")),
                "interval": int(self.settings.get("watch_interval") or 120),
                "running": not self.stop_event.is_set(),
                "last_probe": self.last_probe,
                "last_action": self.last_action,
            }

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        # 启动后先等一个周期，避免和首次手动检测撞车
        while not self.stop_event.wait(max(15, int(self.settings.get("watch_interval") or 120))):
            if not self.settings.get("auto_follow"):
                continue
            if time.time() < self.cooldown_until:
                continue
            try:
                probe = quick_timezone_probe(self.settings)
            except Exception as exc:  # noqa: BLE001
                with self.lock:
                    self.last_probe = {"error": str(exc), "at": datetime.now().isoformat(timespec="seconds")}
                continue
            with self.lock:
                self.last_probe = probe
            system_tz = probe.get("system_tz") or ""
            exit_tz = probe.get("exit_tz") or ""
            if not exit_tz or exit_tz == system_tz:
                self.failures = 0
                continue
            cmp = probe.get("comparison") or {}
            if cmp.get("same_offset"):
                continue
            remember_local_timezone(self.settings)
            result = apply_timezone(exit_tz)
            with self.lock:
                self.last_action = {
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "from": system_tz,
                    "to": exit_tz,
                    "ok": result.ok,
                    "message": result.message,
                }
            if result.ok:
                self.failures = 0
            else:
                self.failures += 1
                # 用户取消授权或失败时退避，避免反复弹窗
                self.cooldown_until = time.time() + min(1800, 300 * self.failures)


class TrafficMonitor:
    """每秒读一次 /connections 的累计流量，换算成速率并把最近 60 个样本留给前端画曲线。"""

    def __init__(self) -> None:
        self.samples: "deque[Dict[str, Any]]" = deque(maxlen=60)
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._last: Optional[Tuple[float, int, int]] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            if not core.status().get("running"):
                continue
            try:
                data = clash.open_client(timeout=2.0).connections()
            except Exception:  # noqa: BLE001
                continue
            up = int(data.get("uploadTotal") or 0)
            down = int(data.get("downloadTotal") or 0)
            now = time.time()
            with self.lock:
                if self._last is None:
                    up_rate = down_rate = 0.0
                else:
                    dt = max(0.2, now - self._last[0])
                    up_rate = max(0.0, (up - self._last[1]) / dt)
                    down_rate = max(0.0, (down - self._last[2]) / dt)
                delta_up = 0 if self._last is None else max(0, up - self._last[1])
                delta_down = 0 if self._last is None else max(0, down - self._last[2])
                self._last = (now, up, down)
                if delta_up or delta_down:
                    record_usage(delta_down, delta_up)
                self.samples.append(
                    {
                        "t": round(now, 2),
                        "up": int(up_rate),
                        "down": int(down_rate),
                        "up_total": up,
                        "down_total": down,
                    }
                )

    def enable(self) -> None:
        self._last = None
        self.start()

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            items = list(self.samples)
        latest = items[-1] if items else {"up": 0, "down": 0, "up_total": 0, "down_total": 0}
        return {
            "samples": items,
            "up": latest.get("up", 0),
            "down": latest.get("down", 0),
            "up_total": latest.get("up_total", 0),
            "down_total": latest.get("down_total", 0),
        }


class LogBuffer:
    """后台读 mihomo 的 /logs 流，保留最近 400 行。"""

    def __init__(self, limit: int = 400) -> None:
        self.lines: "deque[Dict[str, Any]]" = deque(maxlen=limit)
        self.lock = threading.Lock()
        self._seq = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not core.status().get("running"):
                self._stop.wait(1.5)
                continue
            conn = None
            try:
                conn, resp = clash.open_client(timeout=5.0).open_stream("/logs?level=info")
                while not self._stop.is_set():
                    raw = resp.readline()
                    if not raw:
                        break
                    parsed = clash.parse_log_line(raw.decode("utf-8", errors="replace"))
                    if not parsed:
                        continue
                    with self.lock:
                        self._seq += 1
                        parsed["seq"] = self._seq
                        parsed["at"] = time.time()
                        self.lines.append(parsed)
            except Exception:  # noqa: BLE001
                self._stop.wait(2.0)
            finally:
                try:
                    if conn is not None:
                        conn.close()
                except Exception:  # noqa: BLE001
                    pass

    def snapshot(self, since: int = 0, limit: int = 200) -> Dict[str, Any]:
        with self.lock:
            items = [l for l in self.lines if l["seq"] > since][-limit:]
            seq = self._seq
        return {"items": items, "seq": seq}


class AppState:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.last_report: Optional[Dict[str, Any]] = load_report()
        self.jobs: Dict[str, ScanJob] = {}
        self.current_job: Optional[ScanJob] = None
        self.lock = threading.RLock()
        self.watcher = Watcher(self.settings)
        self.started_at = time.time()
        self.port = 0
        self.last_error = ""
        self.traffic = TrafficMonitor()
        self.logs = LogBuffer()
        if core.adopt():
            clash.set_endpoint_override(core.endpoint())
            self._apply_saved_node()
            self.traffic.enable()
        else:
            # 上次异常退出可能留下 127.0.0.1 DNS，这里兜底还原
            core.restore_dns_from_backup()
            # 上次启用的订阅在应用重启后自动拉起，省得每次手动点「启用」
            def _restore_core() -> None:
                want_tun = bool(self.settings.get("tun"))
                item_id = str(self.settings.get("active_sub") or "")
                item = subscription.find(item_id) if item_id else None
                if item is None:
                    return
                for attempt in range(3):
                    if core.is_busy() or core.status().get("running"):
                        return
                    try:
                        core.start(item, tun=want_tun, preferred_port=int(self.settings.get("mixed_port") or 0))
                        clash.set_endpoint_override(core.endpoint())
                        self._apply_saved_node()
                        return
                    except Exception as exc:  # noqa: BLE001
                        if attempt == 2:
                            try:
                                (core.CORE_DIR / "restore.log").write_text(
                                    f"{datetime.now().isoformat()} {exc}\n", encoding="utf-8"
                                )
                            except OSError:
                                pass
                            return
                        time.sleep(1.5)

            threading.Thread(target=_restore_core, daemon=True).start()
        self.traffic.start()
        self.logs.start()

    def _apply_saved_node(self) -> None:
        name = str(self.settings.get("active_node") or "")
        if not name:
            return
        try:
            client = clash.open_client(timeout=6.0)
            groups = client.groups()
            for group in groups:
                if group.is_selector and any(n.name == name for n in group.nodes):
                    client.select(group.name, name)
                    return
        except Exception:  # noqa: BLE001
            pass

    def start(self) -> None:
        self.watcher.start()
        threading.Thread(target=self._auto_update_loop, daemon=True).start()

    def _auto_update_loop(self) -> None:
        while True:
            time.sleep(600)
            try:
                hours = int(self.settings.get("sub_auto_hours") or 0)
                if hours <= 0:
                    continue
                for item in subscription.load_all():
                    updated = float(item.get("updated_at") or 0)
                    if time.time() - updated < hours * 3600:
                        continue
                    try:
                        fresh = subscription.update(item["id"])
                        append_event("sub", f"自动更新订阅 {fresh.get('name') or item['id']}（{fresh.get('nodes')} 个节点）")
                    except Exception as exc:  # noqa: BLE001
                        append_event("sub", f"自动更新失败：{exc}")
            except Exception:  # noqa: BLE001
                continue

    def start_scan(self) -> ScanJob:
        with self.lock:
            if self.current_job and self.current_job.state == "running":
                return self.current_job
            job = ScanJob()
            self.jobs[job.id] = job
            self.current_job = job
            # 只保留最近 8 个任务
            if len(self.jobs) > 8:
                for key in list(self.jobs)[:-8]:
                    self.jobs.pop(key, None)
        threading.Thread(target=self._run_job, args=(job,), daemon=True).start()
        return job

    def _run_job(self, job: ScanJob) -> None:
        try:
            report = run_scan(job, dict(self.settings))
            self.last_report = report
            save_report(report)
            append_history(report)
            self.settings["last_scan"] = time.time()
            save_settings(self.settings)
        except Exception as exc:  # noqa: BLE001
            with job.lock:
                job.state = "error"
                job.error = f"{type(exc).__name__}: {exc}"
                job.finished = time.time()

    def settings_payload(self) -> Dict[str, Any]:
        with self.lock:
            return dict(self.settings)

    def update_settings(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self.lock:
            if "auto_follow" in patch:
                self.settings["auto_follow"] = bool(patch["auto_follow"])
            if "menu_bar" in patch:
                self.settings["menu_bar"] = bool(patch["menu_bar"])
            if "app_update_url" in patch:
                self.settings["app_update_url"] = str(patch["app_update_url"] or "").strip()[:400]
            if "lang" in patch:
                lang = str(patch["lang"] or "")
                if lang in ("zh", "en"):
                    self.settings["lang"] = lang
            if "theme" in patch:
                theme = str(patch["theme"] or "")
                if theme in ("dark", "light", "auto"):
                    self.settings["theme"] = theme
            if "sub_auto_hours" in patch:
                try:
                    self.settings["sub_auto_hours"] = max(0, min(168, int(patch["sub_auto_hours"])))
                except (TypeError, ValueError):
                    pass
            if "watch_interval" in patch:
                try:
                    self.settings["watch_interval"] = max(30, min(3600, int(patch["watch_interval"])))
                except (TypeError, ValueError):
                    pass
            if "mixed_port" in patch:
                try:
                    port = int(patch["mixed_port"])
                    if 1024 <= port <= 65535:
                        self.settings["mixed_port"] = port
                except (TypeError, ValueError):
                    pass
            if "allow_lan" in patch:
                self.settings["allow_lan"] = bool(patch["allow_lan"])
            if "ipv6" in patch:
                self.settings["ipv6"] = bool(patch["ipv6"])
            if "log_level" in patch:
                level = str(patch["log_level"] or "")
                if level in ("debug", "info", "warning", "error", "silent"):
                    self.settings["log_level"] = level
            if "dns_enabled" in patch:
                self.settings["dns_enabled"] = bool(patch["dns_enabled"])
            if "dns_mode" in patch:
                mode = str(patch["dns_mode"] or "")
                if mode in ("fake-ip", "redir-host", "normal"):
                    self.settings["dns_mode"] = mode
            for key in ("dns_nameservers", "dns_fallback", "dns_fakeip_filter"):
                if key in patch:
                    value = patch[key]
                    if isinstance(value, list):
                        items = [str(item).strip() for item in value if str(item).strip()]
                        self.settings[key] = items[:24]
            if "tun_stack" in patch:
                stack = str(patch["tun_stack"] or "")
                if stack in ("gvisor", "system", "mixed"):
                    self.settings["tun_stack"] = stack
            if "launch_mode" in patch:
                mode = str(patch["launch_mode"] or "")
                if mode in ("desktop", "cli", "proxy"):
                    self.settings["launch_mode"] = mode
            if "locale" in patch:
                locale = str(patch["locale"] or "")
                if locale in ("zh-CN", "zh-TW", "en-US", "ja-JP"):
                    self.settings["locale"] = locale
            if "browser" in patch:
                browser = str(patch["browser"] or "")
                if browser in ("auto", "chrome", "safari", "edge"):
                    self.settings["browser"] = browser
            for key in ("claude_app_path", "claude_cli_path"):
                if key in patch:
                    self.settings[key] = str(patch[key] or "").strip()
            for key in ("disable_ipv6", "follow_timezone", "strict_precheck"):
                if key in patch:
                    self.settings[key] = bool(patch[key])
            self.settings = save_settings(self.settings)
            self.watcher.settings = self.settings
            return dict(self.settings)


def make_handler(state: AppState):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"Clash Link/{__version__}"
        protocol_version = "HTTP/1.1"

        # ------------------------------------------------------------------ #
        def log_message(self, fmt: str, *args: Any) -> None:  # 安静些
            if os.environ.get("AICLASH_DEBUG") or os.environ.get("CLAUDENET_DEBUG"):
                super().log_message(fmt, *args)

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ALLOWED_HOSTS

        def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _json(self, payload: Any, code: int = 200) -> None:
            self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

        def _read_json(self) -> Dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return {}
            if length <= 0 or length > MAX_BODY:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
                return data if isinstance(data, dict) else {}
            except (ValueError, UnicodeDecodeError):
                return {}

        # ------------------------------------------------------------------ #
        def _token_ok(self, path: str) -> bool:
            if not require_token() or path == "/api/ping":
                return True
            expected = self.server.state.settings.get("api_token") or ""
            if not expected:
                return True
            got = self.headers.get("X-ClashLink-Token") or ""
            if not got:
                query = parse_qs(urlparse(self.path).query)
                got = (query.get("token") or [""])[0]
            return hmac.compare_digest(str(got), str(expected))

        def do_GET(self) -> None:  # noqa: N802
            if not self._host_ok():
                return self._json({"error": "forbidden host"}, 403)
            path = urlparse(self.path).path
            if path.startswith("/api") and not self._token_ok(path):
                return self._json({"error": "unauthorized"}, 401)
            try:
                if path == "/api/ping":
                    return self._json(
                        {
                            "app": "clashlink",
                            "version": __version__,
                            "display_name": __display_name__,
                            "pid": os.getpid(),
                        }
                    )
                if path == "/api/timezone/privilege":
                    return self._json({"passwordless": has_passwordless()})
                if path == "/api/kernel":
                    return self._json(self._kernel_status())
                if path == "/api/clients":
                    return self._json({"clients": self.server.state.settings.get("ai_clients") or []})
                if path == "/api/state":
                    return self._json(self._state_payload())
                if path.startswith("/api/scan/"):
                    job_id = path.rsplit("/", 1)[-1]
                    job = state.jobs.get(job_id)
                    if not job:
                        return self._json({"error": "not found"}, 404)
                    return self._json(job.snapshot())
                if path == "/api/report.json":
                    report = state.last_report or {}
                    body = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")
                    return self._send(
                        200,
                        body,
                        "application/json; charset=utf-8",
                    )
                if path == "/api/settings":
                    return self._json(state.settings_payload())
                if path == "/api/app/update":
                    url = (state.settings.get("app_update_url") or "").strip()
                    if not url:
                        return self._json({"ok": False, "message": "没有配置更新源（设置 → 应用更新）"}, 400)
                    res = http_request(url, proxy=None, timeout=12.0)
                    if res.error or res.status >= 400:
                        return self._json({"ok": False, "message": f"拉取更新信息失败：{res.error or res.status}"}, 400)
                    data = res.json() or {}
                    latest = str(data.get("version") or "").strip()
                    current = __version__
                    def _key(v):
                        import re as _re
                        return [int(x) for x in _re.findall(r"\d+", v)[:3]] or [0]
                    return self._json({
                        "ok": True,
                        "current": current,
                        "latest": latest,
                        "has_update": bool(latest) and _key(latest) > _key(current),
                        "url": data.get("url") or "",
                        "notes": data.get("notes") or "",
                    })
                if path == "/api/nodes":
                    snap = clash.snapshot()
                    snap["favorites"] = list(state.settings.get("node_favorites") or [])
                    snap["history"] = load_node_history()
                    return self._json(snap)
                if path == "/api/session":
                    backup = core.proxy_backup() or {}
                    return self._json(
                        {
                            "events": load_events()[-20:],
                            "backup": bool(backup),
                            "backup_service": backup.get("service") or "",
                            "proxy": core.system_proxy(),
                            "core": core.status(),
                        }
                    )
                if path == "/api/history":
                    return self._json({"items": load_history()})
                if path == "/api/clash/configs":
                    data = clash.open_client().configs()
                    return self._json(
                        {
                            "mode": data.get("mode") or "rule",
                            "mode_list": ["rule", "global", "direct"],
                            "port": data.get("mixed-port") or data.get("port") or 0,
                        }
                    )
                if path == "/api/clash/connections":
                    data = clash.open_client(timeout=3.0).connections()
                    return self._json(clash.summarize_connections(data))
                if path == "/api/clash/providers":
                    items = clash.open_client(timeout=3.0).providers()
                    return self._json({"count": len(items), "items": items})
                if path == "/api/clash/rules":
                    items = clash.open_client(timeout=3.0).rules()
                    return self._json({"count": len(items), "items": items})
                if path == "/api/usage":
                    data = load_usage()
                    today = datetime.now().strftime("%Y-%m-%d")
                    month = today[:7]
                    t = data.get(today) or {"down": 0, "up": 0}
                    m_down = sum(int(v.get("down", 0)) for k, v in data.items() if k.startswith(month))
                    m_up = sum(int(v.get("up", 0)) for k, v in data.items() if k.startswith(month))
                    recent = [{"date": k, **v} for k, v in sorted(data.items())[-7:]]
                    return self._json({"today": t, "month": {"down": m_down, "up": m_up}, "recent": recent})
                if path == "/api/traffic":
                    return self._json(state.traffic.snapshot())
                if path == "/api/logs":
                    qs = parse_qs(urlparse(self.path).query)
                    since = int(qs.get("since", ["0"])[0] or 0)
                    limit = min(400, int(qs.get("limit", ["200"])[0] or 200))
                    return self._json(state.logs.snapshot(since, limit))
                if path == "/api/subscriptions":
                    return self._json(
                        {
                            "items": subscription.public_all(),
                            "core": core.status(),
                            "system_proxy": core.system_proxy(),
                        }
                    )
                return self._serve_static(path)
            except Exception as exc:  # noqa: BLE001
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

        def _kernel_status(self) -> Dict[str, Any]:
            from .net import detect_proxy, http_request

            return kernel.status(proxy=detect_proxy().proxy)

        def _kernel_update(self) -> Dict[str, Any]:
            from .net import detect_proxy
            from . import subscription as subs_mod

            state_obj = self.server.state
            settings = state_obj.settings
            active = str(settings.get("active_sub") or "")
            was_running = bool(core.status().get("running"))
            want_tun = bool(settings.get("tun"))
            if was_running:
                core.stop()
            res = kernel.update(proxy=detect_proxy().proxy)
            if not res.get("ok"):
                if was_running and active:
                    item = subs_mod.find(active)
                    if item:
                        core.start(item, tun=want_tun)
                        clash.set_endpoint_override(core.endpoint())
                return res
            if was_running and active:
                item = subs_mod.find(active)
                if item:
                    core.start(item, tun=want_tun)
                    clash.set_endpoint_override(core.endpoint())
            append_event("kernel", f"内核更新到 {res.get('version')}")
            return {**res, "core": core.status()}

        def _open_client(self, payload: Dict[str, Any]) -> Dict[str, Any]:
            settings = self.server.state.settings
            cid = str(payload.get("id") or "")
            clients = settings.get("ai_clients") or []
            item = next((c for c in clients if str(c.get("id")) == cid), None)
            if not item:
                return {"ok": False, "message": "客户端不存在"}
            kind = str(item.get("kind") or "desktop")
            target = str(item.get("target") or "").strip()
            if kind == "proxy":
                return {"ok": True, "message": "只准备网络，未启动客户端"}
            if kind == "cli":
                project = str(settings.get("project_dir") or "").strip()
                cd = f"cd {shlex.quote(project)} && " if project else ""
                lang = {"zh-CN": "zh_CN.UTF-8", "zh-TW": "zh_TW.UTF-8", "en-US": "en_US.UTF-8", "ja-JP": "ja_JP.UTF-8"}.get(
                    str(settings.get("locale") or "zh-CN"), "en_US.UTF-8"
                )
                script = (
                    'tell application "Terminal" to do script '
                    f'"export LANG={lang} LC_ALL={lang}; {cd}{shlex.quote(target or 'claude')}"'
                )
                try:
                    subprocess.run(["/usr/bin/osascript", "-e", script], check=True, timeout=20)
                    return {"ok": True, "message": f"已在终端启动 {item.get('name')}"}
                except Exception as exc:  # noqa: BLE001
                    return {"ok": False, "message": f"启动失败：{exc}"}
            if kind == "url":
                try:
                    subprocess.run(["/usr/bin/open", target], check=True, timeout=20)
                    return {"ok": True, "message": f"已打开 {item.get('name')}"}
                except Exception as exc:  # noqa: BLE001
                    return {"ok": False, "message": f"打开失败：{exc}"}
            # desktop
            app = target or "Claude"
            if app.endswith(".app") and os.path.isdir(app):
                cmd = ["/usr/bin/open", "-a", app]
            else:
                cmd = ["/usr/bin/open", "-a", app]
            try:
                subprocess.run(cmd, check=True, timeout=20)
                return {"ok": True, "message": f"已打开 {item.get('name')}"}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "message": f"打开失败：{exc}"}

        def _save_clients(self, payload: Dict[str, Any]) -> Dict[str, Any]:
            settings = self.server.state.settings
            clients = list(settings.get("ai_clients") or [])
            action = str(payload.get("action") or "add")
            item = payload.get("item") or {}
            cid = str(item.get("id") or "").strip()
            if action == "remove":
                clients = [c for c in clients if str(c.get("id")) != cid]
            else:
                name = str(item.get("name") or "").strip() or "AI 客户端"
                kind = str(item.get("kind") or "desktop")
                if kind not in ("desktop", "cli", "url", "proxy"):
                    return {"ok": False, "message": "类型只能是 desktop / cli / url / proxy"}
                target = str(item.get("target") or "").strip()
                if not cid:
                    cid = "c-" + str(int(time.time() * 1000))
                row = {"id": cid, "name": name[:40], "kind": kind, "target": target}
                idx = next((i for i, c in enumerate(clients) if str(c.get("id")) == cid), -1)
                if idx >= 0:
                    clients[idx] = row
                else:
                    clients.append(row)
            settings["ai_clients"] = clients
            save_settings(settings)
            self.server.state.settings = settings
            return {"ok": True, "clients": clients}

        def _open_claude(self) -> Dict[str, Any]:
            settings = self.server.state.settings
            mode = str(settings.get("launch_mode") or "desktop")
            if mode == "proxy":
                return {"ok": True, "message": "仅准备网络，未启动客户端"}
            if mode == "cli":
                project = str(settings.get("project_dir") or "").strip()
                cli = str(settings.get("claude_cli_path") or "").strip() or "claude"
                cd = f"cd {shlex.quote(project)} && " if project else ""
                lang = {"zh-CN": "zh_CN.UTF-8", "zh-TW": "zh_TW.UTF-8", "en-US": "en_US.UTF-8", "ja-JP": "ja_JP.UTF-8"}.get(
                    str(settings.get("locale") or "zh-CN"), "en_US.UTF-8"
                )
                script = (
                    'tell application "Terminal" to do script '
                    f'"export LANG={lang} LC_ALL={lang}; {cd}{shlex.quote(cli)}"'
                )
                try:
                    subprocess.run(["/usr/bin/osascript", "-e", script], check=True, timeout=20)
                    return {"ok": True, "message": "已在终端启动 Claude Code CLI"}
                except Exception as exc:  # noqa: BLE001
                    return {"ok": False, "message": f"启动 CLI 失败：{exc}"}
            app = str(settings.get("claude_app_path") or "").strip()
            if app and os.path.isdir(app):
                try:
                    subprocess.run(["/usr/bin/open", "-a", app], check=True, timeout=20)
                    return {"ok": True, "message": "已打开 Claude 桌面端"}
                except Exception as exc:  # noqa: BLE001
                    return {"ok": False, "message": f"打开失败：{exc}"}
            if not os.path.isdir("/Applications/Claude.app"):
                return {"ok": False, "message": "没有找到 /Applications/Claude.app，请先安装 Claude 桌面端"}
            try:
                subprocess.run(["open", "-a", "Claude"], check=True, timeout=20)
                return {"ok": True, "message": "已打开 Claude 桌面端"}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "message": f"打开失败：{exc}"}

        def _release_system_proxy(self) -> None:
                """内核要停了，先把系统代理收回来，否则浏览器会指向死端口。"""
                try:
                    proxy = core.system_proxy()
                    mixed = int(core.status().get("mixed_port") or 0)
                    if proxy.get("enabled") and int(proxy.get("port") or 0) == mixed:
                        core.set_system_proxy(False)
                except Exception:  # noqa: BLE001
                    pass

        def do_POST(self) -> None:  # noqa: N802
            if not self._host_ok():
                return self._json({"error": "forbidden host"}, 403)
            path = urlparse(self.path).path
            if not self._token_ok(path):
                return self._json({"error": "unauthorized"}, 401)
            payload = self._read_json()
            try:
                if path == "/api/scan":
                    job = state.start_scan()
                    return self._json({"job_id": job.id, "state": job.state})
                if path == "/api/settings":
                    return self._json({"settings": state.update_settings(payload)})
                if path == "/api/clash/mode":
                    mode = str(payload.get("mode") or "")
                    clash.open_client().set_mode(mode)
                    return self._json({"ok": True, "mode": mode})
                if path == "/api/clash/options":
                    return self._json(self._apply_kernel_options(payload))
                if path == "/api/clash/test-rule":
                    target = str(payload.get("target") or "").strip()
                    return self._json(test_rule(target))
                if path == "/api/clash/reload":
                    clash.open_client().reload_config(str(core.CONFIG))
                    return self._json({"ok": True})
                if path == "/api/clash/providers/update":
                    name = str(payload.get("name") or "").strip()
                    if not name:
                        return self._json({"ok": False, "message": "缺少规则集名称"}, 400)
                    clash.open_client(timeout=10.0).update_provider(name)
                    return self._json({"ok": True, "name": name})
                if path == "/api/clash/providers/custom":
                    return self._json(self._save_custom_providers(payload))
                if path == "/api/clash/rules/custom":
                    return self._json(self._save_custom_rules(payload))
                if path == "/api/clash/connections/close":
                    client = clash.open_client()
                    if payload.get("all"):
                        client.close_all_connections()
                    else:
                        client.close_connection(str(payload.get("id") or ""))
                    return self._json({"ok": True})
                if path == "/api/timezone/privilege":
                    ok = install_passwordless()
                    return self._json({
                        "ok": ok.ok,
                        "message": ok.message,
                        "passwordless": has_passwordless(),
                    })
                if path == "/api/timezone":
                    tz = str(payload.get("tz") or "")
                    if not valid_timezone(tz):
                        return self._json({"ok": False, "message": "无效的时区名称"}, 400)
                    remember_local_timezone(state.settings)
                    result = apply_timezone(tz)
                    if result.ok:
                        append_event("timezone", f"系统时区设为 {result.timezone}")
                    if result.ok and state.last_report:
                        state.last_report["timezone"]["system"] = result.timezone
                        state.last_report["timezone"]["comparison"] = compare_timezones(
                            result.timezone, state.last_report["timezone"].get("exit") or ""
                        )
                        save_report(state.last_report)
                    return self._json(result.to_dict())
                if path == "/api/timezone/follow":
                    probe = quick_timezone_probe(state.settings)
                    exit_tz = str(probe.get("exit_tz") or "")
                    current = str(probe.get("system_tz") or "")
                    cmp = probe.get("comparison") or {}
                    if not exit_tz or not valid_timezone(exit_tz):
                        return self._json({"ok": False, "changed": False, "message": "没取到出口时区，稍后再试"}, 400)
                    if cmp.get("same_offset"):
                        return self._json({"ok": True, "changed": False, "timezone": current, "exit": exit_tz})
                    if payload.get("dry"):
                        return self._json({"ok": True, "changed": False, "would_change": True, "from": current, "to": exit_tz})
                    remember_local_timezone(state.settings)
                    result = apply_timezone(exit_tz)
                    if result.ok:
                        append_event("timezone", f"跟随出口时区设为 {result.timezone}")
                        if state.last_report and state.last_report.get("timezone"):
                            state.last_report["timezone"]["system"] = result.timezone
                            state.last_report["timezone"]["comparison"] = compare_timezones(
                                result.timezone, state.last_report["timezone"].get("exit") or ""
                            )
                            save_report(state.last_report)
                    return self._json({
                        "ok": result.ok,
                        "changed": result.ok,
                        "timezone": result.timezone,
                        "from": current,
                        "message": result.message,
                    })
                if path == "/api/timezone/local":
                    tz = str(payload.get("tz") or "").strip()
                    if tz and not valid_timezone(tz):
                        return self._json({"ok": False, "message": "无效的时区名称"}, 400)
                    if tz and system_timezone() == tz and not state.settings.get("tz_backup"):
                        pass
                    state.settings["tz_backup"] = tz
                    save_settings(state.settings)
                    append_event("timezone", f"本地时区记为 {tz}" if tz else "已清除本地时区记录")
                    return self._json({"ok": True, "tz_backup": tz})
                if path == "/api/nodes/select":
                    group = str(payload.get("group") or "").strip()
                    name = str(payload.get("name") or "").strip()
                    if not group or not name:
                        return self._json({"ok": False, "message": "缺少 group 或 name"}, 400)
                    try:
                        client = clash.open_client(timeout=5.0)
                        client.select(group, name)
                    except clash.ClashError as exc:
                        return self._json({"ok": False, "message": str(exc)}, 400)
                    st = self.server.state
                    st.settings["active_node"] = name
                    save_settings(st.settings)
                    return self._json({"ok": True, "group": group, "now": name})
                if path == "/api/nodes/favorite":
                    name = str(payload.get("name") or "").strip()
                    on = bool(payload.get("on"))
                    favs = [x for x in (state.settings.get("node_favorites") or []) if x != name]
                    if on and name:
                        favs.append(name)
                    state.settings["node_favorites"] = favs
                    save_settings(state.settings)
                    return self._json({"ok": True, "favorites": favs})
                if path == "/api/nodes/delay":
                    name = str(payload.get("name") or "").strip()
                    if not name:
                        return self._json({"ok": False, "message": "缺少节点名"}, 400)
                    try:
                        client = clash.open_client(timeout=8.0)
                        value = client.delay(name, timeout_ms=4000)
                    except clash.ClashError as exc:
                        return self._json({"ok": False, "message": str(exc)}, 400)
                    if value is not None:
                        append_node_delay(name, value)
                    return self._json({"ok": True, "name": name, "delay": value})
                if path == "/api/nodes/test":
                    group_name = str(payload.get("group") or "").strip()
                    try:
                        client = clash.open_client(timeout=8.0)
                        groups = client.groups()
                    except clash.ClashError as exc:
                        return self._json({"ok": False, "message": str(exc)}, 400)
                    group = next((g for g in groups if g.name == group_name), None)
                    if group is None:
                        selectors = [g for g in groups if g.is_selector]
                        group = selectors[0] if selectors else (groups[0] if groups else None)
                    if group is None:
                        return self._json({"ok": False, "message": "没有可测速的节点组"}, 400)
                    delays = client.test_group(group)
                    for node_name, value in (delays or {}).items():
                        if value is not None:
                            append_node_delay(str(node_name), value)
                    return self._json(
                        {
                            "ok": True,
                            "group": group.name,
                            "delays": delays,
                            "tested_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        }
                    )
                if path == "/api/subscriptions/add":
                    url = str(payload.get("url") or "").strip()
                    name = str(payload.get("name") or "").strip()
                    if not url:
                        return self._json({"ok": False, "message": "请填写订阅链接"}, 400)
                    try:
                        item = subscription.add(url, name)
                    except RuntimeError as exc:
                        return self._json({"ok": False, "message": str(exc)}, 400)
                    return self._json({"ok": True, "item": subscription.public(item)})
                if path == "/api/subscriptions/delete":
                    item_id = str(payload.get("id") or "").strip()
                    active = core.status().get("active")
                    if active and active == item_id:
                        self._release_system_proxy()
                        core.stop()
                        clash.set_endpoint_override(None)
                        self.server.state.settings["active_sub"] = ""
                        save_settings(self.server.state.settings)
                    removed = subscription.remove(item_id)
                    return self._json({"ok": removed})
                if path == "/api/subscriptions/update":
                    item_id = str(payload.get("id") or "").strip()
                    try:
                        item = subscription.update(item_id)
                    except RuntimeError as exc:
                        return self._json({"ok": False, "message": str(exc)}, 400)
                    if core.status().get("active") == item_id and core.status().get("running"):
                        try:
                            core.start(item, tun=bool(state.settings.get("tun")),
                                       preferred_port=int(state.settings.get("mixed_port") or 0))
                            clash.set_endpoint_override(core.endpoint())
                        except Exception:  # noqa: BLE001
                            pass
                    append_event("sub", f"更新订阅 {item.get('name')}：{item.get('nodes')} 个节点")
                    return self._json({"ok": True, "item": subscription.public(item)})
                if path == "/api/subscriptions/rename":
                    item_id = str(payload.get("id") or "").strip()
                    try:
                        item = subscription.rename(item_id, str(payload.get("name") or ""))
                    except RuntimeError as exc:
                        return self._json({"ok": False, "message": str(exc)}, 400)
                    return self._json({"ok": True, "item": subscription.public(item)})
                if path == "/api/subscriptions/activate":
                    item_id = str(payload.get("id") or "").strip()
                    item = subscription.find(item_id)
                    if item is None:
                        return self._json({"ok": False, "message": "订阅不存在"}, 404)
                    try:
                        snapshot = core.start(item, preferred_port=int(state.settings.get("mixed_port") or 0))
                    except RuntimeError as exc:
                        return self._json({"ok": False, "message": str(exc)}, 400)
                    clash.set_endpoint_override(core.endpoint())
                    append_event("core", f"启用订阅「{item.get('name')}」，内核端口 {snapshot.get('mixed_port')}")
                    self.server.state.settings["active_sub"] = item_id
                    save_settings(self.server.state.settings)
                    return self._json({"ok": True, "core": snapshot})

                if path == "/api/project":
                    value = str(payload.get("path") or "").strip()
                    target = Path(value).expanduser() if value else None
                    if target is not None and not target.is_dir():
                        return self._json({"ok": False, "message": "目录不存在"}, 400)
                    st = self.server.state
                    st.settings["project_dir"] = str(target) if target else ""
                    save_settings(st.settings)
                    return self._json({"ok": True, "project_dir": st.settings["project_dir"]})

                if path == "/api/project/browse":
                    try:
                        out = subprocess.run(
                            ["/usr/bin/osascript", "-e", "POSIX path of (choose folder)"],
                            capture_output=True, text=True, timeout=180,
                        )
                    except subprocess.TimeoutExpired:
                        return self._json({"ok": False, "message": "选择目录超时"}, 400)
                    if out.returncode != 0:
                        return self._json({"ok": False, "message": "已取消"}, 400)
                    folder = out.stdout.strip().rstrip("/")
                    st = self.server.state
                    st.settings["project_dir"] = folder
                    save_settings(st.settings)
                    return self._json({"ok": True, "project_dir": folder})

                if path == "/api/open-url":
                    url = str(payload.get("url") or "")
                    host = urlparse(url).hostname or ""
                    if not any(host == h or host.endswith("." + h) for h in URL_ALLOWED_HOSTS):
                        return self._json({"ok": False, "message": "不允许的地址"}, 400)
                    settings = self.server.state.settings
                    browser = str(settings.get("browser") or "auto")
                    locale = str(settings.get("locale") or "zh-CN")
                    apps = {"chrome": "Google Chrome", "safari": "Safari", "edge": "Microsoft Edge"}
                    app = apps.get(browser)
                    if not app:
                        app = "Google Chrome" if os.path.isdir("/Applications/Google Chrome.app") else ""
                    if app in ("Google Chrome", "Microsoft Edge"):
                        # Chromium 系支持 --lang，用它切核对窗口的界面语言
                        subprocess.Popen(["/usr/bin/open", "-na", app, "--args", f"--lang={locale}", "--new-window", url])
                    elif app:
                        subprocess.Popen(["/usr/bin/open", "-a", app, url])
                    else:
                        subprocess.Popen(["/usr/bin/open", url])
                    return self._json({"ok": True})


                if path == "/api/pick-file":
                    kind = str(payload.get("kind") or "file")
                    if kind == "app":
                        script = 'POSIX path of (choose application default location POSIX file "/Applications")'
                    else:
                        script = 'POSIX path of (choose file)'
                    try:
                        out = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=180)
                    except subprocess.TimeoutExpired:
                        return self._json({"ok": False, "message": "选择超时"}, 400)
                    if out.returncode != 0:
                        return self._json({"ok": False, "message": "已取消"}, 400)
                    return self._json({"ok": True, "path": out.stdout.strip().rstrip("/")})

                if path == "/api/env/ipv6":
                    turn_on = bool(payload.get("on"))
                    try:
                        info = core.set_ipv6(turn_on)
                    except RuntimeError as exc:
                        return self._json({"ok": False, "message": str(exc)}, 400)
                    append_event("ipv6", "已临时关闭物理网卡 IPv6" if not turn_on else "已恢复物理网卡 IPv6")
                    return self._json({"ok": True, "ipv6": info})

                if path == "/api/core/tun":
                    turn_on = bool(payload.get("on"))
                    settings = self.server.state.settings
                    item = subscription.find(str(settings.get("active_sub") or ""))
                    if item is None:
                        return self._json({"ok": False, "message": "先启用一条订阅，再开 TUN"}, 400)
                    settings["tun"] = turn_on
                    save_settings(settings)
                    try:
                        if (core.system_proxy() or {}).get("enabled"):
                            core.set_system_proxy(False)
                        core.stop()
                        clash.set_endpoint_override(None)
                        snapshot = core.start(item, tun=turn_on, preferred_port=int(state.settings.get("mixed_port") or 0))
                        if turn_on and not snapshot.get("tun"):
                            core.stop()
                            snapshot = core.start(item, tun=True)
                        if turn_on and not snapshot.get("tun"):
                            raise RuntimeError("TUN 没有起来（内核不是 root 或虚拟网卡未就绪），请再点一次")
                        state.last_error = ""
                    except RuntimeError as exc:
                        state.last_error = str(exc)
                        append_event("error", f"TUN 启动失败：{exc}")
                        if turn_on:
                            try:
                                core.start(item, tun=False)
                                clash.set_endpoint_override(core.endpoint())
                            except Exception:  # noqa: BLE001
                                pass
                        return self._json({"ok": False, "message": str(exc)}, 400)
                    clash.set_endpoint_override(core.endpoint())
                    if not turn_on:
                        restore_local_timezone(settings)
                    append_event("tun", "开启 TUN（接管整机流量）" if turn_on else "关闭 TUN，回到混合端口代理")
                    return self._json({"ok": True, "core": snapshot})
                if path == "/api/core/stop":
                    self._release_system_proxy()
                    restore_local_timezone(state.settings)
                    core.stop()
                    self.server.state.settings["active_sub"] = ""
                    save_settings(self.server.state.settings)
                    clash.set_endpoint_override(None)
                    return self._json({"ok": True, "core": core.status()})
                if path == "/api/system-proxy":
                    turn_on = bool(payload.get("on"))
                    try:
                        state_payload = core.set_system_proxy(turn_on)
                    except RuntimeError as exc:
                        state.last_error = str(exc)
                        append_event("error", f"TUN 启动失败：{exc}")
                        if turn_on:
                            try:
                                core.start(item, tun=False)
                                clash.set_endpoint_override(core.endpoint())
                            except Exception:  # noqa: BLE001
                                pass
                        return self._json({"ok": False, "message": str(exc)}, 400)
                    if not turn_on:
                        restore_local_timezone(state.settings)
                    append_event(
                        "proxy",
                        "接管系统代理" if turn_on else "恢复系统代理",
                        {"port": state_payload.get("port")},
                    )
                    return self._json({"ok": True, "system_proxy": state_payload})
                if path == "/api/session/restore":
                    messages = []
                    try:
                        core.set_system_proxy(False)
                        messages.append("系统代理已还原")
                    except Exception as exc:  # noqa: BLE001
                        messages.append(f"还原系统代理失败：{exc}")
                    try:
                        core.stop()
                        clash.set_endpoint_override(None)
                        self.server.state.settings["active_sub"] = ""
                        save_settings(self.server.state.settings)
                        messages.append("内核已停止")
                    except Exception as exc:  # noqa: BLE001
                        messages.append(f"停止内核失败：{exc}")
                    append_event("restore", "恢复临时设置：" + "；".join(messages))
                    return self._json({"ok": True, "message": "；".join(messages), "core": core.status()})
                if path == "/api/kernel/update":
                    return self._json(self._kernel_update())
                if path == "/api/clients/open":
                    return self._json(self._open_client(payload))
                if path == "/api/clients/route":
                    routes = payload.get("routes")
                    if not isinstance(routes, dict):
                        return self._json({"ok": False, "message": "routes 必须是对象"}, 400)
                    clean = {str(k): str(v) for k, v in routes.items() if str(v or "").strip()}
                    state.settings["ai_routes"] = clean
                    save_settings(state.settings)
                    return self._json({"ok": True, "routes": clean, "restart_hint": "重新连接后生效"})
                if path == "/api/clients/save":
                    return self._json(self._save_clients(payload))
                if path == "/api/open-claude":
                    return self._json(self._open_claude())
                if path == "/api/repair":
                    return self._json(self._repair_network())
                if path == "/api/quit":
                    # 退出前把系统代理还回去，否则指针留在已关闭的 7899 上会断网
                    proxy = core.system_proxy()
                    if proxy.get("enabled") and int(proxy.get("port") or 0) == int(core.status().get("mixed_port") or 0):
                        try:
                            core.set_system_proxy(False)
                        except Exception:  # noqa: BLE001
                            pass
                    core.stop()
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return self._json({"ok": True})
                return self._json({"error": "not found"}, 404)
            except Exception as exc:  # noqa: BLE001
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

        # ------------------------------------------------------------------ #


        def _save_custom_providers(self, payload: Dict[str, Any]) -> Dict[str, Any]:
            raw = str(payload.get("text") or "")
            items: List[Dict[str, str]] = []
            bad = ""
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split()
                if len(parts) < 3:
                    bad = line
                    break
                name, behavior, url = parts[0], parts[1], " ".join(parts[2:])
                if not url.startswith("http"):
                    bad = line
                    break
                items.append({"name": name, "behavior": behavior, "url": url})
            if bad:
                return {"ok": False, "message": "格式应为：名称 domain|ipcidr|classical URL"}
            with state.lock:
                state.settings["custom_providers"] = items
                save_settings(state.settings)
            if core.status().get("running"):
                try:
                    core.rewrite_config()
                    clash.open_client().reload_config(str(core.CONFIG))
                except Exception as exc:  # noqa: BLE001
                    return {"ok": False, "message": f"已保存，但热重载失败：{exc}"}
            return {"ok": True, "providers": items, "count": len(items)}

        def _restart_core(self) -> None:
            item = subscription.find(str(state.settings.get("active_sub") or ""))
            if item is None:
                return
            tun = bool(state.settings.get("tun"))
            core.stop()
            core.start(
                item,
                tun=tun,
                preferred_port=int(state.settings.get("mixed_port") or 0),
            )
            clash.set_endpoint_override(core.endpoint())

        def _apply_kernel_options(self, payload: Dict[str, Any]) -> Dict[str, Any]:
            before = dict(state.settings)
            state.update_settings(payload)
            live: Dict[str, Any] = {}
            dns_keys = ("dns_enabled", "dns_mode", "dns_nameservers", "dns_fallback", "dns_fakeip_filter")
            if any(key in payload for key in dns_keys):
                try:
                    core.rewrite_config()
                    clash.open_client().reload_config(str(core.CONFIG))
                except Exception:  # noqa: BLE001
                    pass
            for key in ("allow_lan", "ipv6", "log_level"):
                if key in payload:
                    live[key.replace("_", "-")] = state.settings.get(key)
            if live:
                try:
                    clash.open_client().patch_configs(live)
                except Exception:  # noqa: BLE001
                    pass
            restarted = False
            if core.status().get("running"):
                try:
                    core.rewrite_config()
                    clash.open_client().reload_config(str(core.CONFIG))
                except Exception:  # noqa: BLE001
                    pass
            if "tun_stack" in payload and before.get("tun_stack") != state.settings.get("tun_stack"):
                if core.status().get("running"):
                    try:
                        self._restart_core()
                        restarted = True
                    except Exception as exc:  # noqa: BLE001
                        return {"ok": False, "message": str(exc), "settings": state.settings}
            return {"ok": True, "settings": state.settings, "restarted": restarted}

        def _save_custom_rules(self, payload: Dict[str, Any]) -> Dict[str, Any]:
            raw = str(payload.get("text") or "")
            lines = [line.strip() for line in raw.splitlines() if line.strip()]
            bad = [line for line in lines if not re.match(r"^[A-Za-z][A-Za-z0-9_-]*,", line)]
            if bad:
                return {"ok": False, "message": "规则格式应为 类型,内容[,策略]：" + bad[0][:60]}
            with state.lock:
                state.settings["custom_rules"] = lines
                save_settings(state.settings)
            reloaded = False
            if core.status().get("running"):
                try:
                    core.rewrite_config()
                    clash.open_client().reload_config(str(core.CONFIG))
                    reloaded = True
                except Exception as exc:  # noqa: BLE001
                    return {"ok": False, "message": f"规则已保存，但热重载失败：{exc}", "rules": lines}
            return {"ok": True, "rules": lines, "count": len(lines), "reloaded": reloaded}

        def _repair_network(self) -> Dict[str, Any]:
            steps = []
            st = self.server.state
            try:
                if (core.system_proxy() or {}).get("enabled"):
                    core.set_system_proxy(False)
                    steps.append("关闭系统代理")
                if core.restore_dns_from_backup():
                    steps.append("还原 DNS")
                if core.status().get("running"):
                    core.stop()
                    steps.append("停止内核")
                clash.set_endpoint_override(None)
                item = subscription.find(str(st.settings.get("active_sub") or ""))
                if item is not None:
                    want_tun = bool(st.settings.get("tun"))
                    core.start(item, tun=want_tun, preferred_port=int(st.settings.get("mixed_port") or 0))
                    clash.set_endpoint_override(core.endpoint())
                    steps.append("已用" + ("TUN" if want_tun else "混合端口") + "模式重启内核")
                st.last_error = ""
                append_event("repair", "一键修复：" + ("、".join(steps) or "无需处理"))
                return {"ok": True, "steps": steps, "core": core.status()}
            except Exception as exc:  # noqa: BLE001
                st.last_error = str(exc)
                return {"ok": False, "steps": steps, "message": str(exc)}

        def _state_payload(self) -> Dict[str, Any]:
            watcher = state.watcher.snapshot()
            with state.lock:
                job = state.current_job
                job_state = job.snapshot() if job else None
            return {
                "app": "clashlink",
                "version": __version__,
                "display_name": __display_name__,
                "port": state.port,
                "pid": os.getpid(),
                "started_at": state.started_at,
                "system_tz": system_timezone(),
                "settings": state.settings_payload(),
                "report": state.last_report,
                "last_job": job_state,
                "last_error": state.last_error,
                "watcher": watcher,
                "data_dir": str(data_dir()),
            }

        def _serve_static(self, path: str) -> None:
            if path in ("", "/"):
                path = "/index.html"
            name = path.lstrip("/")
            if name.startswith("api/"):
                return self._json({"error": "not found"}, 404)
            target = (WEB_DIR / name).resolve()
            try:
                target.relative_to(WEB_DIR)
            except ValueError:
                return self._json({"error": "forbidden"}, 403)
            if not target.is_file():
                return self._json({"error": "not found"}, 404)
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                ctype += "; charset=utf-8"
            data = target.read_bytes()
            if name == "index.html":
                token = str(self.server.state.settings.get("api_token") or "")
                meta = f'<meta name="clashlink-token" content="{token}">'
                data = data.replace(b"<head>", ("<head>" + meta).encode("utf-8"), 1)
                from . import __version__ as _ver
                data = data.replace(b"__VER__", _ver.encode("utf-8"))
            self._send(200, data, ctype)

    return Handler


class LocalHTTPServer(ThreadingHTTPServer):
    """HTTP 服务端口。

    ``HTTPServer.server_bind`` 会对监听地址做一次反向 DNS（``socket.getfqdn``）。
    在开启 TUN / fake-ip 的代理环境里，localhost 的反向解析可能被劫持并长时间
    卡住，导致控制台启动时看起来"没反应"。这里直接跳过解析。
    """

    daemon_threads = True

    def server_bind(self) -> None:  # noqa: D102 - 见类注释
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


def create_server(host: str, port: int) -> LocalHTTPServer:
    state = AppState()
    state.port = port
    handler = make_handler(state)
    server = LocalHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.state = state  # type: ignore[attr-defined]
    return server


def pick_port(preferred: int = 8788) -> int:
    for port in [preferred] + [preferred + i for i in range(1, 20)]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def ping_existing(port: int) -> Optional[Dict[str, Any]]:
    import urllib.request

    try:
        # 显式绕过系统代理：本机回环不该走机场
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/ping", timeout=0.8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data if data.get("app") == "clashlink" else None
    except Exception:
        return None


def ensure_api_token() -> str:
    st = load_settings()
    token = str(st.get("api_token") or "")
    import secrets

    if not token:
        token = secrets.token_hex(20)
        st["api_token"] = token
        save_settings(st)
    try:
        path = data_dir() / API_TOKEN_FILE
        path.write_text(token, encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError:
        pass
    os.environ["CLASHLINK_REQUIRE_TOKEN"] = "1"
    return token


def run(port: Optional[int] = None, open_browser: bool = True) -> int:
    ensure_api_token()
    if port is None:
        # 先看默认端口上是否已经有一个本应用在跑
        existing = ping_existing(DEFAULT_PORT)
        if existing:
            if open_browser:
                webbrowser.open(f"http://127.0.0.1:{DEFAULT_PORT}/")
            print(f"检测到已运行的控制台：http://127.0.0.1:{DEFAULT_PORT}/")
            return 0
        port = pick_port(DEFAULT_PORT)
    server = create_server("127.0.0.1", port)
    state: AppState = server.state  # type: ignore[attr-defined]
    state.port = port
    state.start()
    if port != DEFAULT_PORT:
        append_event("port", f"端口 {DEFAULT_PORT} 被占用，已改用 {port}")
    url = f"http://127.0.0.1:{port}/"
    print(f"{__display_name__} 已启动：{url}")
    print(f"按 Ctrl+C 退出（日志目录：{data_dir()}）")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    def _terminate(_signum, _frame):
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _terminate)
        except (ValueError, OSError):
            pass
    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        pass
    finally:
        state.watcher.stop()
        try:
            core.stop()
        except Exception:  # noqa: BLE001
            pass
        server.server_close()
    return 0
