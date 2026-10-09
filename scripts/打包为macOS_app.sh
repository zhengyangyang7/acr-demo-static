#!/bin/bash
# macOS 打包脚本（在仓库根目录生成 dist/）
cd "$(dirname "$0")/.."
echo "=== 新嘉理财师周度数据更新工具 - macOS 打包脚本 ==="

pip3 install openpyxl pyinstaller -q

pyinstaller -y --onedir --windowed \
    --name "周度数据更新工具" \
    --hidden-import openpyxl \
    --hidden-import openpyxl.styles \
    --hidden-import openpyxl.utils \
    周度数据更新工具.py

echo ""
echo "打包完成！应用位于: dist/周度数据更新工具.app"
open dist
