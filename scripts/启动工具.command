#!/bin/bash
# 周度数据更新工具 - 启动脚本（双击运行）
cd "$(dirname "$0")/.."

PYTHON=$(command -v python3 || command -v python)
if [ -z "$PYTHON" ]; then
    echo "错误: 未找到 Python，请先安装 Python 3.8+"
    exit 1
fi

echo "正在启动周度数据更新工具..."
echo "如提示「无法打开」，请右键点击 → 打开"
"$PYTHON" "周度数据更新工具.py"
