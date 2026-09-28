#!/bin/bash
# ============================================
# AI Quant Agent - 本地模式一键停止脚本
# 停止本项目的 后端(uvicorn) + 前端(vite)
# 采集中的后端 SIGTERM 会被优雅停机无限等待, 因此带 SIGKILL 兜底
# 注意: 停止后端会中断采集, 但任务队列已持久化(断点续采),
#       下次 start_all.sh 启动后会自动续采。
# ============================================

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

BACKEND_PATTERN="uvicorn app.main:app --host 0.0.0.0 --port 8000"
FRONTEND_PATTERN="frontend/node_modules/.bin/vite"

echo "=========================================="
echo "  AI Quant Agent (本地模式) 停止中..."
echo "=========================================="

# 1. 停止后端 uvicorn (SIGTERM 优雅, 超时 SIGKILL)
echo "[...] 停止后端 uvicorn..."
if pkill -f "$BACKEND_PATTERN" 2>/dev/null; then
    echo "     已发送优雅停止信号, 等待退出..."
    sleep 3
    if pgrep -f "$BACKEND_PATTERN" > /dev/null 2>&1; then
        echo "[!] 后端仍在运行(可能正在采集), 强制终止..."
        pkill -9 -f "$BACKEND_PATTERN" 2>/dev/null || true
        sleep 1
    fi
    echo "     后端已停止"
else
    echo "     后端未在运行"
fi

# 2. 停止前端 vite
echo "[...] 停止前端 vite..."
if pkill -f "$FRONTEND_PATTERN" 2>/dev/null; then
    sleep 2
    if pgrep -f "$FRONTEND_PATTERN" > /dev/null 2>&1; then
        echo "[!] 前端仍在运行, 强制终止..."
        pkill -9 -f "$FRONTEND_PATTERN" 2>/dev/null || true
        sleep 1
    fi
    echo "     前端已停止"
else
    echo "     前端未在运行"
fi

# 3. 兜底: 按端口强杀(防止残留)
for port in 8000 5173; do
    PIDS=$(lsof -ti tcp:$port -sTCP:LISTEN 2>/dev/null || true)
    if [ -n "$PIDS" ]; then
        echo "[...] 端口 $port 仍有进程 (PID: $PIDS)，强杀..."
        kill -9 $PIDS 2>/dev/null || true
        sleep 1
    fi
done

# 4. 最终确认
if curl -s http://localhost:8000/api/v1/system/health > /dev/null 2>&1; then
    echo "[!] 后端仍在运行，请手动检查: ps aux | grep uvicorn"
else
    echo "[✓] 后端已停止"
fi
if lsof -ti tcp:5173 -sTCP:LISTEN > /dev/null 2>&1; then
    echo "[!] 前端端口仍被占用，请手动检查: lsof -iTCP:5173"
else
    echo "[✓] 前端已停止"
fi

echo ""
echo "[✓] 全部服务已停止。注意: MySQL 未停止(数据在宿主机, 不影响)"
echo "    下次启动自动续采: bash scripts/start_all.sh"
