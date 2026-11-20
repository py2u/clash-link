"""通过 mihomo / Clash 的外部控制器（External Controller）读取与切换节点。

Clash Verge Rev 默认关闭外部控制器；开启后会在 ``config.yaml`` 里暴露
``external-controller``（HTTP）或 ``external-controller-unix``（Unix socket）以及
``secret``。本模块只做三件事：列节点、切节点、测延迟，不修改任何代理配置。

也支持用环境变量覆盖，方便调试或对接其他内核::

    AIREADY_CLASH_API=http://127.0.0.1:9090
    AIREADY_CLASH_SECRET=xxx
"""
from __future__ import annotations

import http.client
import json
import os
import re
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import env as _env
from urllib.parse import quote, urlsplit

DEFAULT_TIMEOUT = 4.0
DELAY_TEST_URL = "http://www.gstatic.com/generate_204"
GROUP_TYPES = {"Selector", "URLTest", "Fallback", "LoadBalance", "Relay"}
DIRECT_NAMES = {"DIRECT", "REJECT", "COMPATIBLE", "PASS", "GLOBAL"}

VERGE_DIRS = (
    "io.github.clash-verge-rev.clash-verge-rev",
    "io.github.clash-verge-rev.clash-verge",
    "clash-verge-rev",
)


class ClashError(Exception):
    """控制器不可用，或请求失败。"""


@dataclass
class ClashEndpoint:
    kind: str  # "http" | "unix"
    address: str
    secret: str = ""
    source: str = ""

    def describe(self) -> str:
        if self.kind == "unix":
            return "unix socket"
        return f"http://{self.address}"


@dataclass
class Node:
    name: str
    type: str = ""
    delay: Optional[int] = None
    alive: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "type": self.type, "delay": self.delay, "alive": self.alive}


@dataclass
class Group:
    name: str
    type: str
    now: str = ""
    now_delay: Optional[int] = None
    nodes: List[Node] = field(default_factory=list)

    @property
    def is_selector(self) -> bool:
        return self.type in ("Selector", "Fallback")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "now": self.now,
            "now_delay": self.now_delay,
            "selector": self.is_selector,
            "nodes": [n.to_dict() for n in self.nodes],
        }


