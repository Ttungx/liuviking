@echo off
rem 小R科技 WiFi 小车 WASD 遥控客户端启动脚本
cd /d "%~dp0"
python src\main.py %*
if errorlevel 1 pause
