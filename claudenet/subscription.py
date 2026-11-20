"""机场订阅：拉取、识别格式、转换成 mihomo 可用配置、落盘。"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from . import store
from .net import Proxy, detect_proxy, http_request

SUBS_DIR = store.data_dir() / "subs"
INDEX = SUBS_DIR / "index.json"
UA = "clash-verge/v2.5.10"

CLASH_KEYS = ("proxies:", "proxy-groups:", "proxy-providers:")


def _ensure_dirs() -> None:
    SUBS_DIR.mkdir(parents=True, exist_ok=True)


def load_index() -> List[Dict[str, Any]]:
    _ensure_dirs()
    try:
        data = json.loads(INDEX.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def load_all() -> List[Dict[str, Any]]:
    return load_index()


def save_index(items: List[Dict[str, Any]]) -> None:
    _ensure_dirs()
    tmp = INDEX.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, INDEX)


def _slug(name: str) -> str:
    raw = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", name).strip("-") or "sub"
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return f"{raw[:24]}-{digest}"


def yaml_path(item: Dict[str, Any]) -> Path:
    return SUBS_DIR / f"{item['id']}.yaml"


def read_config(item: Dict[str, Any]) -> str:
    return yaml_path(item).read_text(encoding="utf-8")


def find(item_id: str) -> Optional[Dict[str, Any]]:
    for item in load_index():
        if item.get("id") == item_id:
            return _hydrate(item)
    return None



# ---------------------------------------------------------------- 凭据（Keychain）

KC_SERVICE = "io.clashlink.subs"


def _kc(args, timeout: float = 8.0):
    try:
        return subprocess.run(["/usr/bin/security", *args], capture_output=True, text=True, timeout=timeout)
    except Exception:  # noqa: BLE001
        return None


def save_url(item_id: str, url: str) -> None:
    res = _kc(["add-generic-password", "-U", "-a", item_id, "-s", KC_SERVICE, "-w", url])
    if res is None or res.returncode != 0:
        raise RuntimeError("写入钥匙串失败：无法保存订阅地址")


def load_url(item_id: str) -> str:
    res = _kc(["find-generic-password", "-a", item_id, "-s", KC_SERVICE, "-w"])
    if res is None or res.returncode != 0:
        return ""
    return (res.stdout or "").strip()


def delete_url(item_id: str) -> None:
    _kc(["delete-generic-password", "-a", item_id, "-s", KC_SERVICE])


def _hydrate(item: Dict[str, Any]) -> Dict[str, Any]:
    if not item.get("url"):
        item["url"] = load_url(str(item.get("id") or ""))
    return item

# ---------------------------------------------------------------- 拉取


def _proxy() -> Optional[Proxy]:
    info = detect_proxy()
    return info.proxy


def _fetch_once(url: str, proxy: Optional[Proxy], timeout: float):
    return http_request(
        url,
        proxy=proxy,
        timeout=timeout,
        headers={"User-Agent": UA, "Accept": "*/*"},
        max_body=8 * 1024 * 1024,
    )


def parse_userinfo(raw: str) -> Dict[str, int]:
    """解析 subscription-userinfo：upload/download/total/expire。"""
    info: Dict[str, int] = {}
    for part in (raw or "").split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        try:
            info[key] = int(float(value))
        except ValueError:
            continue
    return {k: v for k, v in info.items() if k in ("upload", "download", "total", "expire")}


def fetch(url: str, timeout: float = 30.0) -> Dict[str, Any]:
    proxy = _proxy()
    result = _fetch_once(url, proxy, timeout)
    # 内置内核当前节点可能已经失效（切到挂掉的节点时很常见），
    # 拉订阅失败就绕开代理再试一次，不要把「添加订阅」也拖死。
    if result.error or (result.status and result.status >= 400):
        direct = _fetch_once(url, None, min(timeout, 20.0))
        if not direct.error and (not direct.status or direct.status < 400) and direct.body.strip():
            return {"body": direct.body, "usage": parse_userinfo(direct.header("subscription-userinfo"))}
    if result.error:
        raise RuntimeError(f"下载订阅失败：{result.error}")
    if result.status and result.status >= 400:
        raise RuntimeError(f"订阅返回 HTTP {result.status}")
    if not result.body.strip():
        raise RuntimeError("订阅内容为空")
    return {"body": result.body, "usage": parse_userinfo(result.header("subscription-userinfo"))}


# ---------------------------------------------------------------- 链接格式


def _b64decode(text: str) -> str:
    cleaned = re.sub(r"\s+", "", text)
    cleaned += "=" * (-len(cleaned) % 4)
    try:
        return base64.b64decode(cleaned).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return ""


def _name_from(fragment: str, fallback: str) -> str:
    if not fragment:
        return fallback
    return unquote(fragment).strip() or fallback


def _parse_ss(link: str) -> Optional[dict]:
    body = link[len("ss://"):]
    name = ""
    if "#" in body:
        body, name = body.split("#", 1)
    if "@" in body:
        userinfo, hostpart = body.rsplit("@", 1)
    else:
        decoded = _b64decode(body)
        if "@" not in decoded:
            return None
        userinfo, hostpart = decoded.rsplit("@", 1)
    if ":" not in userinfo:
        userinfo = _b64decode(userinfo)
    if ":" not in userinfo or ":" not in hostpart:
        return None
    method, password = userinfo.split(":", 1)
    host, port = hostpart.rsplit(":", 1)
    return {
        "name": _name_from(name, f"{host}:{port}"),
        "type": "ss",
        "server": host,
        "port": int(port),
        "cipher": method,
        "password": password,
        "udp": True,
    }


def _parse_trojan(link: str) -> Optional[dict]:
    parts = urlsplit(link)
    if not parts.hostname or not parts.port:
        return None
    query = parse_qs(parts.query)
    return {
        "name": _name_from(parts.fragment, f"{parts.hostname}:{parts.port}"),
        "type": "trojan",
        "server": parts.hostname,
        "port": parts.port,
        "password": unquote(parts.username or ""),
        "sni": (query.get("sni") or [parts.hostname])[0],
        "skip-cert-verify": (query.get("allowInsecure") or ["0"])[0] in ("1", "true"),
        "udp": True,
    }


def _parse_vmess(link: str) -> Optional[dict]:
    payload = _b64decode(link[len("vmess://"):])
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    host = str(data.get("add") or "")
    port = data.get("port")
    uuid = str(data.get("id") or "")
    if not host or not port or not uuid:
        return None
    network = str(data.get("net") or "tcp")
    tls = str(data.get("tls") or "") == "tls"
    proxy: Dict[str, Any] = {
        "name": str(data.get("ps") or f"{host}:{port}"),
        "type": "vmess",
        "server": host,
        "port": int(port),
        "uuid": uuid,
        "alterId": int(data.get("aid") or 0),
        "cipher": str(data.get("scy") or "auto"),
        "udp": True,
        "tls": tls,
    }
    if tls:
        proxy["servername"] = str(data.get("sni") or data.get("host") or host)
    if network == "ws":
        proxy["network"] = "ws"
        proxy["ws-opts"] = {
            "path": str(data.get("path") or "/"),
            "headers": {"Host": str(data.get("host") or host)},
        }
    return proxy


def _parse_vless(link: str) -> Optional[dict]:
    parts = urlsplit(link)
    if not parts.hostname or not parts.port or not parts.username:
        return None
    query = parse_qs(parts.query)
    security = (query.get("security") or ["none"])[0]
    network = (query.get("type") or ["tcp"])[0]
    proxy: Dict[str, Any] = {
        "name": _name_from(parts.fragment, f"{parts.hostname}:{parts.port}"),
        "type": "vless",
        "server": parts.hostname,
        "port": parts.port,
        "uuid": parts.username,
        "udp": True,
    }
    if security in ("tls", "reality"):
        proxy["tls"] = True
        proxy["servername"] = (query.get("sni") or [parts.hostname])[0]
        proxy["skip-cert-verify"] = (query.get("allowInsecure") or ["0"])[0] in ("1", "true")
    if network == "ws":
        proxy["network"] = "ws"
        proxy["ws-opts"] = {"path": (query.get("path") or ["/"])[0]}
    if (query.get("flow") or [""])[0]:
        proxy["flow"] = (query.get("flow") or [""])[0]
    return proxy


def _parse_hysteria2(link: str) -> Optional[dict]:
    parts = urlsplit(link)
    if not parts.hostname or not parts.port:
        return None
    query = parse_qs(parts.query)
    return {
        "name": _name_from(parts.fragment, f"{parts.hostname}:{parts.port}"),
        "type": "hysteria2",
        "server": parts.hostname,
        "port": parts.port,
        "password": unquote(parts.username or ""),
        "sni": (query.get("sni") or [parts.hostname])[0],
        "skip-cert-verify": (query.get("insecure") or ["0"])[0] in ("1", "true"),
    }


PARSERS = {
    "ss://": _parse_ss,
    "trojan://": _parse_trojan,
    "vmess://": _parse_vmess,
    "vless://": _parse_vless,
    "hysteria2://": _parse_hysteria2,
    "hy2://": _parse_hysteria2,
}


def parse_links(text: str) -> List[dict]:
    proxies: List[dict] = []
    seen: Dict[str, int] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        for prefix, parser in PARSERS.items():
            if line.lower().startswith(prefix):
                try:
                    proxy = parser(line)
                except (ValueError, IndexError):
                    proxy = None
                if proxy:
                    name = proxy["name"]
                    if name in seen:
                        seen[name] += 1
                        proxy["name"] = f"{name} {seen[name]}"
                    else:
                        seen[name] = 1
                    proxies.append(proxy)
                break
    return proxies


def proxies_to_yaml(proxies: List[dict]) -> str:
    if not proxies:
        raise RuntimeError("订阅里没有识别到任何节点")
    names = [p["name"] for p in proxies]
    lines = ["proxies:"]
    for proxy in proxies:
        lines.append("  - " + json.dumps(proxy, ensure_ascii=False))
    lines.append("proxy-groups:")
    lines.append("  - name: PROXY")
    lines.append("    type: select")
    lines.append("    proxies: " + json.dumps(names + ["DIRECT"], ensure_ascii=False))
    lines.append("rules:")
    lines.append("  - MATCH,PROXY")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 规范化


def normalize(text: str) -> Dict[str, Any]:
    """把订阅内容变成 clash 配置文本，返回 {kind, yaml, nodes}。"""
    stripped = text.strip()
    if "proxies:" in stripped or "proxy-providers:" in stripped:
        return {"kind": "clash", "yaml": stripped + "\n", "nodes": count_proxies(stripped)}
    decoded = _b64decode(stripped)
    if decoded and "proxies:" in decoded:
        return {"kind": "clash", "yaml": decoded.strip() + "\n", "nodes": count_proxies(decoded)}
    links = parse_links(decoded or stripped)
    if links:
        return {"kind": "links", "yaml": proxies_to_yaml(links), "nodes": len(links)}
    raise RuntimeError("无法识别订阅格式（既不是 Clash 配置，也不是节点链接列表）")


def count_proxies(text: str) -> int:
    head = text.split("proxy-groups:", 1)[0]
    if "proxies:" not in head:
        return 0
    body = head.split("proxies:", 1)[1]
    match = re.search(r"(?m)^([ \t]*)-\s", body)
    if not match:
        return 0
    indent = match.group(1)
    return len(re.findall(r"(?m)^" + re.escape(indent) + r"-\s", body))


def add(url: str, name: str = "", timeout: float = 30.0) -> Dict[str, Any]:
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise RuntimeError("订阅地址需要以 http:// 或 https:// 开头")
    fetched = fetch(url, timeout=timeout)
    data = normalize(fetched["body"])
    items = load_index()
    for item in items:
        if load_url(str(item.get("id") or "")) == url:
            item["yaml"] = data["yaml"]
            item.update(
                name=name.strip() or item.get("name") or url.split("/")[-1][:24],
                kind=data["kind"],
                nodes=data["nodes"],
                usage=fetched.get("usage") or {},
                updated_at=time.time(),
            )
            save_url(str(item["id"]), url)
            yaml_path(item).write_text(data["yaml"], encoding="utf-8")
            save_index(items)
            item["url"] = url
            return item
    item = {
        "id": _slug(name.strip() or url.split("/")[-1][:24] or url),
        "name": name.strip() or url.split("/")[-1][:24] or "订阅",
        "url": "",
        "kind": data["kind"],
        "nodes": data["nodes"],
        "usage": fetched.get("usage") or {},
        "added_at": time.time(),
        "updated_at": time.time(),
    }
    _ensure_dirs()
    save_url(str(item["id"]), url)
    yaml_path(item).write_text(data["yaml"], encoding="utf-8")
    items.append(item)
    save_index(items)
    item["url"] = url
    return item


def update(item_id: str, timeout: float = 30.0) -> Dict[str, Any]:
    """重新拉取订阅；失败时抛错并保留原有节点，不清空。"""
    item = find(item_id)
    if not item:
        raise RuntimeError("订阅不存在")
    url = item.get("url", "")
    if not url:
        raise RuntimeError("订阅没有保存地址，请重新添加")
    try:
        fetched = fetch(url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"更新失败，已保留原有 {item.get('nodes', 0)} 个节点：{exc}") from exc
    data = normalize(fetched["body"])
    if not data.get("yaml") or not data.get("nodes"):
        raise RuntimeError("更新失败，返回内容里没有可用节点；已保留原有配置")
    item["yaml"] = data["yaml"]
    item.update(
        kind=data["kind"],
        nodes=data["nodes"],
        usage=fetched.get("usage") or item.get("usage") or {},
        updated_at=time.time(),
    )
    yaml_path(item).write_text(data["yaml"], encoding="utf-8")
    items = load_index()
    for index, old in enumerate(items):
        if old.get("id") == item_id:
            items[index] = item
    save_index(items)
    return item


def rename(item_id: str, name: str) -> Dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise RuntimeError("订阅名称不能为空")
    items = load_index()
    for item in items:
        if item.get("id") == item_id:
            item["name"] = name[:40]
            save_index(items)
            return item
    raise RuntimeError("订阅不存在")


def remove(item_id: str) -> bool:
    items = load_index()
    keep = [i for i in items if i.get("id") != item_id]
    if len(keep) == len(items):
        return False
    for item in items:
        if item.get("id") == item_id:
            try:
                yaml_path(item).unlink()
            except OSError:
                pass
            delete_url(str(item.get("id") or ""))
    save_index(keep)
    return True


def public_all() -> List[Dict[str, Any]]:
    return [public(_hydrate(dict(i))) for i in load_index()]


def public(item: Dict[str, Any]) -> Dict[str, Any]:
    """对外展示时隐藏订阅地址里的 token。"""
    url = item.get("url", "")
    parts = urlsplit(url)
    host = parts.hostname or ""
    tail = parts.path.rstrip("/").split("/")[-1] if parts.path else ""
    masked = f"{parts.scheme}://{host}/…/{tail[-6:]}" if tail else f"{parts.scheme}://{host}/…"
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "url": masked,
        "kind": item.get("kind"),
        "nodes": item.get("nodes"),
        "usage": item.get("usage") or {},
        "added_at": item.get("added_at"),
        "updated_at": item.get("updated_at"),
    }
