@echo off
echo ========================================
echo   漫剧工坊 - EXE打包脚本
echo ========================================
echo.

REM 检查Python环境
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到Python，请先安装Python 3.11+
    pause
    exit /b 1
)

REM 安装依赖
echo [1/3] 安装依赖...
pip install -r requirements.txt --quiet
pip install pyinstaller --quiet

REM 清理旧构建
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist *.spec del /q *.spec.bak

REM 打包EXE
echo [2/3] 开始打包...
pyinstaller --clean 漫剧工坊.spec

REM 创建启动脚本
echo [3/3] 创建启动脚本...
echo @echo off > 启动漫剧工坊.bat
echo start "" "dist\漫剧工坊.exe" >> 启动漫剧工坊.bat

echo.
echo ========================================
echo   打包完成！
echo ========================================
echo.
echo 输出目录: dist\
echo 可执行文件: dist\漫剧工坊.exe
echo 启动脚本: 启动漫剧工坊.bat
echo.
echo 提示: 首次运行可能需要安装ComfyUI
pause
