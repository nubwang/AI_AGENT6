#!/bin/bash
# ============================================
# AI Quant Agent - 本地模式一键启动脚本
# 后端(venv+uvicorn) + 前端(npm dev) 本地直跑
# 依赖宿主机 MySQL (127.0.0.1:3306, quant_user)
#
# 用法（前后端统一一条命令）:
#   bash scripts/start_all.sh            # 幂等: 已在运行的服务自动跳过
#   bash scripts/start_all.sh --restart  # 先按端口结束旧的 后端/前端(含 npm 外壳) 再全新启动 (-r)
#   bash scripts/start_all.sh --status   # 只查看 后端/前端 状态与 PID (-s)
#   bash scripts/stop_all.sh             # 一键停止后端+前端
# ============================================

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

RESTART=0
STATUS=0
case "${1:-}" in
    --restart|-r) RESTART=1 ;;
    --status|-s)  STATUS=1 ;;
    --help|-h)
        echo "用法: bash scripts/start_all.sh [--restart|-r] [--status|-s]"
        echo "  (无参数)   幂等启动：已在运行的服务自动跳过"
        echo "  --restart  先结束旧的 后端/前端（含 npm 外壳与端口占用）再全新启动"
        echo "  --status   只查看 后端/前端 运行状态与 PID"
        exit 0
        ;;
esac

# 本项目进程匹配模式(避免误杀其他项目)
BACKEND_PATTERN="uvicorn app.main:app --host 0.0.0.0 --port 8000"
FRONTEND_PATTERN="frontend/node_modules/.bin/vite"
BACKEND_HEALTH="http://localhost:8000/api/v1/system/health"
BACKEND_PORT=8000
FRONTEND_PORT=5173

# 按端口强杀: 进程匹配可能被改造(如 vite 被 npm 派生/变成孤儿), 端口不会骗人。
# 同时清理 npm 外壳 —— `npm run dev` 被杀后 vite 会变孤儿进程占着端口, 导致下次启动失败。
kill_port() {
    local port="$1" pids pid ppid cmd
    pids=$(lsof -ti tcp:"$port" -sTCP:LISTEN 2>/dev/null || true)
    [ -z "$pids" ] && return 0
    for pid in $pids; do
        ppid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ' || true)
        echo "     端口 $port 被 PID $pid 占用 → 结束该进程"
        kill "$pid" 2>/dev/null || true
        if [ -n "$ppid" ] && [ "$ppid" != "1" ]; then
            cmd=$(ps -o command= -p "$ppid" 2>/dev/null || true)
            case "$cmd" in
                *"npm run dev"*)
                    echo "     同时结束其 npm 外壳 PID $ppid"
                    kill "$ppid" 2>/dev/null || true
                    ;;
            esac
        fi
    done
    sleep 2
    pids=$(lsof -ti tcp:"$port" -sTCP:LISTEN 2>/dev/null || true)
    if [ -n "$pids" ]; then
        echo "     优雅停止超时 → 强制终止 PID $pids"
        kill -9 $pids 2>/dev/null || true
        sleep 1
    fi
}

# 终止本项目后端进程: SIGTERM 优雅停止, 超时则 SIGKILL
# (采集中的后端 SIGTERM 会被 uvicorn 优雅停机无限等待, 必须强制终止)
kill_backend() {
    pkill -f "$BACKEND_PATTERN" 2>/dev/null || true
    sleep 3
    if pgrep -f "$BACKEND_PATTERN" > /dev/null 2>&1; then
        echo "     优雅停止超时, 强制终止..."
        pkill -9 -f "$BACKEND_PATTERN" 2>/dev/null || true
        sleep 1
    fi
    kill_port "$BACKEND_PORT"
}

# 终止本项目前端进程
kill_frontend() {
    pkill -f "$FRONTEND_PATTERN" 2>/dev/null || true
    sleep 2
    if pgrep -f "$FRONTEND_PATTERN" > /dev/null 2>&1; then
        echo "     优雅停止超时, 强制终止..."
        pkill -9 -f "$FRONTEND_PATTERN" 2>/dev/null || true
        sleep 1
    fi
    kill_port "$FRONTEND_PORT"
}

# 状态速查: bash scripts/start_all.sh --status
show_status() {
    echo "=========================================="
    echo "  AI Quant Agent 运行状态"
    echo "=========================================="
    local be fe
    be=$(pgrep -f "$BACKEND_PATTERN" 2>/dev/null | tr '\n' ' ' || true)
    if curl -s -m 5 "$BACKEND_HEALTH" > /dev/null 2>&1; then
        echo "[✓] 后端: 运行中  PID=${be:-?}  http://localhost:$BACKEND_PORT/docs"
    else
        echo "[✗] 后端: 未运行  (PID=${be:-无})"
    fi
    fe=$(lsof -ti tcp:"$FRONTEND_PORT" -sTCP:LISTEN 2>/dev/null | tr '\n' ' ' || true)
    if [ -n "$fe" ]; then
        echo "[✓] 前端: 运行中  PID=$fe  http://localhost:$FRONTEND_PORT"
    else
        echo "[✗] 前端: 未运行"
    fi
    echo "重启(先杀端口占用再启动): bash scripts/start_all.sh --restart"
    echo "停止: bash scripts/stop_all.sh"
}

if [ "$STATUS" = "1" ]; then
    show_status
    exit 0
fi

echo "=========================================="
echo "  AI Quant Agent (本地模式) 启动中..."
echo "=========================================="

# 1. 检查 .env
if [ ! -f .env ]; then
    echo "[!] .env 文件不存在，请先创建"
    exit 1
fi

