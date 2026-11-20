"""系统时区读取、对比与一键修改（macOS 为主，兼容 Linux/Windows）。"""
from __future__ import annotations

import base64
import hashlib
import os
import pathlib
import platform
import re
import shlex
import subprocess
import time
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

TZ_ROOT = "/var/db/timezone/zoneinfo"
SYSTEMSETUP = "/usr/sbin/systemsetup"

TZ_RE = re.compile(r"^[A-Za-z0-9_+\-]+(?:/[A-Za-z0-9_+\-]+)*$")


@dataclass
class TZApplyResult:
    ok: bool
    message: str
    command: str = ""
    timezone: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "ok": self.ok,
            "message": self.message,
            "command": self.command,
            "timezone": self.timezone,
        }


def system_timezone() -> str:
    """读取当前系统时区。macOS 上 /etc/localtime 指向 zoneinfo 文件。"""
    env_tz = os.environ.get("TZ", "")
    if env_tz and _valid_tz(env_tz):
        return env_tz
    try:
        link = os.readlink("/etc/localtime")
    except OSError:
        link = ""
    if TZ_ROOT + "/" in link:
        return link.split(TZ_ROOT + "/", 1)[1]
    if "zoneinfo/" in link:
        return link.split("zoneinfo/", 1)[1]
    for candidate in ("/etc/timezone",):
        try:
            with open(candidate, "r", encoding="utf-8") as fh:
                name = fh.read().strip()
            if name:
                return name
        except OSError:
            pass
    return "Unknown"


def _valid_tz(name: str) -> bool:
    if not name or not TZ_RE.match(name):
        return False
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return False


def valid_timezone(name: str) -> bool:
    if not name or len(name) > 64 or not TZ_RE.match(name):
        return False
    try:
        return name in available_timezones()
    except Exception:
        try:
            ZoneInfo(name)
            return True
        except Exception:
            return False


def tz_offset_seconds(name: str, at: Optional[datetime] = None) -> Optional[int]:
    try:
        zone = ZoneInfo(name)
    except Exception:
        return None
    moment = at or datetime.now(timezone.utc)
    off = moment.astimezone(zone).utcoffset()
    return int(off.total_seconds()) if off is not None else None


def tz_clock(name: str) -> str:
    try:
        return datetime.now(ZoneInfo(name)).strftime("%H:%M")
    except Exception:
        return "--:--"


def tz_date(name: str) -> str:
    try:
        return datetime.now(ZoneInfo(name)).strftime("%m-%d")
    except Exception:
        return "--"


def offset_label(seconds: Optional[int]) -> Optional[str]:
    if seconds is None:
        return None
    sign = "+" if seconds >= 0 else "-"
    seconds = abs(seconds)
    hours, minutes = divmod(seconds // 60, 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def compare_timezones(
    system_tz: str, exit_tz: str
) -> Dict[str, object]:
    """比较系统时区与出口时区，给出偏移差与一致性判断。"""
    sys_off = tz_offset_seconds(system_tz)
    exit_off = tz_offset_seconds(exit_tz)
    if sys_off is None or exit_off is None:
        return {
            "comparable": False,
            "same_name": system_tz == exit_tz,
            "diff_hours": None,
            "system_offset": sys_off,
            "exit_offset": exit_off,
        }
    diff = exit_off - sys_off
    return {
        "comparable": True,
        "same_name": system_tz == exit_tz,
        "same_offset": diff == 0,
        "diff_seconds": diff,
        "diff_hours": round(diff / 3600.0, 2),
        "system_offset": sys_off,
        "exit_offset": exit_off,
        "system_offset_label": offset_label(sys_off),
        "exit_offset_label": offset_label(exit_off),
    }


