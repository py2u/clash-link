"""体检规则：把「Claude 能不能用」拆成 13 个可解释的检查项。"""
from __future__ import annotations

import json
import os
import platform
import plistlib
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone as dt_timezone
from typing import Any, Callable, Dict, List, Optional

from . import __version__
from .geo import GeoInfo, country_name, fetch_geo, is_blocked_region
from .net import (
    CHROME_UA,
    HttpResult,
    Proxy,
    ProxyInfo,
    detect_proxy,
    http_request,
    probe_tls,
    resolve_ips,
)
from .timezone import (
    compare_timezones,
    describe_drift,
    system_timezone,
    tz_clock,
    tz_date,
    tz_offset_seconds,
    offset_label,
)

GROUP_ORDER = ["出口与代理", "Claude 连通性", "其他 AI 服务", "环境一致性"]

# 海外 AI 服务的可达性探测：优先打官方 API，返回 401/403 也说明链路通
AI_PROVIDERS = [
    ("ai-openai", "OpenAI / ChatGPT", "https://api.openai.com/v1/models", "chatgpt.com"),
    ("ai-gemini", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta/models", "gemini.google.com"),
    ("ai-grok", "xAI Grok", "https://api.x.ai/v1/models", "grok.com"),
    ("ai-perplexity", "Perplexity", "https://api.perplexity.ai/chat/completions", "perplexity.ai"),
    ("ai-mistral", "Mistral", "https://api.mistral.ai/v1/models", "chat.mistral.ai"),
    ("ai-copilot", "GitHub Copilot", "https://api.githubcopilot.com/models", "copilot.microsoft.com"),
]

STATUS_LABEL = {
    "pass": "正常",
    "warn": "注意",
    "fail": "异常",
    "info": "信息",
    "pending": "等待",
}


@dataclass
class Check:
    id: str
    group: str
    title: str
    status: str = "pending"
    summary: str = "等待检测…"
    detail: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    fix: Optional[Dict[str, Any]] = None
    critical: bool = False
    elapsed_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status_label"] = STATUS_LABEL.get(self.status, self.status)
        return d


@dataclass(frozen=True)
class Spec:
    id: str
    group: str
    title: str
    critical: bool = False


SPECS: List[Spec] = [
    Spec("proxy", "出口与代理", "代理链路"),
    Spec("exit", "出口与代理", "出口节点"),
    Spec("dns", "出口与代理", "DNS 解析"),
    Spec("api", "Claude 连通性", "Anthropic API", critical=True),
    Spec("web", "Claude 连通性", "Claude 网页版"),
    Spec("console", "Claude 连通性", "Console 控制台"),
    Spec("statsig", "Claude 连通性", "Statsig 遥测"),
    Spec("tls", "Claude 连通性", "TLS 与证书", critical=True),
    Spec("latency", "Claude 连通性", "链路延迟"),
    Spec("region", "环境一致性", "地区可用性", critical=True),
    Spec("timezone", "环境一致性", "时区一致性"),
    Spec("clock", "环境一致性", "系统时钟"),
    Spec("local", "环境一致性", "本地客户端"),
]
_region_index = next(i for i, _s in enumerate(SPECS) if _s.id == "region")
for _offset, (_pid, _title, _api, _web) in enumerate(AI_PROVIDERS):
    SPECS.insert(_region_index + _offset, Spec(_pid, "其他 AI 服务", _title))


class ScanContext:
    def __init__(self, settings: Optional[Dict[str, Any]] = None) -> None:
        self.settings = settings or {}
        self.proxy_info: ProxyInfo = detect_proxy()
        self._geo: Optional[GeoInfo] = None
        self._geo_lock = threading.Lock()
        self._geo_event = threading.Event()
        self._geo_loading = False
        self.system_tz = system_timezone()
        self.started = time.time()

    @property
    def proxy(self) -> Optional[Proxy]:
        return self.proxy_info.proxy

    def geo(self, refresh: bool = False) -> GeoInfo:
        """单飞（single-flight）拉取出口信息，多个检查共享一次请求。"""
        if refresh:
            with self._geo_lock:
                self._geo = None
        with self._geo_lock:
            if self._geo is not None:
                return self._geo
            if not self._geo_loading:
                self._geo_loading = True
                owner = True
            else:
                owner = False
        if owner:
            try:
                value = fetch_geo(self.proxy)
                with self._geo_lock:
                    self._geo = value
                    return value
            finally:
                with self._geo_lock:
                    self._geo_loading = False
                self._geo_event.set()
        self._geo_event.wait(20)
        with self._geo_lock:
            return self._geo or GeoInfo(error="出口信息获取超时")


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _result(
    spec: Spec,
    started: float,
    status: str,
    summary: str,
    detail: str = "",
    data: Optional[Dict[str, Any]] = None,
    fix: Optional[Dict[str, Any]] = None,
) -> Check:
    return Check(
        id=spec.id,
        group=spec.group,
        title=spec.title,
        status=status,
        summary=summary,
        detail=detail,
        data=data or {},
        fix=fix,
        critical=spec.critical,
        elapsed_ms=_elapsed(started),
    )


def _boundary_connect(host: str, port: int, timeout: float) -> float:
    """仅测 TCP 连接耗时（毫秒），失败返回 -1。"""
    started = time.perf_counter()
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return (time.perf_counter() - started) * 1000
    except OSError:
        return -1.0


# --------------------------------------------------------------------------- #
# 各项检查
# --------------------------------------------------------------------------- #
def check_proxy(ctx: ScanContext, spec: Spec) -> Check:
    started = time.monotonic()
    info = ctx.proxy_info
    data = {
        "proxy": info.proxy.label if info.proxy else "",
        "proxy_source": info.proxy.source if info.proxy else "",
        "tun": info.tun_active,
        "ports": info.open_ports,
        "notes": info.notes,
    }
    if info.proxy or info.tun_active:
        bits = []
        if info.tun_active:
            bits.append("TUN 透明代理")
        if info.proxy:
            bits.append(f"{info.proxy.label}（{info.proxy.source}）")
        return _result(spec, started, "pass", "；".join(bits), "；".join(info.notes), data)
    return _result(
        spec,
        started,
        "fail",
        "未发现代理或 TUN",
        "当前是直连出口，在中国大陆等受限地区通常无法访问 Claude。",
        data,
        fix={"action": "hint", "label": "启动 Clash 等代理后再检测"},
    )


def check_exit(ctx: ScanContext, spec: Spec) -> Check:
    started = time.monotonic()
    geo = ctx.geo()
    data = geo.to_dict()
    if geo.error or not geo.ip:
        return _result(
            spec,
            started,
            "fail",
            geo.error or "无法获取出口 IP",
            "所有归属地接口均失败，可能是代理未放行 IP 查询或网络中断。",
            data,
        )
    place = " · ".join([p for p in (geo.city, geo.region) if p]) or geo.country
    summary = f"{geo.flag} {country_name(geo.country_code, geo.country)} · {place}"
    detail_bits = [f"出口 IP {geo.ip}"]
    if geo.asn or geo.org:
        detail_bits.append(f"ASN {geo.asn} {geo.org}".strip())
    if geo.colo:
        detail_bits.append(f"Cloudflare 边缘 {geo.colo}")
    status = "pass"
    if geo.trace_loc and geo.country_code and geo.trace_loc != geo.country_code:
        status = "warn"
        detail_bits.append(f"trace 判定为 {geo.trace_loc}，与归属地库不一致")
    return _result(spec, started, status, summary, " · ".join(detail_bits), data)


def check_dns(ctx: ScanContext, spec: Spec) -> Check:
    started = time.monotonic()
    ips = resolve_ips("claude.ai")
    fake = [ip for ip in ips if ip.startswith("198.18.") or ip.startswith("198.19.")]
    data = {"resolved": ips, "fake_ip": bool(fake)}
    if not ips:
        return _result(spec, started, "fail", "claude.ai 无法解析", "DNS 解析失败，检查 DNS 设置。", data)
    if fake:
        return _result(
            spec,
            started,
            "pass",
            f"fake-ip 接管（{fake[0]}）",
            "TUN + fake-ip 模式下域名由代理内核解析，不会向本地 ISP 泄漏真实 DNS 查询。",
            data,
        )
    return _result(
        spec,
        started,
        "pass",
        f"解析到 {', '.join(ips[:3])}",
        "未启用 fake-ip，域名由系统 DNS 解析；如出现解析污染可开启 TUN。",
        data,
    )


def _body_snippet(body: str, limit: int = 180) -> str:
    text = " ".join((body or "").split())
    return text[:limit]


def check_api(ctx: ScanContext, spec: Spec) -> Check:
    started = time.monotonic()
    res: HttpResult = http_request(
        "https://api.anthropic.com/v1/models",
        proxy=ctx.proxy,
        timeout=10,
        headers={"Accept": "application/json", "anthropic-version": "2023-06-01"},
    )
    payload = res.json() or {}
    err = payload.get("error") if isinstance(payload, dict) else None
    err_type = (err or {}).get("type", "") if isinstance(err, dict) else ""
    err_msg = (err or {}).get("message", "") if isinstance(err, dict) else ""
    data = {
        "status": res.status,
        "error_type": err_type,
        "error_message": err_msg,
        "elapsed_ms": res.elapsed_ms,
        "body": _body_snippet(res.body),
    }

    if res.error and not res.status:
        return _result(
            spec,
            started,
            "fail",
            "无法连接 api.anthropic.com",
            f"{res.error}。通常是节点不通、TLS 被重置或本地防火墙拦截。",
            data,
        )
    if res.status == 401:
        return _result(
            spec,
            started,
            "pass",
            f"可达（{res.elapsed_ms} ms，401 未授权属预期）",
            "未携带 API Key 时返回 401，说明链路与 TLS 均正常、且未被地区封锁。",
            data,
        )
    if res.status == 403:
        if err_type in ("request_not_allowed", "forbidden") or "region" in err_msg.lower() or "not allowed" in err_msg.lower():
            return _result(
                spec,
                started,
                "fail",
                "Anthropic 拒绝该出口地区",
                f"接口返回 403 {err_type or ''}：{err_msg or 'Request not allowed'}。需要更换到支持地区的节点。",
                data,
                fix={"action": "hint", "label": "切换到美国/日本等支持地区节点"},
            )
        return _result(
            spec,
            started,
            "warn",
            f"403（{err_type or '未知原因'}）",
            _body_snippet(res.body),
            data,
        )
    if res.status == 429:
        return _result(
            spec,
            started,
            "warn",
            "被限流（429）",
            "节点 IP 可能被多人共用触发限流，换节点或稍后重试。",
            data,
        )
    if res.status and res.status < 500:
        return _result(spec, started, "pass", f"可达（HTTP {res.status}）", _body_snippet(res.body), data)
    return _result(
        spec,
        started,
        "warn",
        f"服务端异常（HTTP {res.status}）",
        _body_snippet(res.body),
        data,
    )


def check_web(ctx: ScanContext, spec: Spec) -> Check:
    started = time.monotonic()
    res = http_request(
        "https://claude.ai/",
        proxy=ctx.proxy,
        timeout=10,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
        },
    )
    body = res.body or ""
    mitigated = res.header("cf-mitigated")
    data = {
        "status": res.status,
        "cf_mitigated": mitigated,
        "elapsed_ms": res.elapsed_ms,
        "body": _body_snippet(body, 220),
    }
    lowered = body.lower()
    if res.error and not res.status:
        return _result(
            spec, started, "fail", "无法连接 claude.ai", res.error, data
        )
    if res.status in (200, 301, 302, 307, 308):
        return _result(
            spec,
            started,
            "pass",
            f"可达（HTTP {res.status}，{res.elapsed_ms} ms）",
            "claude.ai 正常响应。",
            data,
        )
    if res.status == 403 and mitigated == "challenge":
        return _result(
            spec,
            started,
            "warn",
            "可达，但被 Cloudflare 人机验证拦下",
            "命令行请求缺少浏览器指纹，403 + cf-mitigated: challenge 属于正常防护；"
            "用真实浏览器打开 claude.ai 通常可以通过。若浏览器也一直转圈，说明该节点 IP 已被重点风控。",
            data,
        )
    if "not available in your region" in lowered or "unavailable in your region" in lowered:
        return _result(
            spec,
            started,
            "fail",
            "Claude 在该地区不可用",
            "页面明确提示地区不支持，需要更换节点。",
            data,
            fix={"action": "hint", "label": "切换到美国/日本等支持地区节点"},
        )
    if res.status == 403:
        return _result(
            spec,
            started,
            "warn",
            "403 被拒绝",
            _body_snippet(body),
            data,
        )
    return _result(
        spec, started, "warn", f"HTTP {res.status}", _body_snippet(body), data
    )


