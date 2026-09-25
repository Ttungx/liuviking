@echo off
rem 小R科技 WiFi 小车 PC 端控制台启动脚本（纯标准库，无需 pip install）
rem 启动本地 HTTP 服务并自动打开浏览器
cd /d "%~dp0"
python src\main.py %*
if errorlevel 1 pause
