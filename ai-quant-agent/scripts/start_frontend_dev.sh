#!/bin/bash
# ============================================
# 前端 dev server 独立启动（已并入统一入口，本脚本保留为薄包装）
#
# 前后端统一用一条命令：  bash scripts/start_all.sh --restart
#   （先按端口结束旧的 后端/前端，含残留的 npm 外壳与孤儿 vite，再全新启动并做就绪校验）
#
# 本脚本仅作为"只想动前端"时的快捷入口，实际逻辑全部由 start_all.sh 承担：
#   bash scripts/start_frontend_dev.sh            # 等价于 start_all.sh --restart
#   bash scripts/start_frontend_dev.sh --check    # 等价于 start_all.sh --status
#   bash scripts/start_frontend_dev.sh --stop     # 等价于 stop_all.sh
# ============================================
set -u

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

case "${1:-}" in
    --check|check)
        exec bash scripts/start_all.sh --status
        ;;
    --stop|stop)
        exec bash scripts/stop_all.sh
        ;;
    *)
        echo "[frontend_dev] 转交统一入口: bash scripts/start_all.sh --restart"
        echo "[frontend_dev] 提示: 该命令会同时重启后端与前端（这是推荐做法，避免只重启一半导致接口/页面版本不一致）"
        exec bash scripts/start_all.sh --restart
        ;;
esac
