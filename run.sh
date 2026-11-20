#!/bin/bash
# 开发用启动脚本：直接跑源码，不打包 App。
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m claudenet.app "$@"
