import os

"""Clash Link —— 海外 AI 服务网络体检、节点切换与出口时区同步。

纯标准库实现，支持 macOS / Linux，附带本地网页控制台。
"""

__version__ = "0.0.16"
__app_name__ = "clashlink"
__display_name__ = "Clash Link"


def env(name: str, default: str = "") -> str:
    """读取 AIREADY_* 环境变量，兼容旧的 CLAUDENET_*。"""
    return (
        os.environ.get(f"CLASHLINK_{name}", "")
        or os.environ.get(f"AIREADY_{name}", "")
        or os.environ.get(f"CLAUDENET_{name}", "")
        or default
    )
