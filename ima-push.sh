#!/bin/bash
# IMA 推送脚本 - 打包整个项目供上传
# 用法：bash ima-push.sh

CDATE=$(date +%Y%m%d-%H%M)
FILE="ams-project-${CDATE}.tar.gz"
PROJECT="/Users/gookie.gu/WorkBuddy/2026-05-19-task-4"

cd "$PROJECT"

# 排除不需要的文件
tar -czf "$FILE" \
    --exclude='.git' \
    --exclude='data/assets.db' \
    --exclude='data/backups/*.db' \
    --exclude='data/uploads/*' \
    --exclude='node_modules' \
    --exclude='*.tar.gz' \
    --exclude='*.old' \
    --exclude='__pycache__' \
    --exclude='.DS_Store' \
    --exclude='*.pyc' \
    .

ls -lh "$FILE"
echo ""
echo "✅ 打包完成: $FILE"
echo "   请手动上传到 IMA 知识库"