def _run(cmd: list, timeout: float) -> Tuple[bool, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "操作超时（可能未完成授权）"
    except OSError as exc:
        return False, f"无法执行命令：{exc}"
    output = (proc.stdout or "").strip()
    error = (proc.stderr or "").strip()
    if proc.returncode == 0:
        return True, output or "已执行"
    if "User canceled" in error or "(-128)" in error:
        return False, "已取消管理员授权"
    return False, error or output or f"命令返回码 {proc.returncode}"

SUDOERS_FILE = "/etc/sudoers.d/clashlink"


TUN_HELPER = "/usr/local/bin/clashlink-tun"


def has_passwordless() -> bool:
    """是否已经装过免密规则；装过之后改时区不再弹管理员框。"""
    if os.geteuid() == 0:
        return True
    if platform.system() != "Darwin":
        return False
    proc = subprocess.run(
        ["/usr/bin/sudo", "-n", SYSTEMSETUP, "-gettimezone"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return proc.returncode == 0


def install_passwordless(timeout: float = 180.0) -> TZApplyResult:
    """一次性写入 sudoers 免密规则（只放开 systemsetup 的读写时区）。"""
    if platform.system() != "Darwin":
        return TZApplyResult(False, "只有 macOS 支持免密规则", timezone="")
    rule = "%admin ALL=(root) NOPASSWD: /usr/local/bin/clashlink-tun *"
    from . import core as core_mod

    helper_path = Path(TUN_HELPER)
    try:
        current = helper_path.read_text(encoding="utf-8")
    except OSError:
        current = ""
    try:
        core_mod.write_tun_env()
    except Exception:  # noqa: BLE001
        pass
    helper = _helper_script(core_mod)
    # sudoers 规则变化时也强制重装（helper 自身不变，靠规则指纹触发）
    helper += f"\n# rules-sha: {hashlib.sha256(rule.encode()).hexdigest()[:12]}\n"
    if helper.strip() == current.strip() and has_passwordless():
        return TZApplyResult(True, "免密接管已是最新，无需重装", timezone="")
    helper_b64 = base64.b64encode(helper.encode("utf-8")).decode("ascii")
    plist = _plist(core_mod)
    plist_b64 = base64.b64encode(plist.encode("utf-8")).decode("ascii")
    helper_path = Path(TUN_HELPER)
    plist_path = Path("/Library/LaunchDaemons/io.clashlink.core.plist")
    inner = (
        "mkdir -p /etc/sudoers.d /usr/local/bin && "
        f"printf '%s\\n' \"{rule}\" > {SUDOERS_FILE} && "
        f"chown root:wheel {SUDOERS_FILE} && chmod 440 {SUDOERS_FILE} && "
        f"printf %s '{helper_b64}' | base64 -D > {helper_path} && "
        f"chown root:wheel {helper_path} && chmod 755 {helper_path} && "
        f"printf %s '{plist_b64}' | base64 -D > {plist_path} && "
        f"chown root:wheel {plist_path} && chmod 644 {plist_path} && "
        f"( /bin/launchctl bootout system/io.claudenet.core 2>/dev/null || true ) && "
        f"( /bin/launchctl bootout system/io.clashlink.core 2>/dev/null || true ) && "
        f"/bin/launchctl bootstrap system {plist_path} && "
        f"visudo -cf {SUDOERS_FILE}"
    )
    escaped = inner.replace("\\", "\\\\").replace('"', '\\"')
    script = f'do shell script "{escaped}" with administrator privileges'
    ok, msg = _run(["osascript", "-e", script], timeout)
    if ok and has_passwordless() and helper_path.exists():
        return TZApplyResult(True, "已启用免密改时区与 TUN 起停", timezone="")
    if ok and has_passwordless():
        return TZApplyResult(False, "sudoers 已写入，但 TUN helper 没有落盘", timezone="")
    if ok:
        return TZApplyResult(False, "规则写入失败，请手动检查 /etc/sudoers.d/clashlink", timezone="")
    return TZApplyResult(False, msg, timezone="")


def apply_timezone(tz: str, timeout: float = 180.0) -> TZApplyResult:
    """把系统时区改成 tz。macOS 会弹出原生管理员授权框。"""
    if not valid_timezone(tz):
        return TZApplyResult(False, f"无效的时区名称：{tz}", timezone=tz)

    system = platform.system()
    if system == "Darwin":
        command = f"{SYSTEMSETUP} -settimezone {shlex.quote(tz)}"
        if os.geteuid() == 0:
            ok, msg = _run([SYSTEMSETUP, "-settimezone", tz], timeout)
        elif os.path.exists(TUN_HELPER):
            ok, msg = _run(["/usr/bin/sudo", "-n", TUN_HELPER, "tz", tz], timeout)
        elif has_passwordless():
            ok, msg = _run(["/usr/bin/sudo", "-n", SYSTEMSETUP, "-settimezone", tz], timeout)
        else:
            escaped = command.replace("\\", "\\\\").replace('"', '\\"')
            script = f'do shell script "{escaped}" with administrator privileges'
            ok, msg = _run(["osascript", "-e", script], timeout)
    elif system == "Linux":
        command = f"sudo timedatectl set-timezone {shlex.quote(tz)}"
        ok, msg = _run(["sudo", "-n", "timedatectl", "set-timezone", tz], timeout)
        if not ok and "password" in msg.lower():
            msg = "需要 root 权限，请手动执行：" + command
    elif system == "Windows":
        command = f"tzutil /s {shlex.quote(tz)}"
        ok, msg = _run(["tzutil", "/s", tz], timeout)
    else:
        command = ""
        ok, msg = False, f"暂不支持的系统：{system}"

    if ok:
        for _ in range(6):
            if system_timezone() == tz:
                return TZApplyResult(True, f"系统时区已切换为 {tz}", command, tz)
            time.sleep(0.4)
        return TZApplyResult(
            False,
            "命令已执行，但系统时区未生效（可能被“自动设置时区”覆盖）",
            command,
            tz,
        )
    return TZApplyResult(False, msg, command, tz)


def describe_drift(system_tz: str, exit_tz: str) -> str:
    cmp = compare_timezones(system_tz, exit_tz)
    if cmp.get("same_name"):
        return "系统时区与出口时区一致"
    if cmp.get("comparable") and cmp.get("same_offset"):
        return "名称不同但 UTC 偏移一致，通常无影响"
    hours = cmp.get("diff_hours")
    if hours is None:
        return f"系统 {system_tz} 与出口 {exit_tz} 不一致"
    direction = "快" if float(hours) < 0 else "慢"
    return f"系统时间比出口当地时间{direction} {abs(float(hours)):g} 小时"


def _helper_script(core_mod) -> str:
    env_file = str(core_mod.CORE_DIR / "tun.env")
    return (
        "#!/bin/sh\n"
        "#!/bin/sh\n"
        "# helper-version: 2\n"
        "set -eu\n"
        f"ENVF={shlex.quote(env_file)}\n"
        "PLIST=/Library/LaunchDaemons/io.clashlink.core.plist\n"
        "esc() { printf '%s' \"$1\" | /usr/bin/sed 's/&/\\&amp;/g; s/</\\&lt;/g; s/>/\\&gt;/g'; }\n"
        'case "${1:-}" in\n'
        "  on)\n"
        '    [ -r "$ENVF" ] || { echo "缺少 $ENVF，先在 App 里启用一次订阅" >&2; exit 1; }\n'
        '    . "$ENVF"\n'
        '    umask 022\n'
        '    cat > "$PLIST" <<PLISTEOF\n'
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        '  <key>Label</key><string>io.clashlink.core</string>\n'
        '  <key>ProgramArguments</key>\n  <array>\n'
        '    <string>$(esc "$MIHOMO")</string>\n'
        '    <string>-f</string>\n'
        '    <string>$(esc "$CONFIG")</string>\n'
        '    <string>-d</string>\n'
        '    <string>$(esc "$CORE_DIR")</string>\n'
        '  </array>\n'
        '  <key>RunAtLoad</key><false/>\n'
        '  <key>KeepAlive</key><false/>\n'
        '  <key>StandardOutPath</key><string>$(esc "$LOG")</string>\n'
        '  <key>StandardErrorPath</key><string>$(esc "$LOG")</string>\n'
        '</dict>\n</plist>\n'
        'PLISTEOF\n'
        '    /usr/sbin/chown root:wheel "$PLIST"\n'
        '    /bin/chmod 644 "$PLIST"\n'
        '    /bin/launchctl bootout system/io.clashlink.core 2>/dev/null || true\n'
        '    /bin/launchctl bootstrap system "$PLIST"\n'
        '    exec /bin/launchctl kickstart -k system/io.clashlink.core\n'
        '    ;;\n'
        '  off) exec /bin/launchctl kill SIGTERM system/io.clashlink.core ;;\n'
        '  tz) exec /usr/sbin/systemsetup -settimezone "$2" ;;\n'
        '  tzget) exec /usr/sbin/systemsetup -gettimezone ;;\n'
        '  proxy)\n'
        '    act="$2"; svc="$3"\n'
        '    if [ "$act" = on ]; then\n'
        '      host="$4"; port="$5"\n'
        '      /usr/sbin/networksetup -setwebproxy "$svc" "$host" "$port" || exit 1\n'
        '      /usr/sbin/networksetup -setsecurewebproxy "$svc" "$host" "$port" || exit 1\n'
        '      /usr/sbin/networksetup -setsocksfirewallproxy "$svc" "$host" "$port" || exit 1\n'
        '    else\n'
        '      /usr/sbin/networksetup -setwebproxystate "$svc" off || exit 1\n'
        '      /usr/sbin/networksetup -setsecurewebproxystate "$svc" off || exit 1\n'
        '      /usr/sbin/networksetup -setsocksfirewallproxystate "$svc" off || exit 1\n'
        '    fi ;;\n'
        '  dns)\n'
        '    mode="$2"; svc="$3"\n'
        '    if [ "$mode" = set ]; then\n'
        '      shift 3\n'
        '      /usr/sbin/networksetup -setdnsservers "$svc" "$@"\n'
        '    else\n'
        '      /usr/sbin/networksetup -setdnsservers "$svc" Empty\n'
        '    fi ;;\n'
        '  *) echo "usage: clashlink-tun on|off|tz <zone>|tzget|proxy on|off <service> [host port]|dns set|dhcp <service> [servers...]" >&2; exit 2 ;;\n'
        "esac\n"
    )


def _plist(core_mod) -> str:
    exe = str(core_mod.binary() or "")
    config = str(core_mod.CONFIG)
    core_dir = str(core_mod.CORE_DIR)
    log = str(pathlib.Path(core_dir) / "core.log")
    args = "".join(f"    <string>{arg}</string>\n" for arg in (exe, "-f", config, "-d", core_dir))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        "  <key>Label</key><string>io.clashlink.core</string>\n"
        "  <key>ProgramArguments</key>\n  <array>\n" + args + "  </array>\n"
        "  <key>RunAtLoad</key><false/>\n"
        "  <key>KeepAlive</key><false/>\n"
        f"  <key>StandardOutPath</key><string>{log}</string>\n"
        f"  <key>StandardErrorPath</key><string>{log}</string>\n"
        "</dict>\n</plist>\n"
    )
