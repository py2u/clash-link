"""底层网络能力：代理发现、HTTP 请求、TLS 握手探测。

只依赖 Python 标准库，避免用户为了跑一个体检工具去装依赖。
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from . import env as _env

CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 10.0
MAX_BODY = 256 * 1024

# 常见的本地代理端口：Clash / Clash Verge / mihomo / Surge / V2Ray 等
COMMON_PROXY_PORTS = (7897, 7890, 7891, 1087, 1080, 6152, 8888, 8080)


# --------------------------------------------------------------------------- #
# 代理
# --------------------------------------------------------------------------- #
@dataclass
class Proxy:
    kind: str  # "http" | "socks5"
    host: str
    port: int
    source: str = ""

    @property
    def label(self) -> str:
        return f"{self.kind}://{self.host}:{self.port}"


@dataclass
class ProxyInfo:
    proxy: Optional[Proxy] = None
    env_proxy: Optional[Proxy] = None
    system_proxy: Optional[Proxy] = None
    open_ports: List[int] = field(default_factory=list)
    tun_active: bool = False
    fake_ip_prefix: str = ""
    notes: List[str] = field(default_factory=list)


def parse_proxy_url(value: str, source: str = "") -> Optional[Proxy]:
    value = (value or "").strip()
    if not value:
        return None
    if "://" not in value:
        value = "http://" + value
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if not host:
        return None
    scheme = (parts.scheme or "http").lower()
    kind = "socks5" if scheme.startswith("socks") else "http"
    if port is None:
        port = 1080 if kind == "socks5" else 8080
    return Proxy(kind=kind, host=host, port=port, source=source)


def _env_proxy() -> Optional[Proxy]:
    for key in ("https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY", "http_proxy", "HTTP_PROXY"):
        raw = os.environ.get(key)
        if raw:
            p = parse_proxy_url(raw, source=f"环境变量 {key}")
            if p:
                return p
    return None


def _system_proxy_macos() -> Optional[Proxy]:
    if os.uname().sysname != "Darwin":
        return None
    try:
        out = subprocess.run(
            ["scutil", "--proxy"], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception:
        return None

    data: Dict[str, str] = {}
    for line in out.splitlines():
        m = re.match(r"\s*([A-Za-z]+)\s*:\s*(.+?)\s*$", line)
        if m:
            data[m.group(1)] = m.group(2).strip()

    if data.get("HTTPSEnable") == "1" and data.get("HTTPSProxy"):
        return Proxy(
            "http", data["HTTPSProxy"], int(data.get("HTTPSPort", "8080")), "系统代理 (HTTPS)"
        )
    if data.get("HTTPEnable") == "1" and data.get("HTTPProxy"):
        return Proxy(
            "http", data["HTTPProxy"], int(data.get("HTTPPort", "8080")), "系统代理 (HTTP)"
        )
    if data.get("SOCKSEnable") == "1" and data.get("SOCKSProxy"):
        return Proxy(
            "socks5", data["SOCKSProxy"], int(data.get("SOCKSPort", "1080")), "系统代理 (SOCKS5)"
        )
    if data.get("ProxyAutoConfigEnable") == "1":
        return Proxy("http", "", 0, "PAC 自动配置")  # 仅作提示，不能直接用
    return None


def port_open(host: str, port: int, timeout: float = 0.2) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex((host, port)) == 0
    except OSError:
        return False


def resolve_ips(host: str, timeout: float = 3.0) -> List[str]:
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        seen: List[str] = []
        for info in infos:
            ip = info[4][0]
            if ip not in seen:
                seen.append(ip)
        return seen
    except OSError:
        return []
    finally:
        socket.setdefaulttimeout(old)


def _is_fake_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr in ipaddress.ip_network("198.18.0.0/15")


def detect_proxy() -> ProxyInfo:
    """按 环境变量 > 系统代理 > 常见本地端口 的顺序确定出口方式。"""
    info = ProxyInfo()
    info.env_proxy = _env_proxy()
    info.system_proxy = _system_proxy_macos()

    for port in COMMON_PROXY_PORTS:
        if port_open("127.0.0.1", port):
            info.open_ports.append(port)

    ips = resolve_ips("claude.ai")
    fake = [ip for ip in ips if _is_fake_ip(ip)]
    info.tun_active = bool(fake)
    if fake:
        info.fake_ip_prefix = "198.18.0.0/15"
        info.notes.append(f"TUN + fake-ip 已生效（claude.ai → {fake[0]}）")

    if info.system_proxy and not info.system_proxy.host:
        info.notes.append("检测到 PAC 自动代理，但无法直接读取代理地址")
        info.system_proxy = None

    forced = _env("PROXY").strip()
    if forced:
        parsed = parse_proxy_url(forced, source="内置内核")
        if parsed:
            info.proxy = parsed
            info.notes.append(f"出口代理：{parsed.label}（内置内核）")
            return info

    chosen = info.env_proxy or info.system_proxy
    if chosen is None and info.open_ports:
        chosen = Proxy("http", "127.0.0.1", info.open_ports[0], "本地代理端口")
    if chosen:
        info.proxy = chosen
        info.notes.append(f"出口代理：{chosen.label}（{chosen.source}）")
    elif info.tun_active:
        info.notes.append("未发现代理端口，但 TUN 已透明接管流量")
    else:
        info.notes.append("未发现代理，按直连检测")

    return info


# --------------------------------------------------------------------------- #
# 连接
# --------------------------------------------------------------------------- #
def _connect(host: str, port: int, proxy: Optional[Proxy], timeout: float) -> socket.socket:
    if proxy is None:
        return socket.create_connection((host, port), timeout=timeout)
    if proxy.kind == "socks5":
        raise OSError("暂不支持 SOCKS5 探测，请改用 HTTP 混合端口")
    sock = socket.create_connection((proxy.host, proxy.port), timeout=timeout)
    try:
        req = (
            f"CONNECT {host}:{port} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Proxy-Connection: keep-alive\r\n\r\n"
        )
        sock.sendall(req.encode("ascii"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError("代理在 CONNECT 阶段关闭了连接")
            buf += chunk
            if len(buf) > 65536:
                raise OSError("代理响应异常")
        status_line = buf.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        if " 200 " not in status_line + " ":
            raise OSError(f"代理 CONNECT 失败：{status_line}")
        return sock
    except Exception:
        sock.close()
        raise


@dataclass
class TLSProbe:
    ok: bool = False
    host: str = ""
    connect_ms: int = 0
    tls_ms: int = 0
    total_ms: int = 0
    version: str = ""
    cipher: str = ""
    verified: bool = True
    issuer: str = ""
    subject: str = ""
    not_after: str = ""
    error: str = ""


def _cert_text(cert: Dict[str, Any], key: str) -> str:
    parts = cert.get(key, ())
    flat: List[str] = []
    for rdn in parts:
        for item in rdn:
            if len(item) == 2:
                flat.append(f"{item[0]}={item[1]}")
    return ", ".join(flat)


def probe_tls(
    host: str,
    port: int = 443,
    proxy: Optional[Proxy] = None,
    timeout: float = 8.0,
) -> TLSProbe:
    """做一次完整的 TCP + TLS 握手，返回耗时与证书信息。"""
    probe = TLSProbe(host=host)
    started = time.monotonic()
    try:
        raw = _connect(host, port, proxy, timeout)
        connected = time.monotonic()
        ctx = ssl.create_default_context()
        try:
            ss = ctx.wrap_socket(raw, server_hostname=host)
        except ssl.SSLCertVerificationError as exc:
            raw.close()
            probe.verified = False
            probe.error = f"证书校验失败：{exc.verify_message or exc}"
            # 再握一次但不校验，至少拿到协议版本，便于判断是不是中间人
            try:
                raw2 = _connect(host, port, proxy, timeout)
                ctx2 = ssl._create_unverified_context()
                ss2 = ctx2.wrap_socket(raw2, server_hostname=host)
                probe.version = ss2.version() or ""
                probe.cipher = (ss2.cipher() or [""])[0]
                ss2.close()
            except Exception:
                pass
            probe.total_ms = int((time.monotonic() - started) * 1000)
            return probe

        finished = time.monotonic()
        cert = ss.getpeercert() or {}
        probe.ok = True
        probe.connect_ms = int((connected - started) * 1000)
        probe.tls_ms = int((finished - connected) * 1000)
        probe.total_ms = int((finished - started) * 1000)
        probe.version = ss.version() or ""
        probe.cipher = (ss.cipher() or [""])[0]
        probe.issuer = _cert_text(cert, "issuer")
        probe.subject = _cert_text(cert, "subject")
        probe.not_after = cert.get("notAfter", "")
        ss.close()
        return probe
    except Exception as exc:  # noqa: BLE001 - 需要把任意网络异常转成可展示文本
        probe.error = f"{type(exc).__name__}: {exc}"
        probe.total_ms = int((time.monotonic() - started) * 1000)
        return probe


@dataclass
class HttpResult:
    url: str = ""
    status: int = 0
    headers: Dict[str, str] = field(default_factory=dict)
    body: str = ""
    raw: bytes = b""
    error: str = ""
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status > 0 and not self.error

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except Exception:
            return None

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)


def http_request(
    url: str,
    proxy: Optional[Proxy] = None,
    timeout: float = DEFAULT_TIMEOUT,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    max_body: int = MAX_BODY,
) -> HttpResult:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    port = parts.port or (443 if scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    merged = {
        "User-Agent": CHROME_UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "close",
    }
    if headers:
        merged.update(headers)

    started = time.monotonic()
    result = HttpResult(url=url)
    conn = None
    try:
        if proxy and proxy.kind == "socks5":
            raise OSError("暂不支持 SOCKS5 代理探测，请使用 HTTP 混合端口")
        if scheme == "https":
            ctx = ssl.create_default_context()
            if proxy:
                conn = http.client.HTTPSConnection(
                    proxy.host, proxy.port, timeout=timeout, context=ctx
                )
                conn.set_tunnel(host, port)
            else:
                conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ctx)
        else:
            if proxy:
                conn = http.client.HTTPConnection(proxy.host, proxy.port, timeout=timeout)
                conn.set_tunnel(host, port)
            else:
                conn = http.client.HTTPConnection(host, port, timeout=timeout)

        conn.request(method, path, body=body, headers=merged)
        resp = conn.getresponse()
        raw = resp.read(max_body)
        result.status = resp.status
        result.headers = {k.lower(): v for k, v in resp.getheaders()}
        result.raw = raw
        result.body = raw.decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
    return result


def fetch_json(url: str, proxy: Optional[Proxy], timeout: float = 8.0) -> Tuple[Any, str]:
    res = http_request(url, proxy=proxy, timeout=timeout, headers={"Accept": "application/json"})
    if not res.ok:
        return None, res.error or f"HTTP {res.status}"
    data = res.json()
    if data is None:
        return None, f"响应不是 JSON（HTTP {res.status}）"
    return data, ""


def fetch_trace(proxy: Optional[Proxy], timeout: float = 8.0) -> Dict[str, str]:
    """读取 Cloudflare 的 /cdn-cgi/trace，获取 Claude 边缘看到的出口 IP 与地区。"""
    for url in ("https://claude.ai/cdn-cgi/trace", "https://www.cloudflare.com/cdn-cgi/trace"):
        res = http_request(url, proxy=proxy, timeout=timeout)
        if res.ok and "ip=" in res.body:
            data: Dict[str, str] = {}
            for line in res.body.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    data[k.strip()] = v.strip()
            return data
    return {}
