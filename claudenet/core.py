"""内置 mihomo 内核：用订阅配置起一个本地实例，供节点切换与体检使用。"""
from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import env as _env
from . import store, subscription
from .clash import ClashClient, ClashEndpoint

CORE_DIR = store.data_dir() / "core"
CONFIG = CORE_DIR / "config.yaml"
LOG = CORE_DIR / "core.log"
PID_FILE = CORE_DIR / "core.pid"
RUNTIME = CORE_DIR / "runtime.json"

MIXED_PORT = 7899
API_PORT = 9096
MAX_PORT_TRIES = 6

REMOVE_KEYS = {
    "port", "socks-port", "mixed-port", "redir-port", "tproxy-port",
    "external-controller", "external-controller-unix", "external-controller-pipe",
    "secret", "allow-lan", "mode", "log-level", "ipv6", "unified-delay",
    "tun", "dns", "interface-name", "routing-mark", "authentication",
    "skip-auth-prefixes", "lan-allowed-ips", "lan-disallowed-ips",
    "bind-address", "profile", "experimental", "ntp", "sniffer",
}

BUNDLE_BIN = Path(__file__).resolve().parent.parent / "bin" / "mihomo"
TUN_ENV = CORE_DIR / "tun.env"

_proc: Optional[subprocess.Popen] = None
_secret: str = ""
_ports: Dict[str, int] = {"mixed": MIXED_PORT, "api": API_PORT}
_active: str = ""


def binary() -> Optional[Path]:
    candidates: List[str] = []
    env = _env("MIHOMO").strip()
    if env:
        candidates.append(env)
    candidates += [
        str(store.data_dir() / "bin" / "mihomo"),
        str(BUNDLE_BIN),
        "/Applications/Clash Verge.app/Contents/MacOS/verge-mihomo",
        "/Applications/Clash Verge.app/Contents/MacOS/mihomo",
        "/opt/homebrew/bin/mihomo",
        "/usr/local/bin/mihomo",
    ]
    found = shutil.which("mihomo")
    if found:
        candidates.append(found)
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return Path(path)
    return None


def _strip_block(lines: List[str]) -> List[str]:
    """删掉配置里的顶层健（含 tun / dns 这类带子项的块）。"""
    out: List[str] = []
    skipping = False
    for line in lines:
        match = re.match(r"^([A-Za-z][\w-]*):", line)
        if match:
            skipping = match.group(1) in REMOVE_KEYS
            if skipping:
                continue
        elif line and not line[0].isspace() and not line.startswith("#"):
            skipping = False
        if not skipping:
            out.append(line)
    return out


def _proxy_names(text: str) -> List[str]:
    head = text.split("proxy-groups:", 1)[0]
    match = re.search(r"(?m)^([ \t]*)-\s", head)
    if not match:
        return []
    indent = match.group(1)
    names: List[str] = []
    for block in re.split(r"(?m)^" + re.escape(indent) + r"-\s", head)[1:]:
        for match in re.finditer(r"(?m)^\s*name:\s*(.+?)\s*$", block):
            value = match.group(1).strip().strip('"').strip("'")
            if value and value not in names:
                names.append(value)
            break
    return names


def _inject_providers(text: str, providers: list) -> str:
    if not providers:
        return text
    block = []
    for item in providers:
        name = str(item.get("name") or "").strip()
        url = str(item.get("url") or "").strip()
        if not name or not url:
            continue
        behavior = str(item.get("behavior") or "domain").strip() or "domain"
        block += [
            f"  {name}:",
            "    type: http",
            f"    behavior: {behavior}",
            f"    url: {url}",
            f"    path: ./ruleset/{name}.yaml",
            "    interval: 86400",
        ]
    if not block:
        return text
    lines = text.splitlines()
    if "rule-providers:" in lines:
        idx = lines.index("rule-providers:")
        return "\n".join(lines[: idx + 1] + block + lines[idx + 1:])
    return text + "\nrule-providers:\n" + "\n".join(block)


def _inject_rules(text: str, extra: list) -> str:
    lines = text.splitlines()
    try:
        idx = lines.index("rules:")
    except ValueError:
        return text
    head, tail = lines[: idx + 1], lines[idx + 1:]
    # 订阅里的 rules 可能没有缩进，先统一成两格，避免自定义规则插进去后 YAML 断掉
    fixed = []
    for line in tail:
        if line.startswith("- "):
            fixed.append("  " + line)
        else:
            fixed.append(line)
    return "\n".join(head + [f"  - {item}" for item in extra] + fixed)


