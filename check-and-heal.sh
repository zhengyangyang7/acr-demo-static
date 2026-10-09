#!/bin/bash
# 资产管理系统 - 一键检查&自愈脚本
# 功能：服务器重启后，检查并恢复关键服务（容器、定时备份任务）
#
# 用法：
#   sudo bash check-and-heal.sh          → 检查并自动修复
#   sudo bash check-and-heal.sh status   → 只看状态不修复

set -uo pipefail

PROJECT_DIR="/opt/asset-management-system"
CRON_FILE="/etc/cron.d/ams-backup-cron"
BACKUP_SCRIPT="$PROJECT_DIR/backup-db.sh"
LOG_DIR="$PROJECT_DIR/logs"
ONLY_STATUS=false
[ "${1:-}" = "status" ] && ONLY_STATUS=true

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✅ $1${NC}"; }
fail() { echo -e "${RED}❌ $1${NC}"; }
warn() { echo -e "${YELLOW}⚠️  $1${NC}"; }
fix()  { [ "$ONLY_STATUS" = false ] && { echo -e "${YELLOW}🔧 修复: $1${NC}"; eval "$2"; }; }

echo "=============================================="
echo "  资产管理系统 健康检查 $(date '+%Y-%m-%d %H:%M')"
echo "=============================================="

# ── 1. 检查 Docker 容器 ──
echo ""
echo "【1/5】Docker 容器"
if docker ps --format '{{.Names}}' | grep -q "ams-backend"; then
    ok "ams-backend 运行中"
else
    fail "ams-backend 未运行"
    fix "启动容器" "cd $PROJECT_DIR && docker-compose up -d"
fi
if docker ps --format '{{.Names}}' | grep -q "ams-nginx"; then
    ok "ams-nginx 运行中"
else
    fail "ams-nginx 未运行"
    fix "启动容器" "cd $PROJECT_DIR && docker-compose up -d"
fi

# ── 2. 检查 cron 服务 ──
echo ""
echo "【2/5】Cron 服务"
if systemctl is-active --quiet cron 2>/dev/null || systemctl is-active --quiet crond 2>/dev/null; then
    ok "cron 服务运行中"
else
    fail "cron 服务未运行"
    fix "启动 cron" "systemctl start cron 2>/dev/null || systemctl start crond 2>/dev/null || service cron start 2>/dev/null"
fi

# ── 3. 检查定时任务配置 ──
echo ""
echo "【3/5】定时备份任务"
if [ -f "$CRON_FILE" ]; then
    ok "定时任务配置存在: $CRON_FILE"
else
    fail "定时任务配置缺失"
    if [ -f "$PROJECT_DIR/ams-backup-cron" ]; then
        fix "重新安装 cron 配置" "cp $PROJECT_DIR/ams-backup-cron $CRON_FILE && chmod 644 $CRON_FILE"
    else
        warn "找不到源配置 $PROJECT_DIR/ams-backup-cron，需手动恢复"
    fi
fi

# ── 4. 检查备份脚本 ──
echo ""
echo "【4/5】备份脚本"
if [ -f "$BACKUP_SCRIPT" ]; then
    ok "备份脚本存在: $BACKUP_SCRIPT"
    if [ -x "$BACKUP_SCRIPT" ]; then
        ok "备份脚本有执行权限"
    else
        fail "备份脚本无执行权限"
        fix "添加执行权限" "chmod +x $BACKUP_SCRIPT"
    fi
else
    fail "备份脚本缺失: $BACKUP_SCRIPT"
fi

# ── 5. 检查数据库和备份目录 ──
echo ""
echo "【5/5】数据库与备份"
DB_FILE="$PROJECT_DIR/data/assets.db"
if [ -f "$DB_FILE" ]; then
    ok "数据库存在: $DB_FILE ($(du -h "$DB_FILE" | cut -f1))"
else
    fail "数据库缺失: $DB_FILE"
fi

BACKUP_DIR="$PROJECT_DIR/data/backups"
mkdir -p "$BACKUP_DIR" "$BACKUP_DIR/monthly" "$LOG_DIR" 2>/dev/null
LATEST_BACKUP=$(ls -t "$BACKUP_DIR"/assets_*.db 2>/dev/null | head -1)
if [ -n "$LATEST_BACKUP" ]; then
    ok "最近备份: $(basename "$LATEST_BACKUP")"
else
    warn "暂无每日备份记录（定时任务首次执行后会出现）"
fi

# ── 汇总 ──
echo ""
echo "=============================================="
echo "  检查完成"
echo "  每日备份: 凌晨 02:00 → $BACKUP_DIR"
echo "  月度备份: 每月1日 03:00 → $BACKUP_DIR/monthly"
echo "=============================================="

# 如果只是看状态，提示可执行修复
if [ "$ONLY_STATUS" = true ]; then
    echo ""
    echo "提示：运行 sudo bash check-and-heal.sh 可自动修复发现的问题"
fi
