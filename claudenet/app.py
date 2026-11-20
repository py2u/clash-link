"""桌面启动器：拉起本地控制台并打开浏览器。"""
from __future__ import annotations

import argparse
import os
import sys

from .server import ping_existing, run

DEFAULT_PORT = 8788


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claudenet-gui", description="启动 Clash Link 控制台")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认 8788）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args(argv)

    if args.port is None:
        existing = ping_existing(DEFAULT_PORT)
        if existing:
            if not args.no_browser:
                import webbrowser

                webbrowser.open(f"http://127.0.0.1:{DEFAULT_PORT}/")
                print("检测到已在运行的控制台，已为你打开页面。")
            else:
                print(f"检测到已在运行的控制台：http://127.0.0.1:{DEFAULT_PORT}/")
            return 0

    return run(port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    sys.exit(main())
