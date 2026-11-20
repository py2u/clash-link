"""命令行入口：python3 -m claudenet check / fix / status / serve。"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List

from . import __display_name__, __version__
from .checks import ScanJob, quick_timezone_probe, run_scan
from .server import ping_existing, run as run_server
from .timezone import apply_timezone, compare_timezones, system_timezone, tz_clock

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
CYAN = "\033[36m"

STATUS_STYLE = {
    "pass": (GREEN, "✓"),
    "warn": (YELLOW, "!"),
    "fail": (RED, "✕"),
    "info": (BLUE, "i"),
    "pending": (DIM, "·"),
}


def _color_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def paint(text: str, color: str, enabled: bool) -> str:
    return f"{color}{text}{RESET}" if enabled else text


def rule(width: int = 64, enabled: bool = False) -> str:
    return paint("─" * width, DIM, enabled)


def print_report(report: Dict[str, Any]) -> None:
    color = _color_enabled()
    verdict = report.get("verdict", "warn")
    vcolor = {"pass": GREEN, "warn": YELLOW, "fail": RED}.get(verdict, DIM)
    print()
    print(paint(f"  {__display_name__}  v{__version__}", BOLD, color))
    print(rule(enabled=color))
    print(
        "  结论  "
        + paint(f"{report.get('score', 0)}/100  {report.get('headline', '')}", vcolor + BOLD, color)
    )
    geo = report.get("exit") or {}
    if geo.get("ip"):
        print(f"  出口  {geo.get('flag', '')} {geo.get('country', '')} · {geo.get('city', '')} · {geo.get('ip', '')}")
        if geo.get("asn") or geo.get("org"):
            print(f"  ASN   {geo.get('asn', '')} {geo.get('org', '')}".rstrip())
    proxy = report.get("proxy") or {}
    if proxy.get("label") or proxy.get("tun"):
        print(f"  代理  {'TUN + ' if proxy.get('tun') else ''}{proxy.get('label', '')}")
    tz = report.get("timezone") or {}
    if tz.get("exit"):
        same = (tz.get("comparison") or {}).get("same_name")
        mark = paint("已对齐", GREEN, color) if same else paint("不一致", YELLOW, color)
        print(
            f"  时区  {tz.get('system')} {tz.get('system_clock', '')} → "
            f"{tz.get('exit')} {tz.get('exit_clock', '')}  [{mark}]"
        )
    print(rule(enabled=color))

    current_group = None
    for check in report.get("checks", []):
        if check.get("group") != current_group:
            current_group = check.get("group")
            print()
            print(paint(f"  {current_group}", BOLD, color))
        style, glyph = STATUS_STYLE.get(check.get("status", "pending"), (DIM, "·"))
        line = f"  {paint(glyph, style, color)} {check.get('title', ''):<10} {check.get('summary', '')}"
        print(line)
        if check.get("detail"):
            for chunk in _wrap(check["detail"], 74):
                print(paint(f"      {chunk}", DIM, color))
    print()
    print(paint(f"  完成于 {report.get('generated', '')} · 用时 {report.get('duration_ms', 0)} ms", DIM, color))
    print()


def _wrap(text: str, width: int) -> List[str]:
    lines: List[str] = []
    line = ""
    for ch in text:
        line += ch
        if len(line) >= width:
            lines.append(line)
            line = ""
    if line:
        lines.append(line)
    return lines


def cmd_check(args: argparse.Namespace) -> int:
    job = ScanJob()
    started = time.time()
    if not args.json:
        print(paint("正在检测本机网络…", DIM, _color_enabled()), end="", flush=True)
    report = run_scan(job, {})
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("\r" + " " * 30 + "\r", end="")
        print_report(report)
        if report.get("verdict") != "pass" and report.get("timezone", {}).get("exit"):
            tz = report["timezone"]
            if not (tz.get("comparison") or {}).get("same_name"):
                print(
                    paint(
                        f"  修复时区：python3 -m claudenet fix    （系统 {tz.get('system')} → 出口 {tz.get('exit')}）",
                        YELLOW,
                        _color_enabled(),
                    )
                )
                print()
    return 0 if report.get("verdict") != "fail" else 2


def cmd_status(args: argparse.Namespace) -> int:
    color = _color_enabled()
    probe = quick_timezone_probe({})
    geo = probe.get("exit") or {}
    cmp = probe.get("comparison") or {}
    print()
    print(paint(f"  {__display_name__} · 快速状态", BOLD, color))
    print(rule(enabled=color))
    print(f"  出口  {geo.get('flag', '')} {geo.get('country', '')} · {geo.get('city', '')} · {geo.get('ip', '')}")
    print(f"  时区  系统 {probe.get('system_tz')}（{tz_clock(probe.get('system_tz', ''))}）")
    print(f"        出口 {probe.get('exit_tz')}（{tz_clock(probe.get('exit_tz', ''))}）")
    if cmp.get("same_name"):
        print("  结论  时区一致")
    elif cmp.get("same_offset"):
        print("  结论  偏移一致（无害）")
    else:
        print(paint(f"  结论  相差 {cmp.get('diff_hours')} 小时，建议 python3 -m claudenet fix", YELLOW, color))
    print()
    return 0


def cmd_fix(args: argparse.Namespace) -> int:
    color = _color_enabled()
    probe = quick_timezone_probe({})
    exit_tz = probe.get("exit_tz") or ""
    system_tz = probe.get("system_tz") or ""
    if not exit_tz:
        print(paint("无法确定出口时区，请确认代理已开启。", RED, color))
        return 1
    if exit_tz == system_tz:
        print(paint(f"系统时区已经是 {exit_tz}，无需修改。", GREEN, color))
        return 0
    print(f"系统时区：{system_tz}（{tz_clock(system_tz)}）")
    print(f"出口时区：{exit_tz}（{tz_clock(exit_tz)}）")
    print(f"将执行：sudo systemsetup -settimezone {exit_tz}")
    if not args.yes:
        try:
            answer = input("确认修改系统时区？需要管理员密码 [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("已取消。")
            return 1
    result = apply_timezone(exit_tz)
    print(("✓ " if result.ok else "✕ ") + result.message)
    return 0 if result.ok else 1


def cmd_serve(args: argparse.Namespace) -> int:
    if args.port and ping_existing(args.port):
        print(f"端口 {args.port} 上已有控制台在运行。")
        return 0
    return run_server(port=args.port, open_browser=not args.no_browser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m claudenet", description=f"{__display_name__} CLI")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_check = sub.add_parser("check", help="完整体检")
    p_check.add_argument("--json", action="store_true", help="输出 JSON")
    p_check.set_defaults(func=cmd_check)

    p_status = sub.add_parser("status", help="快速查看出口与时区")
    p_status.set_defaults(func=cmd_status)

    p_fix = sub.add_parser("fix", help="把系统时区同步到出口时区")
    p_fix.add_argument("-y", "--yes", action="store_true", help="跳过确认")
    p_fix.set_defaults(func=cmd_fix)

    p_serve = sub.add_parser("serve", help="启动网页控制台")
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--no-browser", action="store_true")
    p_serve.set_defaults(func=cmd_serve)
    return parser


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