def check_console(ctx: ScanContext, spec: Spec) -> Check:
    started = time.monotonic()
    res = http_request("https://console.anthropic.com/", proxy=ctx.proxy, timeout=8)
    data = {"status": res.status, "elapsed_ms": res.elapsed_ms}
    if res.error and not res.status:
        return _result(spec, started, "fail", "无法连接 console.anthropic.com", res.error, data)
    if res.status < 400:
        return _result(spec, started, "pass", f"可达（HTTP {res.status}）", "", data)
    return _result(spec, started, "warn", f"HTTP {res.status}", _body_snippet(res.body), data)


def check_statsig(ctx: ScanContext, spec: Spec) -> Check:
    """Claude Code 的遥测/功能开关走 Statsig。

    注意：statsig.anthropic.com 在 2026-11 已是 NXDOMAIN（域名退役），
    继续探测它会让所有用户都看到一条吓人的「TLS 握手被中断」。
    现在改探 Statsig 的公共入口 api.statsig.com：403/401/404 都说明链路可达。
    """
    started = time.monotonic()
    res = http_request("https://api.statsig.com/", proxy=ctx.proxy, timeout=8)
    data = {"status": res.status, "error": res.error, "elapsed_ms": res.elapsed_ms, "host": "api.statsig.com"}
    if res.status and res.status < 500:
        return _result(
            spec,
            started,
            "pass",
            f"可达（HTTP {res.status}）",
            "Claude Code 的功能开关与遥测入口（api.statsig.com）正常。",
            data,
        )
    if res.error:
        return _result(
            spec,
            started,
            "warn",
            "Statsig 不可达",
            f"{res.error}。该入口供 Claude Code 拉取功能开关/遥测；不影响网页版，"
            "但可能导致 Claude Code 启动变慢或部分功能异常。",
            data,
        )
    return _result(spec, started, "warn", f"HTTP {res.status}", _body_snippet(res.body), data)


