@echo off
chcp 936 >nul
title 热点自动助手 - 调试启动
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 没有在 PATH 里找到 python。
    echo 请安装 Python 3.9+ 并勾选 "Add Python to PATH"。
    pause
    exit /b 1
)

echo 正在启动热点自动助手...
echo 关闭本窗口不会结束程序，请用界面按钮或托盘菜单退出。
echo.
python app.py %*
set code=%errorlevel%
echo.
if not "%code%"=="0" (
    echo [退出码 %code%] 程序异常结束，请把上面的输出发出来排查。
    pause
)
exit /b %code%
