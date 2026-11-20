#!/usr/bin/env python3
"""把 claudenet 打包成一个可双击的 macOS 应用（.app）。

打包结果完全自包含（除 Python 解释器外）：应用会把 claudenet 源码、网页资源
和启动脚本放进 ``Contents/Resources``，图标在构建时由 ``tools/icon.svg``
渲染成 ``.icns``。

用法::

    python3 tools/build_app.py            # 生成 dist/AIClash.app
    python3 tools/build_app.py --no-sign  # 跳过 ad-hoc 签名
"""
from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claudenet import __display_name__, __version__  # noqa: E402

APP_NAME = __display_name__
BUNDLE_ID = "io.clashlink.app"
EXECUTABLE = "ClashLink"
ICON_SVG = ROOT / "tools" / "icon.svg"
DIST = ROOT / "dist"

LAUNCHER = """#!/bin/bash
# 由 tools/build_app.py 生成：定位可用的 Python 3，再启动本地控制台。
set -u

RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
LOG_DIR="$HOME/Library/Logs"
LOG="$LOG_DIR/ClashLink.log"
mkdir -p "$LOG_DIR"
exec >>"$LOG" 2>&1
echo "--- $(date '+%Y-%m-%d %H:%M:%S') launch ---"

CANDIDATES=(
  "{python}"
  /opt/homebrew/bin/python3
  /usr/local/bin/python3
  "$(command -v python3 2>/dev/null || true)"
  /usr/bin/python3
)

for py in "${{CANDIDATES[@]}}"; do
  [ -n "$py" ] || continue
  [ -x "$py" ] || continue
  if "$py" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    exec "$py" -u -B "$RES/run.py" "$@"
  fi
done

osascript -e 'display alert "缺少 Python 3" message "Clash Link 需要 Python 3.9 或更高版本。请先安装 python.org 或 Homebrew 的 python3，然后重新打开本应用。" as critical'
exit 1
"""

RUN_PY = """#!/usr/bin/env python3
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from claudenet.app import main

if __name__ == "__main__":
    sys.exit(main())
"""

CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
]


def log(message: str) -> None:
    print(f"  {message}")


def render_png(svg: Path, target: Path, size: int = 1024) -> bool:
    """优先用本机 Chromium 渲染 SVG；失败时退回 Pillow 画一个近似图标。"""
    chrome = next((p for p in CHROME_PATHS if Path(p).exists()), None)
    if chrome:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                subprocess.run(
                    [
                        chrome,
                        "--headless=new",
                        "--disable-gpu",
                        "--hide-scrollbars",
                        "--no-first-run",
                        "--no-default-browser-check",
                        f"--user-data-dir={tmp}/profile",
                        f"--window-size={size},{size}",
                        "--default-background-color=00000000",
                        f"--screenshot={target}",
                        svg.as_uri(),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=90,
                )
            except (subprocess.SubprocessError, OSError):
                pass
            else:
                if target.exists() and target.stat().st_size > 2000:
                    return True
    return draw_fallback_icon(target, size)


def draw_fallback_icon(target: Path, size: int) -> bool:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return False
    scale = size / 1024.0
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 竖向渐变底
    top = (240, 160, 129)
    bottom = (179, 84, 58)
    for y in range(size):
        t = y / max(1, size - 1)
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,)
        draw.line([(0, y), (size, y)], fill=color)

    # 圆角遮罩
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [52 * scale, 52 * scale, 972 * scale, 972 * scale], radius=206 * scale, fill=255
    )
    img.putalpha(mask)

    shield = [
        (512, 244), (718, 312), (718, 476), (700, 560), (660, 646), (604, 716),
        (548, 764), (512, 776), (476, 764), (420, 716), (364, 646), (324, 560),
        (306, 476), (306, 312),
    ]
    points = [(x * scale, y * scale) for x, y in shield]
    draw.line(points + [points[0]], fill=(255, 255, 255, 255), width=int(54 * scale), joint="curve")
    draw.line(
        [(420 * scale, 520 * scale), (486 * scale, 586 * scale), (612 * scale, 448 * scale)],
        fill=(255, 255, 255, 255),
        width=int(54 * scale),
        joint="curve",
    )
    img.save(target)
    return True


def build_icns(work: Path, resources: Path) -> bool:
    png = work / "icon_1024.png"
    if not render_png(ICON_SVG, png):
        log("跳过图标（未找到 Chromium，也无法使用 Pillow）")
        return False
    iconset = work / "AppIcon.iconset"
    iconset.mkdir(parents=True, exist_ok=True)
    sizes = [
        ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
        ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
        ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
        ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
        ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024),
    ]
    for name, px in sizes:
        try:
            subprocess.run(
                ["sips", "-z", str(px), str(px), str(png), "--out", str(iconset / name)],
                check=True,
                capture_output=True,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            log(f"图标缩放失败：{exc}")
            return False
    try:
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(resources / "AppIcon.icns")],
            check=True,
            capture_output=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log(f"生成 icns 失败：{exc}")
        return False
    log("图标：AppIcon.icns")
    return True


def force_rmtree(path: Path) -> None:
    """清掉旧包；包内 Python 运行时是只读的，需要先恢复写权限。"""
    def onerror(func, target, exc_info):  # noqa: ANN001
        try:
            os.chmod(os.path.dirname(target), 0o755)
            os.chmod(target, 0o755)
        except OSError:
            pass
        try:
            func(target)
        except OSError:
            pass

    for item in sorted(path.rglob("*"), key=lambda x: len(x.parts), reverse=True):
        try:
            item.chmod(0o755 if item.is_dir() else 0o644)
        except OSError:
            pass
    shutil.rmtree(path, onerror=onerror)


