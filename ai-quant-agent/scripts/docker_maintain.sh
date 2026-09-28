#!/bin/bash
# ============================================================
# Docker 定期维护脚本
# 用途：清理 Docker 未使用的资源，防止磁盘膨胀
# 建议：每周运行一次
# 使用：chmod +x scripts/docker_maintain.sh && ./scripts/docker_maintain.sh
# ============================================================

echo "=========================================="
echo " Docker 维护脚本"
echo " 时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="

# 检查 Docker 是否在运行
if ! docker info >/dev/null 2>&1; then
    echo "[ERROR] Docker 守护进程未运行"
    exit 1
fi

echo ""
echo "[1/5] 清理未使用的容器..."
docker container prune -f 2>&1 | grep -v "Total reclaimed"

echo ""
echo "[2/5] 清理未使用的镜像..."
docker image prune -af 2>&1 | grep -v "Total reclaimed"

echo ""
echo "[3/5] 清理未使用的网络..."
docker network prune -f 2>&1 | grep -v "Total reclaimed"

echo ""
echo "[4/5] 清理构建缓存..."
docker builder prune -af 2>&1 | grep -v "Total reclaimed"

echo ""
echo "[5/5] 清理未使用的数据卷..."
docker volume prune -f 2>&1 | grep -v "Total reclaimed"

echo ""
echo "=========================================="
echo " 磁盘使用情况"
echo "=========================================="
df -h /System/Volumes/Data 2>/dev/null | tail -1 || df -h / 2>/dev/null | tail -1

echo ""
echo " Docker 磁盘使用:"
if [ -f ~/Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw ]; then
    ls -lh ~/Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw
fi

echo ""
echo "=========================================="
echo " ✅ 维护完成"
echo "=========================================="
