#!/bin/bash
# 资产管理系统 - 数据库自动备份脚本
# 功能：
#   1. 每日备份（凌晨02:00执行），保留30天
#   2. 每月大版本备份（每月1日凌晨03:00执行），保留12个月
#
# 用法：
#   bash backup-db.sh daily      → 每日备份
#   bash backup-db.sh monthly    → 每月大版本备份
#   bash backup-db.sh            → 默认每日备份

set -euo pipefail

BACKUP_DIR="/opt/asset-management-system/data/backups"
MONTHLY_DIR="$BACKUP_DIR/monthly"
DB_FILE="/opt/asset-management-system/data/assets.db"
MODE="${1:-daily}"

mkdir -p "$BACKUP_DIR" "$MONTHLY_DIR"

# 检查数据库存在
if [ ! -f "$DB_FILE" ]; then
    echo "❌ 数据库文件不存在: $DB_FILE"
    exit 1
fi

if [ "$MODE" = "monthly" ]; then
    # ── 每月大版本备份 ──
    DATE=$(date +%Y-%m)
    BACKUP_FILE="$MONTHLY_DIR/assets_monthly_${DATE}.db"
    cp "$DB_FILE" "$BACKUP_FILE"
    echo "✅ 月度大版本备份完成: $BACKUP_FILE"
    # 保留最近12个月
    find "$MONTHLY_DIR" -name "assets_monthly_*.db" -mtime +365 -delete 2>/dev/null || true
else
    # ── 每日备份 ──
    DATE=$(date +%Y-%m-%d_%H%M)
    BACKUP_FILE="$BACKUP_DIR/assets_${DATE}.db"
    cp "$DB_FILE" "$BACKUP_FILE"
    echo "✅ 每日备份完成: $BACKUP_FILE"
    # 保留最近30天
    find "$BACKUP_DIR" -name "assets_*.db" -mtime +30 -delete 2>/dev/null || true
fi

# 显示备份列表
echo ""
echo "最近的备份："
ls -lht "$BACKUP_DIR"/assets_*.db 2>/dev/null | head -5 || true
echo ""
echo "月度大版本："
ls -lht "$MONTHLY_DIR"/assets_monthly_*.db 2>/dev/null | head -12 || true