def bundle_python(resources: Path) -> None:
    """复制独立 CPython 运行时，App 不再依赖系统 python3。"""
    runtime = ROOT / "tools" / "runtime" / "python"
    if not (runtime / "bin" / "python3").exists():
        log("警告：没有内置 Python 运行时，将退回系统 python3")
        return
    shutil.copytree(
        runtime,
        resources / "python",
        symlinks=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.a", "*.pyo"),
    )
    root = resources / "python"
    (root / "bin" / "python3").chmod(0o755)
    # 运行时可写，方便用户替换/删除；.pyc 由启动器的 -B 与 PYTHONDONTWRITEBYTECODE 抑制
    for path in root.rglob("*"):
        try:
            if path.is_dir() or os.access(path, os.X_OK):
                path.chmod(0o755)
            else:
                path.chmod(0o644)
        except OSError:
            pass
    size = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
    log(f"内置 Python：{size / 1024 / 1024:.0f} MB")


def copy_sources(resources: Path) -> None:
    package = ROOT / "claudenet"
    shutil.copytree(
        package,
        resources / "claudenet",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
    )
    (resources / "run.py").write_text(RUN_PY, encoding="utf-8")
    os.chmod(resources / "run.py", 0o755)


KERNEL_CANDIDATES = [
    "/Applications/Clash Verge.app/Contents/MacOS/verge-mihomo",
    "/Applications/Clash Verge.app/Contents/MacOS/mihomo",
    "/opt/homebrew/bin/mihomo",
    "/usr/local/bin/mihomo",
]


def bundle_kernel(resources: Path) -> None:
    """把 mihomo 内核复制进包内，双开时不再依赖 Clash Verge。"""
    source = os.environ.get("AICLASH_MIHOMO", "").strip()
    if source and os.path.isfile(source):
        found = Path(source)
    else:
        found = next((Path(p) for p in KERNEL_CANDIDATES if os.path.isfile(p)), None)
    if found is None:
        log("警告：没找到 mihomo 内核，订阅与 TUN 将退回系统里已安装的 mihomo")
        return
    target_dir = resources / "bin"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "mihomo"
    shutil.copy2(found, target)
    os.chmod(target, 0o755)
    version = ""
    try:
        version = subprocess.run([str(target), "-v"], capture_output=True, text=True, timeout=10).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        pass
    (target_dir / "NOTICE-mihomo.txt").write_text(
        "mihomo (Clash Meta) kernel bundled with Clash Link.\n"
        f"binary: {found}\n"
        f"version: {version or 'unknown'}\n"
        "license: GPL-3.0 (https://github.com/MetaCubeX/mihomo/blob/Meta/LICENSE)\n"
        "source: https://github.com/MetaCubeX/mihomo\n"
        "This is an unmodified upstream build.\n",
        encoding="utf-8",
    )
    log(f"内核：{found} → Resources/bin/mihomo（{target.stat().st_size / 1024 / 1024:.0f} MB）")


def compile_native(target: Path) -> None:
    """编译原生窗口外壳（WKWebView），不再调用系统浏览器。"""
    src = ROOT / "tools" / "native" / "AppDelegate.swift"
    if not src.exists():
        raise SystemExit(f"缺少原生外壳源码：{src}")
    cmd = [
        "swiftc", "-O",
        str(src),
        "-o", str(target),
        "-framework", "Cocoa",
        "-framework", "WebKit",
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=180)
    except (subprocess.SubprocessError, OSError) as exc:
        detail = getattr(exc, "stderr", b"")
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", "replace")
        raise SystemExit(f"编译原生外壳失败：{exc}\n{detail}")
    os.chmod(target, 0o755)
    log(f"原生外壳：{target.name}")


def write_plist(contents: Path) -> None:
    info = {
        "CFBundleDevelopmentRegion": "zh_CN",
        "CFBundleDisplayName": APP_NAME,
        "CFBundleExecutable": EXECUTABLE,
        "CFBundleIconFile": "AppIcon",
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": "Clash Link",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "LSApplicationCategoryType": "public.app-category.utilities",
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        "NSHumanReadableCopyright": "Clash Link · 海外 AI 网络体检",
    }
    with (contents / "Info.plist").open("wb") as fh:
        plistlib.dump(info, fh, sort_keys=False)


def ad_hoc_sign(app: Path) -> None:
    try:
        subprocess.run(
            ["codesign", "--force", "--deep", "--sign", "-", str(app)],
            check=True,
            capture_output=True,
            timeout=120,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log(f"ad-hoc 签名跳过：{exc}")
    else:
        log("ad-hoc 签名完成")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打包 macOS 应用")
    parser.add_argument("--no-sign", action="store_true", help="跳过 ad-hoc 签名")
    parser.add_argument("--out", default=str(DIST), help="输出目录")
    args = parser.parse_args(argv)

    out_dir = Path(args.out).expanduser().resolve()
    app = out_dir / f"{APP_NAME}.app"
    print(f"构建 {app}")

    if app.exists():
        force_rmtree(app)
    contents = app / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)

    copy_sources(resources)
    log("源码与网页资源已复制")
    bundle_kernel(resources)
    bundle_python(resources)

    compile_native(macos / EXECUTABLE)

    write_plist(contents)
    log("Info.plist 已写入")

    with tempfile.TemporaryDirectory() as tmp:
        build_icns(Path(tmp), resources)

    if not args.no_sign:
        ad_hoc_sign(app)

    size = sum(p.stat().st_size for p in app.rglob("*") if p.is_file())
    print(f"完成：{app}（{size / 1024 / 1024:.1f} MB）")
    print("双击打开应用窗口（不再启动浏览器）；首次启动 macOS 可能提示「未验证的开发者」，右键 → 打开 即可。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
