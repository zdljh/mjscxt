@echo off
chcp 65001 >nul
echo ========================================
echo 漫剧生成系统 - Flask 服务重启脚本
echo ========================================
echo.

REM 统一使用 venv Python 3.14
set PYTHON=C:\Users\liujianghua\.workbuddy\binaries\python\envs\mjscxt\Scripts\python.exe

echo [1/3] 停止旧进程...
taskkill /F /IM python.exe /FI "WINDOWTITLE eq *serve*" >nul 2>&1
timeout /t 2 /nobreak >nul

echo [2/3] 启动新服务...
cd /d "%~dp0"
start "漫剧生成系统-Flask" %PYTHON% app/serve.py

echo [3/3] 等待服务启动...
timeout /t 5 /nobreak >nul

echo.
echo ========================================
echo 服务已启动！
echo Python: %PYTHON%
echo 访问地址: http://127.0.0.1:5000
echo ========================================
echo.
pause