AI_DOMAINS = {
    "claude-desktop": ["claude.ai", "anthropic.com"],
    "claude-cli": ["claude.ai", "anthropic.com"],
    "chatgpt-app": ["chatgpt.com", "openai.com"],
    "chatgpt": ["chatgpt.com", "openai.com"],
    "gemini": ["gemini.google.com", "generativelanguage.googleapis.com"],
    "antigravity": ["antigravity.google"],
}


def _ai_rules(settings: Dict[str, Any]) -> list:
    routes = settings.get("ai_routes") or {}
    if not isinstance(routes, dict):
        return []
    rules = []
    for client_id, node in routes.items():
        node = str(node or "").strip()
        if not node:
            continue
        for domain in AI_DOMAINS.get(str(client_id), []):
            rules.append(f"DOMAIN-SUFFIX,{domain},{node}")
    return rules


def build_config(
    raw: str,
    mixed_port: int,
    api_port: int,
    secret: str,
    tun: bool = False,
    allow_lan: bool = False,
    ipv6: bool = False,
    log_level: str = "warning",
    tun_stack: str = "gvisor",
    custom_rules: Optional[list] = None,
    ai_rules: Optional[list] = None,
    custom_providers: Optional[list] = None,
    dns_enabled: bool = False,
    dns_mode: str = "fake-ip",
    dns_nameservers: Optional[list] = None,
    dns_fallback: Optional[list] = None,
    dns_fakeip_filter: Optional[list] = None,
) -> str:
    lines = _strip_block(raw.splitlines())
    text = "\n".join(lines).strip("\n")
    if "proxy-groups:" not in text:
        names = _proxy_names(text)
        if not names:
            raise RuntimeError("订阅里没有可用的节点")
        text += "\nproxy-groups:\n  - name: PROXY\n    type: select\n    proxies: " + str(names + ["DIRECT"])
    if "rules:" not in text:
        text += "\nrules:\n  - MATCH,PROXY"
    header = [
        f"mixed-port: {mixed_port}",
        f"external-controller: 127.0.0.1:{api_port}",
        f"secret: {secret}",
        f"allow-lan: {'true' if allow_lan else 'false'}",
        "mode: rule",
        f"log-level: {log_level}",
        f"ipv6: {'true' if ipv6 else 'false'}",
        # 与 Clash Verge 一致：延迟扣除协议握手差异，各协议可比
        "unified-delay: true",
    ]
    # TUN 会劫持 any:53，若内核没有自己的 DNS 会退回系统解析器（ISP 泄漏），
    # 所以 TUN 模式下强制启用 DoH DNS，不依赖 dns_enabled 开关。
    use_dns = dns_enabled or tun
    if use_dns:
        header += [
            "dns:",
            "  enable: true",
            f"  enhanced-mode: {dns_mode or 'fake-ip'}",
            "  fake-ip-range: 198.18.0.1/16",
            "  listen: 127.0.0.1:53",
            "  proxy-server-nameserver:",
            "    - 223.5.5.5",
            "    - 119.29.29.29",
            "  nameserver:",
            *[
                f"    - {item}"
                for item in (
                    dns_nameservers
                    or ["https://dns.alidns.com/dns-query", "https://doh.pub/dns-query"]
                )
            ],
        ]
        if dns_fallback:
            header += ["  fallback:", *[f"    - {item}" for item in dns_fallback]]
        filters = dns_fakeip_filter if dns_fakeip_filter else ["*.lan", "*.local", "localhost.ptlogin2.qq.com"]
        if filters:
            header += ["  fake-ip-filter:", *[f"    - '{item}'" for item in filters]]
    if tun:
        header += [
            "tun:",
            "  enable: true",
            f"  stack: {tun_stack or 'gvisor'}",
            "  auto-route: true",
            "  auto-detect-interface: true",
            "  dns-hijack:",
            "    - any:53",
        ]
    text = _inject_providers(text, list(custom_providers or []))
    text = _inject_rules(text, list(ai_rules or []) + list(custom_rules or []))
    return "\n".join(header) + "\n" + text + "\n"


