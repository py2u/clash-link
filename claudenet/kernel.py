"""从 GitHub 更新 mihomo 内核。

内核下载到用户数据目录（~/Library/Application Support/ClashLink/bin/mihomo），
而不是覆盖 App 包里的二进制——避免破坏 ad-hoc 签名。
core.binary() 会优先使用这里的版本。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import platform
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional

from . import store
from .net import http_request

GITHUB_REPO = "MetaCubeX/mihomo"
API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
BIN_DIR = store.data_dir() / "bin"
TARGET = BIN_DIR / "mihomo"
META = BIN_DIR / "kernel.json"


def _arch() -> str:
    return "arm64" if platform.machine() in ("arm64", "aarch64") else "amd64"


def installed_version() -> str:
    """优先读已下载内核的版本；没有就回落到当前可用内核。"""
    from . import core

    binary = str(TARGET) if TARGET.exists() else (core.binary() or "")
    if not binary:
        return ""
    try:
        out = subprocess.run([binary, "-v"], capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return ""
    m = re.search(r"v(\d+\.\d+\.\d+)", out)
    return f"v{m.group(1)}" if m else ""


def latest_release(proxy=None, timeout: float = 25.0) -> Dict[str, Any]:
    res = http_request(API_LATEST, proxy=proxy, timeout=timeout, headers={"Accept": "application/vnd.github+json"})
    if res.error or not res.json():
        return {"ok": False, "error": res.error or f"HTTP {res.status}"}
    data = res.json()
    tag = str(data.get("tag_name") or "")
    asset = ""
    size = 0
    digest = ""
    arch = _arch()
    for item in data.get("assets") or []:
        name = str(item.get("name") or "")
        if not name.startswith(f"mihomo-darwin-{arch}-"):
            continue
        if "-go" in name or not name.endswith(".gz"):
            continue
        asset = name
        size = int(item.get("size") or 0)
        digest = str(item.get("digest") or "")
        break
    if not tag or not asset:
        return {"ok": False, "error": "没有找到适合本机的内核资源"}
    return {
        "ok": True,
        "tag": tag,
        "asset": asset,
        "size": size,
        "digest": digest,
        "url": f"https://github.com/{GITHUB_REPO}/releases/download/{tag}/{asset}",
    }


def status(proxy=None) -> Dict[str, Any]:
    current = installed_version()
    latest = latest_release(proxy)
    data = {
        "current": current,
        "latest": latest.get("tag") if latest.get("ok") else "",
        "update_available": bool(latest.get("ok") and latest.get("tag") and latest["tag"] != current),
        "source": "downloaded" if TARGET.exists() else "bundled",
        "error": latest.get("error", ""),
    }
    if latest.get("ok"):
        data["size"] = latest.get("size")
        data["asset"] = latest.get("asset")
    return data


def update(proxy=None, timeout: float = 240.0) -> Dict[str, Any]:
    latest = latest_release(proxy)
    if not latest.get("ok"):
        return {"ok": False, "message": latest.get("error") or "检查更新失败"}
    tag = latest["tag"]
    url = latest["url"]
    res = None
    for _ in range(6):
        res = http_request(url, proxy=proxy, timeout=timeout, max_body=80 * 1024 * 1024)
        if res.status in (301, 302, 303, 307, 308):
            location = res.header("location") or ""
            if not location:
                break
            if location.startswith("/"):
                location = "https://github.com" + location
            url = location
            continue
        break
    if res is None or res.error or res.status != 200 or not res.body:
        return {"ok": False, "message": (res.error if res and res.error else f"下载失败（HTTP {res.status if res else '—'}）")}
    raw = getattr(res, "raw", b"") or (res.body if isinstance(res.body, bytes) else str(res.body).encode())
    digest = str((latest.get("digest") or "")).strip().lower()
    if digest.startswith("sha256:"):
        expected = digest.split(":", 1)[1]
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expected:
            return {"ok": False, "message": "内核校验失败：sha256 与 GitHub 公布值不一致，已取消安装"}
    try:
        if latest["asset"].endswith(".gz"):
            raw = gzip.decompress(raw)
    except OSError as exc:
        return {"ok": False, "message": f"解压失败：{exc}"}
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    tmp = TARGET.with_suffix(".tmp")
    tmp.write_bytes(raw)
    os.chmod(tmp, 0o755)
    os.replace(tmp, TARGET)
    META.write_text(
        json.dumps({"version": tag, "asset": latest["asset"], "installed_at": time.time()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"ok": True, "version": tag, "path": str(TARGET)}
