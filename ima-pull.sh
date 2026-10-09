#!/bin/bash
# IMA 拉取脚本 - 从下载的包恢复项目
# 用法：bash ima-pull.sh <下载的文件.tar.gz>

set -e
PROJECT="/Users/gookie.gu/WorkBuddy/2026-05-19-task-4"

TARFILE="${1:-}"
if [ -z "$TARFILE" ] || [ ! -f "$TARFILE" ]; then
    echo "用法: bash ima-pull.sh <从IMA下载的.tar.gz文件>"
    echo ""
    echo "步骤:"
    echo "  1. 从 IMA 下载最新的 ams-project-*.tar.gz"
    echo "  2. bash ima-pull.sh ~/Downloads/ams-project-20260725-1200.tar.gz"
    exit 1
fi

echo "从 IMA 恢复项目..."
echo "  源文件: $TARFILE"
echo "  目标:   $PROJECT"

# 备份当前
if [ -f "$PROJECT/ams-deploy-package/deploy.sh" ]; then
    BAK="$PROJECT/../task-4-bak-$(date +%Y%m%d-%H%M)"
    cp -r "$PROJECT" "$BAK"
    echo "  已备份到: $BAK"
fi

# 恢复
tar -xzf "$TARFILE" -C "$PROJECT"
echo ""
echo "✅ 恢复完成！"
echo "  文件数: $(find "$PROJECT" -type f | wc -l | tr -d ' ')"
