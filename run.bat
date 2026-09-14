@echo off
chcp 65001 >nul
echo ========================================
echo   本地漫剧自动生成系统
echo ========================================
echo.

REM 检查 ComfyUI 是否运行
curl -s http://127.0.0.1:8188/system_stats >nul 2>&1
if errorlevel 1 (
    echo [错误] ComfyUI 未运行，请先启动 ComfyUI
    pause
    exit /b 1
)

REM 检查 API Key
if "%ANTHROPIC_API_KEY%"=="" (
    echo [错误] 请设置 ANTHROPIC_API_KEY 环境变量
    echo 示例: set ANTHROPIC_API_KEY=sk-ant-xxx
    pause
    exit /b 1
)

REM 运行生成
python "%~dp0comic_drama_pipeline.py" %*

echo.
echo ========================================
echo   生成完成！
echo   输出目录: ComfyUI\output\comic_drama\
echo ========================================
pause
