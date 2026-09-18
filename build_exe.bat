@echo off
rem 打包 Windows 单文件 GUI（产物在 dist\xiaor-remote\）
cd /d "%~dp0"
python -m pip install --upgrade pyinstaller
if errorlevel 1 exit /b 1
pyinstaller --noconfirm --clean --windowed --name xiaor-remote app.py
if errorlevel 1 exit /b 1
echo.
echo 打包完成：dist\xiaor-remote\xiaor-remote.exe
pause