def check_tls(ctx: ScanContext, spec: Spec) -> Check:
    started = time.monotonic()
    probe = probe_tls("api.anthropic.com", proxy=ctx.proxy, timeout=8)
    data = {
        "ok": probe.ok,
        "verified": probe.verified,
        "issuer": probe.issuer,
        "subject": probe.subject,
        "not_after": probe.not_after,
        "version": probe.version,
        "cipher": probe.cipher,
        "tls_ms": probe.tls_ms,
        "error": probe.error,
    }
    if not probe.ok:
        if not probe.verified:
            return _result(
                spec,
                started,
                "fail",
                "证书校验失败，疑似被中间人代理",
                f"{probe.error}。请确认代理软件没有启用「TLS 解密/MITM」；"
                "Claude 客户端会拒绝此类证书。",
                data,
            )
        return _result(spec, started, "fail", "TLS 握手失败", probe.error, data)
    issuer_org = probe.issuer
    short = issuer_org.split("organizationName=")[-1].split(",")[0] if issuer_org else "受信任 CA"
    return _result(
        spec,
        started,
        "pass",
        f"TLS {probe.version} · {short}",
        f"证书主体 {probe.subject or '—'}；有效期至 {probe.not_after or '—'}；握手 {probe.tls_ms} ms。",
        data,
    )