# --------------------------------------------------------------------------- #
# 端点发现
# --------------------------------------------------------------------------- #
def _read_scalar(path: Path, key: str) -> str:
    """从 YAML 顶层读一个标量，避免为了两行配置引入 PyYAML。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = re.search(rf"^{re.escape(key)}:\s*(.+?)\s*$", text, re.MULTILINE)
    if not match:
        return ""
    value = match.group(1).strip()
    if value[:1] in ("'", '"') and value[-1:] == value[:1]:
        value = value[1:-1]
    if value in ("", "null", "~"):
        return ""
    return value


def _verge_dirs() -> List[Path]:
    base = Path.home() / "Library" / "Application Support"
    dirs = [base / name for name in VERGE_DIRS]
    dirs.append(Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "clash-verge")
    return dirs


def discover_endpoint() -> Optional[ClashEndpoint]:
    """按环境变量 → Clash Verge 配置的顺序找一个可用端点。"""
    api = _env("CLASH_API").strip()
    secret = _env("CLASH_SECRET").strip()
    if api:
        parts = urlsplit(api if "://" in api else f"http://{api}")
        if parts.scheme == "unix":
            return ClashEndpoint("unix", parts.path or parts.netloc, secret, "环境变量")
        host = parts.hostname or "127.0.0.1"
        port = parts.port or 9090
        return ClashEndpoint("http", f"{host}:{port}", secret, "环境变量")

    for directory in _verge_dirs():
        config = directory / "config.yaml"
        if not config.exists():
            continue
        secret = _read_scalar(config, "secret")
        unix_path = _read_scalar(config, "external-controller-unix")
        controller = _read_scalar(config, "external-controller")
        if controller:
            host, _, port = controller.rpartition(":")
            host = host.strip("[]") or "127.0.0.1"
            if port.isdigit():
                return ClashEndpoint("http", f"{host}:{port}", secret, "Clash Verge")
        if unix_path:
            return ClashEndpoint("unix", unix_path, secret, "Clash Verge")
    return None


def external_controller_enabled() -> Optional[bool]:
    """读 Clash Verge 的开关，用于给出更准确的提示（None = 不知道）。"""
    for directory in _verge_dirs():
        verge = directory / "verge.yaml"
        if not verge.exists():
            continue
        raw = _read_scalar(verge, "enable_external_controller").lower()
        if raw in ("true", "false"):
            return raw == "true"
    return None


# --------------------------------------------------------------------------- #
# HTTP 客户端
# --------------------------------------------------------------------------- #
class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        super().__init__("localhost", timeout=timeout)
        self._unix_path = path

    def connect(self) -> None:  # type: ignore[override]
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._unix_path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


class ClashClient:
    def __init__(self, endpoint: ClashEndpoint, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.endpoint = endpoint
        self.timeout = timeout

    # -- 底层 ------------------------------------------------------------- #
    def _connection(self) -> http.client.HTTPConnection:
        if self.endpoint.kind == "unix":
            return _UnixHTTPConnection(self.endpoint.address, self.timeout)
        host, _, port = self.endpoint.address.rpartition(":")
        return http.client.HTTPConnection(host or "127.0.0.1", int(port or 9090), timeout=self.timeout)

    def request(self, method: str, path: str, payload: Optional[dict] = None) -> Tuple[int, Any]:
        headers = {"Accept": "application/json"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.endpoint.secret:
            headers["Authorization"] = f"Bearer {self.endpoint.secret}"
        conn = self._connection()
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            status = resp.status
        except (OSError, http.client.HTTPException) as exc:
            raise ClashError(str(exc)) from exc
        finally:
            conn.close()
        if not raw:
            return status, None
        try:
            return status, json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            return status, raw.decode("utf-8", errors="replace")[:500]

    # -- 业务 ------------------------------------------------------------- #
    def configs(self) -> Dict[str, Any]:
        status, data = self.request("GET", "/configs")
        if status != 200 or not isinstance(data, dict):
            raise ClashError(f"HTTP {status}")
        return data

    def set_mode(self, mode: str) -> None:
        if mode not in ("rule", "global", "direct"):
            raise ClashError("模式只能是 rule / global / direct")
        status, _ = self.request("PATCH", "/configs", {"mode": mode})
        if status not in (200, 204):
            raise ClashError(f"切换模式失败：HTTP {status}")

    def connections(self) -> Dict[str, Any]:
        status, data = self.request("GET", "/connections")
        if status != 200 or not isinstance(data, dict):
            raise ClashError(f"HTTP {status}")
        return data

    def close_connection(self, conn_id: str) -> None:
        status, _ = self.request("DELETE", f"/connections/{quote(conn_id, safe='')}")
        if status not in (200, 204):
            raise ClashError(f"断开连接失败：HTTP {status}")

    def close_all_connections(self) -> None:
        status, _ = self.request("DELETE", "/connections")
        if status not in (200, 204):
            raise ClashError(f"断开全部失败：HTTP {status}")

    def rules(self) -> List[Dict[str, str]]:
        status, data = self.request("GET", "/rules")
        if status != 200 or not isinstance(data, dict):
            raise ClashError(f"HTTP {status}")
        out: List[Dict[str, str]] = []
        for entry in data.get("rules") or []:
            if not isinstance(entry, dict):
                continue
            out.append(
                {
                    "type": str(entry.get("type") or ""),
                    "payload": str(entry.get("payload") or ""),
                    "proxy": str(entry.get("proxy") or ""),
                }
            )
        return out

    def patch_configs(self, payload: Dict[str, Any]) -> None:
        status, _ = self.request("PATCH", "/configs", payload)
        if status not in (200, 204):
            raise ClashError(f"修改配置失败：HTTP {status}")

    def reload_config(self, path: str) -> None:
        status, _ = self.request("PUT", "/configs", {"path": path, "force": True})
        if status not in (200, 204):
            raise ClashError(f"重载配置失败：HTTP {status}")

    def providers(self) -> List[Dict[str, Any]]:
        status, data = self.request("GET", "/providers/rules")
        if status != 200 or not isinstance(data, dict):
            raise ClashError(f"HTTP {status}")
        out: List[Dict[str, Any]] = []
        raw = data.get("providers") or []
        entries = raw.values() if isinstance(raw, dict) else raw
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            out.append(
                {
                    "name": str(entry.get("name") or ""),
                    "type": str(entry.get("vehicleType") or entry.get("type") or ""),
                    "behavior": str(entry.get("behavior") or ""),
                    "count": int(entry.get("ruleCount") or 0),
                    "updated_at": str(entry.get("updatedAt") or ""),
                }
            )
        return sorted(out, key=lambda item: item["name"].lower())

    def update_provider(self, name: str) -> None:
        from urllib.parse import quote

        status, _ = self.request("PUT", f"/providers/rules/{quote(name, safe='')}")
        if status not in (200, 204):
            raise ClashError(f"更新规则集失败：HTTP {status}")

    def open_stream(self, path: str):
        """打开流式接口（/logs、/traffic），返回 (conn, resp)；调用方负责 close。"""
        headers = {"Accept": "application/json"}
        if self.endpoint.secret:
            headers["Authorization"] = f"Bearer {self.endpoint.secret}"
        conn = self._connection()
        conn.timeout = 3600
        try:
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            conn.close()
            raise ClashError(str(exc)) from exc
        if resp.status != 200:
            conn.close()
            raise ClashError(f"HTTP {resp.status}")
        return conn, resp

    def version(self) -> Dict[str, Any]:
        status, data = self.request("GET", "/version")
        if status != 200 or not isinstance(data, dict):
            raise ClashError(f"HTTP {status}")
        return data

    def proxies(self) -> Dict[str, dict]:
        status, data = self.request("GET", "/proxies")
        if status != 200 or not isinstance(data, dict):
            raise ClashError(f"HTTP {status}")
        proxies = data.get("proxies")
        if not isinstance(proxies, dict):
            raise ClashError("响应缺少 proxies")
        return proxies

    @staticmethod
    def _last_delay(entry: dict) -> Optional[int]:
        history = entry.get("history")
        if isinstance(history, list) and history:
            delay = history[-1].get("delay") if isinstance(history[-1], dict) else None
            if isinstance(delay, int) and delay > 0:
                return delay
        return None

    def groups(self) -> List[Group]:
        proxies = self.proxies()
        groups: List[Group] = []
        for name, entry in proxies.items():
            if not isinstance(entry, dict):
                continue
            gtype = str(entry.get("type") or "")
            if gtype not in GROUP_TYPES:
                continue
            nodes: List[Node] = []
            for child in entry.get("all") or []:
                child_entry = proxies.get(child) if isinstance(child, str) else None
                if not isinstance(child_entry, dict):
                    continue
                child_type = str(child_entry.get("type") or "")
                if child_type in GROUP_TYPES:
                    continue
                nodes.append(
                    Node(
                        name=child,
                        type=child_type,
                        delay=self._last_delay(child_entry),
                        alive=child_entry.get("alive", True) is not False,
                    )
                )
            now = str(entry.get("now") or "")
            now_delay = None
            now_entry = proxies.get(now)
            if isinstance(now_entry, dict):
                now_delay = self._last_delay(now_entry)
            groups.append(Group(name=name, type=gtype, now=now, now_delay=now_delay, nodes=nodes))
        groups.sort(key=lambda g: (g.name != "PROXY", g.name.lower()))
        return groups

    def select(self, group: str, name: str) -> None:
        status, data = self.request("PUT", f"/proxies/{quote(group, safe='')}", {"name": name})
        if status not in (200, 204):
            message = data.get("message") if isinstance(data, dict) else data
            raise ClashError(message or f"HTTP {status}")

    def delay(self, name: str, timeout_ms: int = 5000) -> Optional[int]:
        query = f"?timeout={int(timeout_ms)}&url={quote(DELAY_TEST_URL, safe='')}"
        try:
            status, data = self.request("GET", f"/proxies/{quote(name, safe='')}/delay{query}")
        except ClashError:
            return None
        if status == 200 and isinstance(data, dict):
            value = data.get("delay")
            return value if isinstance(value, int) else None
        return None

    def test_group(self, group: Group, workers: int = 2, timeout_ms: int = 5000) -> Dict[str, Optional[int]]:
        names = [n.name for n in group.nodes if n.type not in ("Direct", "Reject", "Compatible")]
        if not names:
            return {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda name: self.delay(name, timeout_ms), names))
        return dict(zip(names, results))


# --------------------------------------------------------------------------- #
# 面向服务器的便捷接口
# --------------------------------------------------------------------------- #
def unavailable_reason(endpoint: Optional[ClashEndpoint], error: str) -> str:
    if endpoint is None:
        return "no_config"
    enabled = external_controller_enabled()
    if enabled is False:
        return "disabled_in_settings"
    if "timed out" in error or "refused" in error or "No such file" in error:
        return "not_running"
    return "unreachable"


_override: Optional[ClashEndpoint] = None


def set_endpoint_override(endpoint: Optional[ClashEndpoint]) -> None:
    """内置内核启动后把请求指向它，而不是 Clash Verge。"""
    global _override
    _override = endpoint


def current_endpoint() -> Optional[ClashEndpoint]:
    return _override or discover_endpoint()


def open_client(timeout: float = DEFAULT_TIMEOUT) -> ClashClient:
    endpoint = current_endpoint()
    if endpoint is None:
        raise ClashError("没有找到 Clash Verge 配置（可在设置里开启外部控制器）")
    return ClashClient(endpoint, timeout)


def snapshot() -> Dict[str, Any]:
    """给 /api/nodes 用的完整快照。"""
    started = time.time()
    endpoint = current_endpoint()
    if endpoint is None:
        return {
            "available": False,
            "reason": "no_config",
            "endpoint": None,
            "groups": [],
            "elapsed_ms": 0,
        }
    client = ClashClient(endpoint, timeout=2.5)
    try:
        version = client.version()
        groups = client.groups()
    except ClashError as exc:
        return {
            "available": False,
            "reason": "core_error" if _override else unavailable_reason(endpoint, str(exc)),
            "error": str(exc),
            "endpoint": {"kind": endpoint.kind, "address": endpoint.address, "source": endpoint.source},
            "groups": [],
            "elapsed_ms": int((time.time() - started) * 1000),
        }
    return {
        "available": True,
        "reason": None,
        "version": version.get("version"),
        "endpoint": {"kind": endpoint.kind, "address": endpoint.address, "source": endpoint.source},
        "groups": [g.to_dict() for g in groups],
        "elapsed_ms": int((time.time() - started) * 1000),
    }

def summarize_connections(data: Dict[str, Any], limit: int = 60) -> Dict[str, Any]:
    items = []
    raw = data.get("connections") if isinstance(data, dict) else None
    for entry in (raw or []):
        if not isinstance(entry, dict):
            continue
        meta = entry.get("metadata") or {}
        items.append({
            "id": entry.get("id") or "",
            "host": meta.get("host") or meta.get("destinationIP") or "",
            "dest": f'{meta.get("destinationIP") or ""}:{meta.get("destinationPort") or ""}'.strip(":"),
            "network": meta.get("network") or "",
            "type": meta.get("type") or "",
            "process": meta.get("process") or meta.get("processPath") or "",
            "rule": entry.get("rule") or "",
            "rule_payload": entry.get("rulePayload") or "",
            "chains": entry.get("chains") or [],
            "upload": int(entry.get("upload") or 0),
            "download": int(entry.get("download") or 0),
            "start": entry.get("start") or "",
        })
    items.sort(key=lambda x: x["upload"] + x["download"], reverse=True)
    return {
        "up_total": int(data.get("uploadTotal") or 0) if isinstance(data, dict) else 0,
        "down_total": int(data.get("downloadTotal") or 0) if isinstance(data, dict) else 0,
        "count": len(items),
        "items": items[:limit],
    }


def parse_log_line(raw: str) -> Optional[Dict[str, str]]:
    raw = raw.strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return {"type": "info", "payload": raw[:400]}
    if not isinstance(data, dict):
        return None
    return {"type": str(data.get("type") or "info"), "payload": str(data.get("payload") or "")[:600]}