# 2. 检查宿主机 MySQL 是否运行
echo "[...] 检查宿主机 MySQL..."
if ! mysqladmin ping -h127.0.0.1 -P3306 -uquant_user -pquant_pass_2024 --silent 2>/dev/null; then
    echo "[!] MySQL 未运行，正在启动 (brew services)..."
    brew services start mysql 2>/dev/null || {
        echo "[✗] MySQL 启动失败，请检查 Homebrew MySQL"
        exit 1
    }
    sleep 5
    # 启动后再确认一次
    if ! mysqladmin ping -h127.0.0.1 -P3306 -uquant_user -pquant_pass_2024 --silent 2>/dev/null; then
        echo "[✗] MySQL 仍未就绪，请手动检查: brew services list"
        exit 1
    fi
fi
echo "[✓] MySQL 已就绪 (127.0.0.1:3306)"

# 3. 检查后端虚拟环境
if [ ! -d backend/venv ]; then
    echo "[!] 后端虚拟环境不存在，正在创建并安装依赖..."
    cd backend
    python3 -m venv venv
    ./venv/bin/pip install --no-cache-dir -r requirements.txt
    cd ..
fi
echo "[✓] 后端环境就绪"

# 4. 检查前端依赖
if [ ! -d frontend/node_modules ]; then
    echo "[!] 前端依赖未安装，正在安装..."
    cd frontend
    npm install
    cd ..
fi
echo "[✓] 前端环境就绪"

# 5. 进程/端口冲突处理
if [ "$RESTART" = "1" ]; then
    echo "[...] --restart: 停止旧的 后端/前端 进程..."
    kill_backend
    kill_frontend
fi

# 清理不占用端口但已残留的僵尸后端进程(端口被占时旧进程会变僵尸)
if ! curl -s -m 5 "$BACKEND_HEALTH" > /dev/null 2>&1; then
    if pgrep -f "$BACKEND_PATTERN" > /dev/null 2>&1; then
        echo "[!] 检测到后端残留进程，清理中..."
        kill_backend
    fi
fi

# 6. 启动后端 (uvicorn) —— 已健康则跳过(幂等)
echo "[1/2] 启动后端 API (localhost:8000)..."
if curl -s -m 5 "$BACKEND_HEALTH" > /dev/null 2>&1; then
    echo "     [✓] 后端已在运行，跳过 (如需重启: bash scripts/start_all.sh --restart)"
    BACKEND_STARTED=0
else
    cd backend
    nohup ./venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 > ../logs/backend.log 2>&1 &
    BACKEND_PID=$!
    cd ..
    echo "     后端 PID: $BACKEND_PID"
    BACKEND_STARTED=1
fi

# 7. 启动前端 (vite dev) —— 端口已占用则跳过(幂等)
echo "[2/2] 启动前端开发服务器 (localhost:$FRONTEND_PORT)..."
if lsof -ti tcp:$FRONTEND_PORT -sTCP:LISTEN > /dev/null 2>&1; then
    echo "     [✓] 前端已在运行，跳过 (如需重启: bash scripts/start_all.sh --restart)"
    FRONTEND_STARTED=0
else
    cd frontend
    nohup npm run dev > ../logs/frontend.log 2>&1 &
    FRONTEND_PID=$!
    cd ..
    echo "     前端 PID: $FRONTEND_PID"
    FRONTEND_STARTED=1
fi

# 8. 等待后端就绪
if [ "$BACKEND_STARTED" = "1" ]; then
    echo "[...] 等待后端就绪..."
    # 后端启动含预热(政策库/知识库/调度器)，实测可达 40s+，故等待上限 60s
    for i in $(seq 1 60); do
        if curl -s -m 5 "$BACKEND_HEALTH" > /dev/null 2>&1; then
            break
        fi
        sleep 1
    done
    if ! curl -s -m 5 "$BACKEND_HEALTH" > /dev/null 2>&1; then
        echo "[✗] 后端启动超时(60s)，日志尾部:"
        tail -n 15 logs/backend.log 2>/dev/null || true
        echo "    完整日志: logs/backend.log"
        exit 1
    fi
    echo "[✓] 后端已就绪"
fi

# 9. 等待前端就绪（避免"脚本说好了、浏览器打不开"）
if [ "$FRONTEND_STARTED" = "1" ]; then
    echo "[...] 等待前端就绪..."
    for i in $(seq 1 30); do
        if curl -s -m 3 -o /dev/null "http://localhost:$FRONTEND_PORT/" 2>/dev/null; then
            break
        fi
        sleep 1
    done
    if ! curl -s -m 3 -o /dev/null "http://localhost:$FRONTEND_PORT/" 2>/dev/null; then
        echo "[✗] 前端启动超时，请查看 logs/frontend.log"
        exit 1
    fi
fi

BE_PID="$(pgrep -f "$BACKEND_PATTERN" 2>/dev/null | tr '\n' ' ' || true)"
FE_PID="$(lsof -ti tcp:$FRONTEND_PORT -sTCP:LISTEN 2>/dev/null | tr '\n' ' ' || true)"

echo ""
echo "=========================================="
echo "  ✅ AI Quant Agent (本地模式) 就绪！"
echo ""
echo "  🌐 前端(开发热更新): http://localhost:$FRONTEND_PORT"
echo "  📚 API 文档:         http://localhost:8000/docs"
echo "  💊 健康检查:         http://localhost:8000/api/v1/system/health"
echo "  📄 日志:             logs/backend.log, logs/frontend.log"
echo ""
echo "  进程:              后端 PID ${BE_PID:-无} / 前端 PID ${FE_PID:-无}"
echo "  查看状态: bash scripts/start_all.sh --status"
echo "  停止服务: bash scripts/stop_all.sh"
echo "  重启服务: bash scripts/start_all.sh --restart"
echo "=========================================="
