@echo off
chcp 65001
cd /d "%~dp0.."
echo === 新嘉理财师周度数据更新工具 - Windows打包脚本 ===
echo.

python --version 2>nul
if %errorlevel% neq 0 (
    echo [错误] 未找到Python，请先安装 Python 3.8+
    echo 下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)

echo 正在安装依赖...
pip install openpyxl pyinstaller -q

echo 正在打包，请稍候（约1-3分钟）...
pyinstaller --onefile --windowed ^
    --name "周度数据更新工具" ^
    --hidden-import openpyxl ^
    --hidden-import openpyxl.styles ^
    --hidden-import openpyxl.utils ^
    周度数据更新工具.py

if %errorlevel% equ 0 (
    echo.
    echo === 打包成功！===
    echo 可执行文件位于: dist\周度数据更新工具.exe
    explorer dist
) else (
    echo [错误] 打包失败，请查看上方错误信息
)
pause
