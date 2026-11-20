"""本地规则命中测试：按 mihomo /rules 的顺序做静态匹配。

只做域名/IP 的常见类型，GEOIP 需要地理库，这里按国家名做粗略判断（cn 走保留段）。
"""
from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import clash, core


def _provider_payload(name: str) -> List[str]:
    path = core.CORE_DIR / "ruleset" / f"{name}.yaml"
    if not path.exists():
        matches = list((core.CORE_DIR / "ruleset").glob(f"{name}*")) if (core.CORE_DIR / "ruleset").exists() else []
        if not matches:
            return []
        path = matches[0]
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    out: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("-"):
            continue
        value = line[1:].strip().strip("'\"")
        if value and not value.startswith("#"):
            out.append(value.lower())
    return out


def _match_suffix(host: str, suffix: str) -> bool:
    if suffix in ("", "*"):
        return True
    if suffix.startswith("+."):
        suffix = suffix[1:]
    if suffix.startswith("."):
        return host.endswith(suffix) or host == suffix[1:]
    return host == suffix or host.endswith("." + suffix) or host.endswith(suffix)


def _match_provider(host: str, name: str) -> bool:
    payload = _provider_payload(name)
    for item in payload:
        data = item
        if data.startswith(("domain,", "domain-suffix,", "domain-keyword,")):
            kind, _, value = data.partition(",")
            value = value.strip()
            if kind == "domain" and host == value:
                return True
            if kind == "domain-suffix" and _match_suffix(host, value):
                return True
            if kind == "domain-keyword" and value in host:
                return True
            continue
        if data.startswith(("ip-cidr,", "ip-cidr6,")):
            _, _, value = data.partition(",")
            try:
                if ipaddress.ip_address(host) in ipaddress.ip_network(value.split(",")[0].strip(), strict=False):
                    return True
            except ValueError:
                continue
            continue
        if _match_suffix(host, data):
            return True
    return False


def _match_ip_cidr(target: str, payload: str) -> bool:
    try:
        addr = ipaddress.ip_address(target)
        net = ipaddress.ip_network(payload.split(",")[0].strip(), strict=False)
        return addr in net
    except ValueError:
        return False


def _is_private(target: str) -> bool:
    try:
        addr = ipaddress.ip_address(target)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback or addr.is_link_local


def test(target: str) -> Dict[str, Any]:
    target = (target or "").strip().lower()
    if not target:
        return {"ok": False, "message": "请输入域名或 IP"}
    host = target
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0].split(":", 1)[0].strip()
    try:
        rules = clash.open_client(timeout=8.0).rules()
    except clash.ClashError as exc:
        return {"ok": False, "message": str(exc)}
    for index, rule in enumerate(rules):
        raw_kind = str(rule.get("type") or "")
        kind = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", raw_kind).upper()
        kind = kind.replace("_", "-")
        payload = str(rule.get("payload") or "")
        proxy = str(rule.get("proxy") or "")
        hit = False
        if kind == "MATCH":
            hit = True
        elif kind == "DOMAIN":
            hit = host == payload.lower()
        elif kind == "DOMAIN-SUFFIX":
            hit = _match_suffix(host, payload.lower())
        elif kind == "DOMAIN-KEYWORD":
            hit = payload.lower() in host
        elif kind in ("IP-CIDR", "IP-CIDR6"):
            hit = _match_ip_cidr(host, payload)
        elif kind == "RULE-SET":
            hit = _match_provider(host, payload)
        elif kind in ("GEOIP", "SRC-GEOIP"):
            if _is_private(host):
                hit = payload.lower() in ("cn", "private")
        if hit:
            return {"ok": True, "target": host, "index": index, "rule": rule, "total": len(rules)}
    return {"ok": True, "target": host, "rule": None, "total": len(rules), "message": "没有命中任何规则，走默认出站"}