def check_latency(ctx: ScanContext, spec: Spec) -> Check:
    started = time.monotonic()
    # 先热身一次并丢弃：首次握手要建 DNS/连接池，会把样本整体拉偏
    probe_tls("api.anthropic.com", proxy=ctx.proxy, timeout=8)
    samples: List[int] = []
    for _ in range(3):
        probe = probe_tls("api.anthropic.com", proxy=ctx.proxy, timeout=8)
        if probe.ok:
            samples.append(probe.total_ms)
    retried = False
    if len(samples) >= 2:
        ordered = sorted(samples)
        slowest, second = ordered[-1], ordered[-2]
        # 单个尖峰（首连/TUN 刚起、机场瞬时拥塞）会同时抬高抖动，复测一次再判定
        if slowest > 2500 and slowest > 2.5 * second:
            again = probe_tls("api.anthropic.com", proxy=ctx.proxy, timeout=8)
            if again.ok:
                samples[samples.index(slowest)] = again.total_ms
                retried = True
    if not samples:
        return _result(
            spec, started, "warn", "无法采样", "TLS 探测全部失败，无法评估延迟。", {"samples": []}
        )
    samples.sort()
    median = samples[len(samples) // 2]
    jitter = samples[-1] - samples[0]
    data = {"samples": samples, "median_ms": median, "jitter_ms": jitter, "retried": retried}
    if median < 1200 and jitter < 900:
        status, label = "pass", "流畅"
    elif median < 2400:
        status, label = "warn", "偏慢"
    else:
        status, label = "fail", "很慢"
    return _result(
        spec,
        started,
        status,
        f"{label} · 中位 {median} ms（抖动 {jitter} ms）",
        f"三次握手样本：{' / '.join(str(s) for s in samples)} ms（含 TCP+TLS，经代理）"
        + ("；其中 1 个尖峰已复测替换。" if retried else "。")
        + "跨太平洋链路握手 700–1200 ms 属正常；超过 2400 ms 时 Claude 首字延迟会明显变慢。",
        data,
    )


def check_region(ctx: ScanContext, spec: Spec) -> Check:
    started = time.monotonic()
    geo = ctx.geo()
    code = geo.country_code
    data = {"country_code": code, "country": country_name(code, geo.country), "trace_loc": geo.trace_loc}
    if not code:
        return _result(spec, started, "warn", "无法判断出口地区", geo.error or "", data)
    if is_blocked_region(code):
        return _result(
            spec,
            started,
            "fail",
            f"{country_name(code, geo.country)} 不在 Anthropic 支持地区",
            "Anthropic 按出口 IP 判定地区，该地区会被拒绝访问（403 request_not_allowed）。",
            data,
            fix={"action": "hint", "label": "切换到美国/日本/新加坡等支持地区节点"},
        )
    return _result(
        spec,
        started,
        "pass",
        f"{country_name(code, geo.country)} 属于支持地区",
        "Anthropic 支持该地区；最终可用性以 API 实测为准。",
        data,
    )


def check_timezone(ctx: ScanContext, spec: Spec) -> Check:
    started = time.monotonic()
    geo = ctx.geo()
    system_tz = ctx.system_tz
    exit_tz = geo.timezone
    data = {
        "system_tz": system_tz,
        "exit_tz": exit_tz,
        "system_clock": tz_clock(system_tz),
        "exit_clock": tz_clock(exit_tz) if exit_tz else "",
        "system_date": tz_date(system_tz),
        "exit_date": tz_date(exit_tz) if exit_tz else "",
    }
    if not exit_tz:
        return _result(
            spec,
            started,
            "warn",
            "无法确定出口时区",
            "归属地接口未返回时区，可手动按出口城市选择时区。",
            data,
        )
    cmp = compare_timezones(system_tz, exit_tz)
    data["comparison"] = cmp
    sys_off = cmp.get("system_offset")
    exit_off = cmp.get("exit_offset")
    sys_label = offset_label(sys_off) if isinstance(sys_off, int) else "?"
    exit_label = offset_label(exit_off) if isinstance(exit_off, int) else "?"
    detail = (
        f"系统 {system_tz}（{sys_label}，当地 {data['system_clock']}） · "
        f"出口 {exit_tz}（{exit_label}，当地 {data['exit_clock']}）。"
    )
    if cmp.get("same_name"):
        return _result(spec, started, "pass", f"已对齐 {system_tz}", detail, data)
    if cmp.get("same_offset"):
        return _result(
            spec, started, "pass", f"偏移一致（{system_tz} / {exit_tz}）", detail, data
        )
    fix = {"action": "set_timezone", "label": f"同步为 {exit_tz}", "tz": exit_tz}
    return _result(
        spec,
        started,
        "warn",
        describe_drift(system_tz, exit_tz),
        detail + " 时区不一致会让 Claude 网页/客户端显示的时间、限额重置提示与你的真实所在地错位，也容易触发账号风控。",
        data,
        fix=fix,
    )


def check_clock(ctx: ScanContext, spec: Spec) -> Check:
    started = time.monotonic()
    res = http_request(
        "https://api.anthropic.com/",
        proxy=ctx.proxy,
        timeout=8,
        method="HEAD",
        headers={"Accept": "*/*"},
    )
    raw = res.header("date")
    data = {"status": res.status, "date_header": raw}
    if not raw:
        return _result(spec, started, "warn", "无法读取服务端时间", res.error or "响应缺少 Date 头", data)
    from email.utils import parsedate_to_datetime

    try:
        server_dt = parsedate_to_datetime(raw)
    except Exception:
        return _result(spec, started, "warn", "服务端时间格式异常", raw, data)
    if server_dt.tzinfo is None:
        server_dt = server_dt.replace(tzinfo=dt_timezone.utc)
    skew = (datetime.now(dt_timezone.utc) - server_dt.astimezone(dt_timezone.utc)).total_seconds()
    data["skew_seconds"] = round(skew, 1)
    abs_skew = abs(skew)
    if abs_skew <= 120:
        return _result(spec, started, "pass", "时钟同步正常", f"与服务端相差 {skew:+.0f} 秒。", data)
    if abs_skew <= 600:
        return _result(
            spec,
            started,
            "warn",
            f"时钟偏差 {skew:+.0f} 秒",
            "偏差过大会导致 TLS/鉴权失败，建议在「系统设置 → 通用 → 日期与时间」开启自动同步。",
            data,
        )
    return _result(
        spec,
        started,
        "fail",
        f"时钟严重偏差 {skew:+.0f} 秒",
        "请立即校准系统时间，否则 Claude API 与登录都会失败。",
        data,
    )


def _find_node() -> str:
    found = shutil.which("node")
    if found:
        return found
    for path in (
        "/opt/homebrew/bin/node",
        "/usr/local/bin/node",
        os.path.expanduser("~/.local/bin/node"),
    ):
        if os.path.exists(path):
            return path
    import glob

    candidates = sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/node")))
    return candidates[-1] if candidates else ""


def check_local(ctx: ScanContext, spec: Spec) -> Check:
    started = time.monotonic()
    data: Dict[str, Any] = {}

    node_path = _find_node()
    if node_path:
        try:
            node_ver = subprocess.run(
                [node_path, "-v"], capture_output=True, text=True, timeout=5
            ).stdout.strip()
        except Exception:
            node_ver = ""
        data["node"] = node_ver
        data["node_path"] = node_path

    claude_bin = shutil.which("claude") or (
        os.path.expanduser("~/.local/bin/claude")
        if os.path.exists(os.path.expanduser("~/.local/bin/claude"))
        else ""
    )
    if claude_bin:
        data["claude_cli"] = claude_bin
        try:
            out = subprocess.run(
                [claude_bin, "--version"], capture_output=True, text=True, timeout=8
            ).stdout.strip()
            if out:
                data["claude_cli_version"] = out.splitlines()[0][:80]
        except Exception:
            pass

    app = "/Applications/Claude.app/Contents/Info.plist"
    if os.path.exists(app):
        try:
            with open(app, "rb") as fh:
                plist = plistlib.load(fh)
            data["desktop"] = plist.get("CFBundleShortVersionString", "")
        except Exception:
            data["desktop"] = ""

    base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
    if base_url:
        data["base_url"] = base_url
    base = os.path.expanduser("~/.claude/settings.json")
    if os.path.exists(base):
        try:
            with open(base, "r", encoding="utf-8") as fh:
                conf = json.load(fh)
            env = conf.get("env") or {}
            if env.get("ANTHROPIC_BASE_URL"):
                data["base_url"] = env["ANTHROPIC_BASE_URL"]
        except Exception:
            pass

    bits = []
    if data.get("node"):
        bits.append(f"Node {data['node']}")
    if data.get("claude_cli"):
        bits.append("Claude Code 已安装")
    if data.get("desktop"):
        bits.append(f"Claude 桌面版 {data['desktop']}")
    summary = " · ".join(bits) or "未检测到 Claude 客户端"
    status = "info" if bits else "info"
    detail = "本地环境仅作参考：网页版不需要 Node；Claude Code 需要 Node.js ≥ 18。"
    if data.get("base_url"):
        detail += f" 已配置自定义 API 地址：{data['base_url']}（不走官方线路时，以上连通性结论仅供参考）。"
    if data.get("node") and not _node_ok(data["node"]):
        status = "warn"
        summary = f"Node {data['node']} 版本过低"
        detail = "Claude Code 需要 Node.js 18 及以上，请升级 Node。"
    return _result(spec, started, status, summary, detail, data)


def _node_ok(version: str) -> bool:
    try:
        major = int(version.lstrip("v").split(".")[0])
        return major >= 18
    except Exception:
        return True


CHECK_FUNCS: Dict[str, Callable[[ScanContext, Spec], Check]] = {
    "proxy": check_proxy,
    "exit": check_exit,
    "dns": check_dns,
    "api": check_api,
    "web": check_web,
    "console": check_console,
    "statsig": check_statsig,
    "tls": check_tls,
    "latency": check_latency,
    "region": check_region,
    "timezone": check_timezone,
    "clock": check_clock,
    "local": check_local,
}

for _pid, _title, _api, _web in AI_PROVIDERS:
    def _make(pid: str, title: str, api: str, web: str):
        def _run(ctx: ScanContext, spec: Spec) -> Check:
            started = time.monotonic()
            res = http_request(api, proxy=ctx.proxy, timeout=10, headers={"Accept": "application/json"})
            data = {"status": res.status, "error": res.error, "elapsed_ms": res.elapsed_ms, "api": api, "web": web}
            if res.error:
                return _result(
                    spec, started, "fail", "无法连接",
                    f"{web} 不可达：{res.error}（经当前代理）。", data,
                )
            if res.status >= 500:
                return _result(
                    spec, started, "warn", f"服务端错误 {res.status}",
                    f"{web} 返回 {res.status}，链路通但对方服务异常，稍后重试。", data,
                )
            body = (res.body or "").lower()
            blocked = any(
                hint in body
                for hint in ("unsupported_country", "not available in your country", "country, region, or territory not supported")
            )
            if blocked:
                return _result(
                    spec, started, "fail", f"地区不可用（HTTP {res.status}）",
                    f"{web} 明确返回「该地区不支持」，换个出口地区即可。", data,
                )
            if res.status == 404:
                return _result(
                    spec, started, "warn", "接口路径不存在（HTTP 404）",
                    f"{web} 域名可达，但探测路径返回 404，可能对方调整了接口。", data,
                )
            detail = f"{web} 经当前出口可达；未带 API Key 返回 {res.status} 属正常。"
            return _result(spec, started, "pass", f"可达（HTTP {res.status}）", detail, data)
        return _run
    CHECK_FUNCS[_pid] = _make(_pid, _title, _api, _web)


# --------------------------------------------------------------------------- #
# 任务与评分
# --------------------------------------------------------------------------- #
class ScanJob:
    def __init__(self) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.state = "running"
        self.error = ""
        self.started = time.time()
        self.finished: Optional[float] = None
        self.checks: List[Check] = [
            Check(id=s.id, group=s.group, title=s.title, critical=s.critical) for s in SPECS
        ]
        self.index = {s.id: i for i, s in enumerate(SPECS)}
        self.report: Optional[Dict[str, Any]] = None
        self.lock = threading.RLock()

    def update(self, check: Check) -> None:
        with self.lock:
            self.checks[self.index[check.id]] = check

    def checkpoint(self) -> int:
        with self.lock:
            return sum(1 for c in self.checks if c.status != "pending")

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            done = sum(1 for c in self.checks if c.status != "pending")
            return {
                "id": self.id,
                "state": self.state,
                "error": self.error,
                "started": self.started,
                "finished": self.finished,
                "progress": round(done / len(self.checks), 3),
                "completed": done,
                "total": len(self.checks),
                "checks": [c.to_dict() for c in self.checks],
                "report": self.report,
            }


def _verdict(checks: List[Check]) -> Dict[str, Any]:
    penalty = 0.0
    critical_fail = False
    issues: List[str] = []
    warnings: List[str] = []
    for c in checks:
        if c.status == "fail":
            penalty += 26 if c.critical else 14
            if c.critical:
                critical_fail = True
            issues.append(c.title)
        elif c.status == "warn":
            penalty += 8
            warnings.append(c.title)
    score = int(max(0, min(100, round(100 - penalty))))
    if critical_fail or score < 55:
        verdict, headline = "fail", "当前网络无法正常使用 Claude"
    elif issues or warnings or score < 92:
        verdict, headline = "warn", "可以连接，但存在需要处理的风险项"
    else:
        verdict, headline = "pass", "当前网络满足 Claude 使用条件"

    if verdict == "pass":
        subline = "出口地区、连通性、TLS 与时区均正常。"
    elif verdict == "fail":
        subline = "优先处理：" + "、".join((issues or warnings)[:3])
    else:
        subline = "建议处理：" + "、".join((issues or warnings)[:3])
    return {
        "verdict": verdict,
        "score": score,
        "headline": headline,
        "subline": subline,
        "issues": issues,
        "warnings": warnings,
    }


def run_scan(job: ScanJob, settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    ctx = ScanContext(settings)
    started = time.monotonic()

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(CHECK_FUNCS[spec.id], ctx, spec): spec for spec in SPECS
        }
        for future in as_completed(futures):
            spec = futures[future]
            try:
                check = future.result()
            except Exception as exc:  # noqa: BLE001 - 单项失败不能拖垮整体
                check = Check(
                    id=spec.id,
                    group=spec.group,
                    title=spec.title,
                    status="fail",
                    summary="检查项异常",
                    detail=f"{type(exc).__name__}: {exc}",
                    critical=spec.critical,
                )
            job.update(check)

    with job.lock:
        checks = list(job.checks)
    summary = _verdict(checks)
    geo = ctx._geo or GeoInfo(error="未获取")
    report = {
        "version": __version__,
        "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "score": summary["score"],
        "verdict": summary["verdict"],
        "headline": summary["headline"],
        "subline": summary["subline"],
        "issues": summary["issues"],
        "warnings": summary["warnings"],
        "checks": [c.to_dict() for c in checks],
        "groups": GROUP_ORDER,
        "exit": geo.to_dict(),
        "proxy": {
            "label": ctx.proxy_info.proxy.label if ctx.proxy_info.proxy else "",
            "source": ctx.proxy_info.proxy.source if ctx.proxy_info.proxy else "",
            "tun": ctx.proxy_info.tun_active,
            "ports": ctx.proxy_info.open_ports,
        },
        "timezone": {
            "system": ctx.system_tz,
            "exit": geo.timezone,
            "system_clock": tz_clock(ctx.system_tz),
            "exit_clock": tz_clock(geo.timezone) if geo.timezone else "",
            "comparison": compare_timezones(ctx.system_tz, geo.timezone) if geo.timezone else None,
        },
        "system": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "hostname": platform.node(),
        },
    }
    with job.lock:
        job.report = report
        job.state = "done"
        job.finished = time.time()
    return report


def quick_timezone_probe(settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """自动巡检用的轻量探测：只查出口 IP 与时区，不跑全部检查。"""
    ctx = ScanContext(settings)
    geo = ctx.geo(refresh=True)
    return {
        "system_tz": ctx.system_tz,
        "exit_tz": geo.timezone,
        "exit": geo.to_dict(),
        "comparison": compare_timezones(ctx.system_tz, geo.timezone) if geo.timezone else None,
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
