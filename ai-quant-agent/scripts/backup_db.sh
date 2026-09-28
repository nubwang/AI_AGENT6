#!/bin/bash
# ============================================================
# MySQL 数据备份脚本
# 用途：备份 quant_db 数据库（当前数据源: 宿主机 MySQL 127.0.0.1:3306）
# 说明：数据在宿主机 MySQL（.env 配置 DB_HOST=127.0.0.1），优先备份宿主机；
#       宿主机不可用时回退到 Docker MySQL（兼容历史 Docker 部署）。
# 建议：每次采集完成后 / 每日运行
# 使用：./scripts/backup_db.sh
#
# 恢复（宿主机 MySQL，仅灾难恢复时手动执行，会覆盖同名表）：
#   mysql -h127.0.0.1 -P3306 -uquant_user -pquant_pass_2024 < backups/quant_db_xxx.sql
# 恢复（Docker MySQL）：
#   docker compose exec mysql mysql -uquant_user -pquant_pass_2024 quant_db < backups/quant_db_xxx.sql
# ============================================================

# 数据库连接参数（与 .env 保持一致）
DB_HOST="127.0.0.1"
DB_PORT="3306"
DB_USER="quant_user"
DB_PASS="quant_pass_2024"
DB_NAME="quant_db"

# 保留备份天数，可按需调大（如 365 = 保留一年）
KEEP_DAYS=30

BACKUP_DIR="$(cd "$(dirname "$0")/.." && pwd)/backups"
TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
BACKUP_FILE="${BACKUP_DIR}/quant_db_${TIMESTAMP}.sql"

mkdir -p "$BACKUP_DIR"

echo "=========================================="
echo " MySQL 数据备份"
echo " 时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="

BACKUP_OK=1

# 优先备份宿主机 MySQL（当前实际数据源）
# 注意: quant_user 无 RELOAD/PROCESS 权限, 无法使用 --single-transaction(会触发
#       FLUSH TABLES / tablespace 权限错误), 故用 --skip-lock-tables --no-tablespaces。
#       为获得一致性备份, 建议备份前先暂停采集(如点击"⏹ 停止全量")。
if mysqladmin ping -h"$DB_HOST" -P"$DB_PORT" -u"$DB_USER" -p"$DB_PASS" --silent 2>/dev/null; then
    echo "[1/2] 检测到宿主机 MySQL (${DB_HOST}:${DB_PORT}), 开始备份..."
    mysqldump -h"$DB_HOST" -P"$DB_PORT" -u"$DB_USER" -p"$DB_PASS" \
        --skip-lock-tables --no-tablespaces \
        --databases "$DB_NAME" > "$BACKUP_FILE" 2>/dev/null
    BACKUP_OK=$?
elif docker info >/dev/null 2>&1 && docker compose -f "$(dirname "$0")/../docker-compose.yml" ps -q mysql >/dev/null 2>&1; then
    echo "[1/2] 宿主机 MySQL 不可用, 回退备份 Docker MySQL..."
    docker compose -f "$(dirname "$0")/../docker-compose.yml" exec -T mysql \
        mysqldump -u"$DB_USER" -p"$DB_PASS" --databases "$DB_NAME" > "$BACKUP_FILE" 2>/dev/null
    BACKUP_OK=$?
else
    echo "[ERROR] 宿主机 MySQL 与 Docker MySQL 均不可用, 无法备份"
    exit 1
fi

if [ $BACKUP_OK -eq 0 ] && [ -s "$BACKUP_FILE" ]; then
    SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
    echo "[2/2] ✅ 备份完成: ${BACKUP_FILE} (${SIZE})"
else
    echo "[ERROR] 备份失败"
    rm -f "$BACKUP_FILE"
    exit 1
fi

# 清理 KEEP_DAYS 天前的旧备份
echo ""
echo "清理 ${KEEP_DAYS} 天前的旧备份..."
find "$BACKUP_DIR" -name "quant_db_*.sql" -mtime +"$KEEP_DAYS" -delete 2>/dev/null

echo ""
echo "当前备份列表:"
ls -lh "$BACKUP_DIR" 2>/dev/null | tail -10

echo ""
echo "=========================================="
echo " ✅ 备份完成"
echo "=========================================="
