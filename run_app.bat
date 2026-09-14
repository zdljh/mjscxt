@echo off
chcp 65001 >nul
echo ========================================
echo   漫剧生成系统 - Web 应用启动器
echo ========================================
echo.

REM 设置环境变量
set COMFYUI_URL=http://127.0.0.1:8188
set LLM_PROVIDER=anthropic
REM 请设置你的 API Key
rem set ANTHROPIC_API_KEY=sk-ant-xxx

echo [1/3] 检查 ComfyUI...
curl -s http://127.0.0.1:8188/system_stats >nul 2>&1
if errorlevel 1 (
    echo [警告] ComfyUI 未运行，请先启动 ComfyUI
    echo        路径: D:\ComfyUI_portable_TE_v260619
    pause
    exit /b 1
)
echo [OK] ComfyUI 已连接

echo.
echo [2/3] 检查依赖...
python -c "import flask, requests, docx, pypdf, ebooklib, charset_normalizer" 2>nul
if errorlevel 1 (
    echo 正在安装依赖...
    cd "%~dp0app"
    pip install -r requirements.txt
)
echo [OK] 依赖已就绪

echo.
echo [3/3] 启动应用（生产级 WSGI 服务器）...
echo.
echo 访问地址: http://localhost:5000
echo 按 Ctrl+C 停止服务
echo.
REM 必须用 serve.py 而不是 app.py：Flask 自带开发服务器无法可靠处理小说上传
cd "%~dp0app"
python serve.py

pause