def _port_free(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _pick_ports(preferred: int = 0) -> Dict[str, int]:
    if preferred and 1024 <= preferred <= 65535:
        api = API_PORT + max(0, preferred - MIXED_PORT)
        if _port_free(preferred) and _port_free(api):
            return {"mixed": preferred, "api": api}
    for offset in range(MAX_PORT_TRIES):
        mixed = MIXED_PORT + offset
        api = API_PORT + offset
        if _port_free(mixed) and _port_free(api):
            return {"mixed": mixed, "api": api}
    raise RuntimeError("本地端口被占用（7899 / 9096 起都不可用）")


def endpoint() -> ClashEndpoint:
    api, secret = (_ports or {}).get("api"), _secret
    # 内核可能是上一次进程/launchd 拉起的，内存里的端口和 secret 可能为空，以 runtime.json 为准
    try:
        data = json.loads(RUNTIME.read_text(encoding="utf-8"))
        api = int(data.get("api") or api or API_PORT)
        secret = str(data.get("secret") or secret or "")
    except (OSError, ValueError):
        api = int(api or API_PORT)
        secret = secret or ""
    return ClashEndpoint("http", f"127.0.0.1:{api}", secret, source="内置内核")


def _read_pid() -> Optional[int]:
    try:
        return int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None


def _write_pid(pid: int) -> None:
    try:
        PID_FILE.write_text(str(pid), encoding="utf-8")
    except OSError:
        pass


def _alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def adopt() -> Optional[Dict[str, Any]]:
    """服务重启后接管仍在运行的内核。"""
    global _secret, _ports, _active
    try:
        data = json.loads(RUNTIME.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not _alive(int(data.get("pid") or 0)):
        return None
    _secret = str(data.get("secret") or "")
    _ports = {"mixed": int(data.get("mixed")), "api": int(data.get("api"))}
    _active = str(data.get("active") or "")
    os.environ["CLASHLINK_PROXY"] = f"http://127.0.0.1:{_ports['mixed']}"
    return status()


def status() -> Dict[str, Any]:
    global _proc
    running = _proc is not None and _proc.poll() is None
    if not running:
        pid = _read_pid()
        running = _alive(pid)
        if not running and PID_FILE.exists():
            try:
                PID_FILE.unlink()
            except OSError:
                pass
    if not running:
        # 可能是 launchd 拉起的 root 内核，进程还在但不归本进程管
        root_pid = _find_core_pid(root_only=True)
        meta = _config_meta()
        if root_pid and meta and _tun_device_up():
            try:
                probe = ClashClient(
                    ClashEndpoint("http", f"127.0.0.1:{meta['api']}", str(meta["secret"]), "core"),
                    timeout=1.5,
                )
                probe.version()
                _write_pid(root_pid)
                global _ports, _secret, _active
                _ports = {"mixed": meta["mixed"], "api": meta["api"]}
                _secret = str(meta["secret"])
                _active = _active or str(store.load_settings().get("active_sub") or "")
                os.environ["CLASHLINK_PROXY"] = f"http://127.0.0.1:{meta['mixed']}"
                RUNTIME.write_text(
                    json.dumps(
                        {"pid": root_pid, "secret": _secret, "mixed": meta["mixed"],
                         "api": meta["api"], "active": _active, "tun": True},
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                running = True
            except Exception:  # noqa: BLE001
                running = False
    version = ""
    if running:
        try:
            client = ClashClient(endpoint(), timeout=1.5)
            version = str(client.version().get("version") or "")
        except Exception:  # noqa: BLE001
            running = False
    try:
        tun_mode = bool(json.loads(RUNTIME.read_text(encoding="utf-8")).get("tun")) or (
            running and _tun_device_up() and _is_root_pid(_read_pid() or 0)
        )
    except (OSError, ValueError):
        tun_mode = False
    if tun_mode:
        pid_now = _read_pid() or 0
        tun_mode = running and _is_root_pid(pid_now) and _tun_device_up()
    return {
        "running": running,
        "tun": tun_mode,
        "pid": _proc.pid if (running and _proc) else _read_pid(),
        "version": version,
        "mixed_port": _ports["mixed"],
        "api_port": _ports["api"],
        "active": _active or store.load_settings().get("active_sub") or "",
        "binary": str(binary() or ""),
        "log": str(LOG),
    }



def _config_meta() -> Dict[str, Any]:
    """从现有 config.yaml 里读出端口和 secret，用于接管已经在跑的内核。"""
    try:
        text = CONFIG.read_text(encoding="utf-8")
    except OSError:
        return {}
    meta: Dict[str, Any] = {"secret": ""}
    for line in text.splitlines()[:12]:
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key == "mixed-port":
            try:
                meta["mixed"] = int(value)
            except ValueError:
                pass
        elif key == "external-controller" and ":" in value:
            try:
                meta["api"] = int(value.rsplit(":", 1)[1])
            except ValueError:
                pass
        elif key == "secret":
            meta["secret"] = value.strip('"')
    return meta if "mixed" in meta and "api" in meta else {}


def _tun_device_up() -> bool:
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=4).stdout
    except (subprocess.SubprocessError, OSError):
        return False
    return "198.18.0.1" in out


TUN_JOB = "io.clashlink.core"
TUN_HELPER = "/usr/local/bin/clashlink-tun"


def write_tun_env() -> Path:
    """把内核路径写到 tun.env，helper 起 TUN 时据此生成 LaunchDaemon。"""
    import shlex

    CORE_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        f"MIHOMO={shlex.quote(str(binary() or ''))}",
        f"CONFIG={shlex.quote(str(CONFIG))}",
        f"CORE_DIR={shlex.quote(str(CORE_DIR))}",
        f"LOG={shlex.quote(str(LOG))}",
    ]
    TUN_ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(TUN_ENV, 0o644)
    return TUN_ENV


def _helper_ready() -> bool:
    try:
        return "helper-version: 2" in Path(TUN_HELPER).read_text(encoding="utf-8")
    except OSError:
        return False


def _admin_tun(action: str) -> None:
    """用安装过的 root helper 免密起停 TUN；没装就走管理员授权。"""
    if _helper_ready():
        if action == "on":
            write_tun_env()
        proc = subprocess.run(
            ["/usr/bin/sudo", "-n", TUN_HELPER, action],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or "TUN 提权失败").strip())
        return
    if action == "on":
        _admin(f'/bin/launchctl submit -l {TUN_JOB} -- "{binary()}" -f "{CONFIG}" -d "{CORE_DIR}"')
    else:
        _admin(f"/bin/launchctl remove {TUN_JOB} 2>/dev/null || true")


def _remove_tun_job() -> None:
    try:
        _admin_tun("off")
    except Exception:  # noqa: BLE001
        pass


def _listening_ports(pid: int) -> list:
    """用 lsof 查这个 pid 正在监听的 TCP 端口。"""
    try:
        out = subprocess.run(
            ["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN", "-Fn"],
            capture_output=True, text=True, timeout=4,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    ports = []
    for line in out.splitlines():
        if line.startswith("n") and ":" in line:
            try:
                ports.append(int(line.rsplit(":", 1)[1]))
            except ValueError:
                continue
    return ports


def _kill_all(admin_for_root: bool = True) -> None:
    """结束所有使用本程序配置的 mihomo（含 root 残留）。"""
    try:
        out = subprocess.run(
            ["pgrep", "-f", f"mihomo -f {CONFIG}"], capture_output=True, text=True, timeout=4
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return
    root_pids = []
    for line in out.split():
        try:
            pid = int(line)
        except ValueError:
            continue
        if _is_root_pid(pid):
            root_pids.append(pid)
            continue
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    if root_pids and admin_for_root and not _helper_ready():
        try:
            _admin("; ".join(f"kill {pid}" for pid in root_pids))
        except Exception:  # noqa: BLE001
            pass


_LOCK = threading.RLock()
_BUSY = False


def is_busy() -> bool:
    return _BUSY


def stop() -> None:
    global _BUSY
    with _LOCK:
        _BUSY = True
        try:
            _stop_locked()
        finally:
            _BUSY = False


def _restore_dns_if_needed() -> None:
    restore_dns_from_backup()


def _stop_locked() -> None:
    global _proc, _active
    _restore_dns_if_needed()
    if _find_core_pid(root_only=True):
        try:
            _admin_tun("off")
        except Exception:  # noqa: BLE001
            pass
    os.environ.pop("CLASHLINK_PROXY", None)
    proc = _proc
    pid = _read_pid()
    for candidate in (proc, None):
        if candidate is not None:
            try:
                candidate.terminate()
                candidate.wait(timeout=5)
            except (subprocess.SubprocessError, OSError):
                try:
                    candidate.kill()
                except (subprocess.SubprocessError, OSError):
                    pass
    if _alive(pid):
        try:
            os.kill(pid, 15)
        except OSError:
            # root 进程要管理员权限才能结束
            try:
                if _is_root_pid(pid):
                    _admin(f"kill {pid}")
            except Exception:  # noqa: BLE001
                pass
    _kill_all()
    if _is_root_pid(pid or 0):
        _remove_tun_job()
    _proc = None
    _active = ""
    for path in (PID_FILE, RUNTIME):
        try:
            path.unlink()
        except OSError:
            pass


def start(item: Dict[str, Any], tun: bool = False, preferred_port: int = 0) -> Dict[str, Any]:
    global _BUSY
    with _LOCK:
        _BUSY = True
        try:
            return _start_locked(item, tun, preferred_port)
        finally:
            _BUSY = False


def rewrite_config() -> str:
    """按当前 settings 重写 config.yaml（不重启内核），供 /configs reload 使用。"""
    st = store.load_settings()
    item = subscription.find(str(st.get("active_sub") or ""))
    if item is None:
        raise RuntimeError("还没有启用订阅")
    try:
        data = json.loads(RUNTIME.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RuntimeError("内核没有在运行")
    raw = subscription.read_config(item)
    CONFIG.write_text(
        build_config(
            raw,
            int(data.get("mixed") or MIXED_PORT),
            int(data.get("api") or API_PORT),
            str(data.get("secret") or ""),
            tun=bool(data.get("tun")),
            allow_lan=bool(st.get("allow_lan")),
            ipv6=bool(st.get("ipv6")),
            log_level=str(st.get("log_level") or "warning"),
            tun_stack=str(st.get("tun_stack") or "gvisor"),
            custom_rules=st.get("custom_rules") or [],
            ai_rules=_ai_rules(st),
            custom_providers=st.get("custom_providers") or [],
            dns_enabled=bool(st.get("dns_enabled")),
            dns_mode=str(st.get("dns_mode") or "fake-ip"),
            dns_nameservers=st.get("dns_nameservers") or [],
            dns_fallback=st.get("dns_fallback") or [],
            dns_fakeip_filter=st.get("dns_fakeip_filter") or [],
        ),
        encoding="utf-8",
    )
    return str(CONFIG)


def _start_locked(item: Dict[str, Any], tun: bool = False, preferred_port: int = 0) -> Dict[str, Any]:
    global _proc, _secret, _ports, _active
    exe = binary()
    if exe is None:
        raise RuntimeError("没有找到 mihomo 内核（安装 Clash Verge 或设置 CLASHLINK_MIHOMO）")
    _stop_locked()
    raw = subscription.read_config(item)
    _ports = _pick_ports(preferred_port)
    _secret = secrets.token_hex(16)
    CORE_DIR.mkdir(parents=True, exist_ok=True)
    _st = store.load_settings()
    CONFIG.write_text(
        build_config(
            raw,
            _ports["mixed"],
            _ports["api"],
            _secret,
            tun=tun,
            allow_lan=bool(_st.get("allow_lan")),
            ipv6=bool(_st.get("ipv6")),
            log_level=str(_st.get("log_level") or "warning"),
            tun_stack=str(_st.get("tun_stack") or "gvisor"),
            custom_rules=_st.get("custom_rules") or [],
            ai_rules=_ai_rules(_st),
            custom_providers=_st.get("custom_providers") or [],
            dns_enabled=bool(_st.get("dns_enabled")),
            dns_mode=str(_st.get("dns_mode") or "fake-ip"),
            dns_nameservers=_st.get("dns_nameservers") or [],
            dns_fallback=_st.get("dns_fallback") or [],
            dns_fakeip_filter=_st.get("dns_fakeip_filter") or [],
        ),
        encoding="utf-8",
    )

    log_handle = LOG.open("a", encoding="utf-8")
    log_handle.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} start {item.get('name')} ---\n")
    log_handle.flush()
    if tun:
        # TUN 需要 root：用管理员授权后台拉起，进程不归本进程 direct 管
        # 用 launchd 提交，进程由 launchd 接管，不会随 osascript 会话退出
        pid = 0
        for attempt in range(2):
            _admin_tun("on")
            for _ in range(20):
                time.sleep(0.3)
                candidate = _find_core_pid(root_only=True)
                if candidate:
                    pid = candidate
                    break
            if pid:
                break
            time.sleep(0.6)
        if not pid:
            _kill_all()
            raise RuntimeError("TUN 需要 root，但内核没有以管理员身份启动")
        _write_pid(pid)
        proc = None
        _proc = None
    else:
        proc = subprocess.Popen(
            [str(exe), "-f", str(CONFIG), "-d", str(CORE_DIR)],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env={
                "HOME": os.environ.get("HOME", ""),
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "TZ": os.environ.get("TZ", ""),
            },
        )
        _proc = proc
        _write_pid(proc.pid)

    deadline = time.time() + 12
    api_port = _ports["api"]
    if tun and _read_pid():
        for _ in range(20):
            found = _listening_ports(_read_pid())
            if _ports["mixed"] in found and _ports["api"] in found:
                break
            if found and _ports["mixed"] in found and _ports["api"] not in found:
                api_port = next((port for port in found if port != _ports["mixed"]), _ports["api"])
                break
            time.sleep(0.3)
    client = ClashClient(ClashEndpoint("http", f"127.0.0.1:{api_port}", _secret, "core"), timeout=1.5)
    last_error = ""
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            last_error = _log_tail()
            break
        try:
            client.version()
            _active = item["id"]
            os.environ["CLASHLINK_PROXY"] = f"http://127.0.0.1:{_ports['mixed']}"
            RUNTIME.write_text(
                json.dumps(
                    {"pid": proc.pid if proc is not None else _read_pid(),
                     "secret": _secret, "mixed": _ports["mixed"],
                     "api": _ports["api"], "active": _active, "tun": bool(tun)},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            settings = store.load_settings()
            settings["active_sub"] = item["id"]
            if tun:
                takeover_dns()
            store.save_settings(settings)
            return status()
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            time.sleep(0.25)
    _kill_all()
    _stop_locked()
    raise RuntimeError(f"内核启动失败：{last_error or '超时'}")


# ---------------------------------------------------------------- 系统代理



def get_system_dns(service: str) -> List[str]:
    try:
        out = subprocess.run(
            ["networksetup", "-getdnsservers", service], capture_output=True, text=True, timeout=8
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    if "aren't any DNS Servers" in out:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _helper(*args: str) -> bool:
    if not _helper_ready():
        return False
    proc = subprocess.run(["/usr/bin/sudo", "-n", TUN_HELPER, *args], capture_output=True, text=True, timeout=60)
    return proc.returncode == 0


def set_system_dns(service: str, servers: List[str]) -> None:
    if servers:
        if _helper("dns", "set", service, *servers):
            return
        args = " ".join(shlex.quote(item) for item in servers)
        _admin(f'/usr/sbin/networksetup -setdnsservers {shlex.quote(service)} {args}')
    else:
        if _helper("dns", "dhcp", service):
            return
        _admin(f'/usr/sbin/networksetup -setdnsservers {shlex.quote(service)} "Empty"')


DNS_BACKUP = CORE_DIR / "dns_backup.json"


def takeover_dns() -> Dict[str, Any]:
    """TUN 时把系统 DNS 指到内核（127.0.0.1:53），防止 LAN/ISP DNS 泄漏。"""
    service = active_service()
    previous = get_system_dns(service)
    if previous == ["127.0.0.1"]:
        return {"ok": True, "service": service, "previous": previous, "changed": False}
    # 先落盘再改，进程中途退出也能在下次启动时还原
    try:
        CORE_DIR.mkdir(parents=True, exist_ok=True)
        DNS_BACKUP.write_text(
            json.dumps({"service": service, "previous": previous}, ensure_ascii=False),
            encoding="utf-8",
        )
        set_system_dns(service, ["127.0.0.1"])
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "service": service, "error": str(exc), "previous": previous}
    return {"ok": True, "service": service, "previous": previous, "changed": True}


def restore_dns_from_backup() -> bool:
    try:
        data = json.loads(DNS_BACKUP.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    service = str(data.get("service") or "")
    previous = data.get("previous") or []
    if not service:
        return False
    try:
        set_system_dns(service, previous)
    except Exception:  # noqa: BLE001
        return False
    try:
        DNS_BACKUP.unlink()
    except OSError:
        pass
    return True


def restore_dns(service: str, previous: List[str]) -> bool:
    try:
        set_system_dns(service, previous or [])
        return True
    except Exception:  # noqa: BLE001
        return False

def active_service() -> str:
    """当前默认路由所在网络服务名（Wi-Fi / Ethernet …）。"""
    try:
        route = subprocess.run(
            ["route", "-n", "get", "default"], capture_output=True, text=True, timeout=5
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return "Wi-Fi"
    match = re.search(r"interface:\s*(\S+)", route)
    if not match:
        return "Wi-Fi"
    device = match.group(1)
    try:
        order = subprocess.run(
            ["networksetup", "-listnetworkserviceorder"], capture_output=True, text=True, timeout=5
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return "Wi-Fi"
    lines = order.splitlines()
    for index, line in enumerate(lines):
        if f"Device: {device})" in line and index > 0:
            prev = lines[index - 1]
            name = re.sub(r"^\(\d+\)\s*", "", prev).strip()
            if name:
                return name
    return "Wi-Fi"


def system_proxy() -> Dict[str, Any]:
    try:
        out = subprocess.run(["scutil", "--proxy"], capture_output=True, text=True, timeout=5).stdout
    except (subprocess.SubprocessError, OSError):
        return {"enabled": False}
    enabled = re.search(r"HTTPEnable\s*:\s*1", out) is not None
    host = re.search(r"HTTPProxy\s*:\s*(\S+)", out)
    port = re.search(r"HTTPPort\s*:\s*(\d+)", out)
    return {
        "enabled": enabled,
        "host": host.group(1) if host else "",
        "port": int(port.group(1)) if port else 0,
        "service": active_service(),
    }


def _passwordless_net() -> bool:
    try:
        proc = subprocess.run(
            ["/usr/bin/sudo", "-n", "/usr/sbin/networksetup", "-getwebproxy", "Wi-Fi"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return proc.returncode == 0


def _admin(script: str) -> None:
    if _passwordless_net():
        proc = subprocess.run(
            ["/bin/sh", "-c", script.replace("sudo ", "/usr/bin/sudo -n ")],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or "执行失败").strip())
        return
    escaped = script.replace('\\', '\\\\').replace('"', '\\"')
    proc = subprocess.run(
        ["osascript", "-e", f'do shell script "{escaped}" with administrator privileges'],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "管理员授权失败").strip())


def _net_get(service: str, kind: str) -> Dict[str, Any]:
    flag = {"web": "-getwebproxy", "secure": "-getsecurewebproxy", "socks": "-getsocksfirewallproxy"}[kind]
    try:
        out = subprocess.run(["networksetup", flag, service], capture_output=True, text=True, timeout=6).stdout
    except (subprocess.SubprocessError, OSError):
        return {"enabled": False, "server": "", "port": 0}
    enabled = re.search(r"Enabled:\s*(Yes|No)", out)
    server = re.search(r"Server:\s*(\S+)", out)
    port = re.search(r"Port:\s*(\d+)", out)
    return {
        "enabled": bool(enabled and enabled.group(1) == "Yes"),
        "server": server.group(1) if server else "",
        "port": int(port.group(1)) if port else 0,
    }


def capture_proxy_state() -> Dict[str, Any]:
    """记录接管前的系统代理，退出/断开时按原样还原。"""
    service = active_service()
    try:
        bypass_out = subprocess.run(
            ["networksetup", "-getproxybypassdomains", service], capture_output=True, text=True, timeout=6
        ).stdout
    except (subprocess.SubprocessError, OSError):
        bypass_out = ""
    bypass = [
        line.strip()
        for line in bypass_out.splitlines()
        if line.strip() and "aren't any" not in line.lower()
    ]
    return {
        "service": service,
        "web": _net_get(service, "web"),
        "secure": _net_get(service, "secure"),
        "socks": _net_get(service, "socks"),
        "bypass": bypass,
    }


def _restore_script(state: Dict[str, Any]) -> str:
    service = state.get("service") or "Wi-Fi"
    parts = []
    for kind, setter, stater in (
        ("web", "-setwebproxy", "-setwebproxystate"),
        ("secure", "-setsecurewebproxy", "-setsecurewebproxystate"),
        ("socks", "-setsocksfirewallproxy", "-setsocksfirewallproxystate"),
    ):
        conf = state.get(kind) or {}
        if conf.get("enabled") and conf.get("server"):
            parts.append(f'sudo networksetup {setter} "{service}" {conf["server"]} {int(conf.get("port") or 0)}')
        else:
            parts.append(f'sudo networksetup {stater} "{service}" off')
    bypass = state.get("bypass") or []
    if bypass:
        parts.append('sudo networksetup -setproxybypassdomains "{service}" ' + " ".join(bypass))
    return "; ".join(parts)


def proxy_backup() -> Optional[Dict[str, Any]]:
    return (store.load_settings() or {}).get("proxy_backup") or None


def _save_backup(state: Optional[Dict[str, Any]]) -> None:
    settings = store.load_settings()
    if state:
        settings["proxy_backup"] = state
    else:
        settings.pop("proxy_backup", None)
    store.save_settings(settings)


def restore_system_proxy() -> Dict[str, Any]:
    """把系统代理还原成接管前的样子（没有记录时就全部关掉）。"""
    backup = proxy_backup()
    if backup:
        _admin(_restore_script(backup))
        _save_backup(None)
    else:
        service = active_service()
        if not _helper("proxy", "off", service):
            _admin(
                f'sudo networksetup -setwebproxystate "{service}" off; '
                f'sudo networksetup -setsecurewebproxystate "{service}" off; '
                f'sudo networksetup -setsocksfirewallproxystate "{service}" off'
            )
    state = system_proxy()
    state["ok"] = True
    return state


def set_system_proxy(on: bool) -> Dict[str, Any]:
    service = active_service()
    if on:
        snap = status()
        if not snap.get("running"):
            raise RuntimeError("请先启用一个订阅")
        port = int(snap.get("mixed_port") or _ports["mixed"] or 0)
    if on and not proxy_backup():
        backup = capture_proxy_state()
        # 已经指向本内核就不用重复记录
        already = (backup.get("web") or {}).get("port") == port
        if not already:
            _save_backup(backup)
    if on:
        if not _helper("proxy", "on", service, "127.0.0.1", str(port)):
            script = (
                f'sudo networksetup -setwebproxy "{service}" 127.0.0.1 {port}; '
                f'sudo networksetup -setsecurewebproxy "{service}" 127.0.0.1 {port}; '
                f'sudo networksetup -setsocksfirewallproxy "{service}" 127.0.0.1 {port}; '
                f'sudo networksetup -setproxybypassdomains "{service}" 127.0.0.1 localhost *.local'
            )
            _admin(script)
    else:
        return restore_system_proxy()
    return _after_proxy_change()


def _after_proxy_change() -> Dict[str, Any]:
    state = system_proxy()
    state["ok"] = True
    return state


def _is_root_pid(pid: int) -> bool:
    try:
        out = subprocess.run(["ps", "-o", "user=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout
    except (subprocess.SubprocessError, OSError):
        return False
    return out.strip() == "root"


def _find_core_pid(root_only: bool = False) -> int:
    try:
        out = subprocess.run(["pgrep", "-f", str(CONFIG)], capture_output=True, text=True, timeout=5).stdout
    except (subprocess.SubprocessError, OSError):
        return 0
    for line in out.splitlines():
        try:
            return int(line.strip())
        except ValueError:
            continue
    return 0


def _log_tail(lines: int = 6) -> str:
    try:
        content = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return " | ".join(content[-lines:])


def ipv6_state() -> Optional[bool]:
    """活动物理网卡当前是否启用 IPv6；无法判断时返回 None。"""
    service = active_service()
    if not service:
        return None
    try:
        out = subprocess.run(
            ["/usr/sbin/networksetup", "-getinfo", service], capture_output=True, text=True, timeout=8
        ).stdout
    except Exception:  # noqa: BLE001
        return None
    for line in out.splitlines():
        if line.lower().startswith("ipv6:"):
            return "off" not in line.lower()
    return None


def set_ipv6(on: bool) -> Dict[str, Any]:
    service = active_service()
    if not service:
        raise RuntimeError("找不到当前网络服务")
    flag = "-setv6automatic" if on else "-setv6off"
    _admin(f'/usr/sbin/networksetup {flag} "{service}"')
    return {"enabled": on, "service": service, "state": ipv6_state()}
