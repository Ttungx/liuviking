@echo off
rem 打包 Windows 控制台（产物在 dist\xiaor-remote\）
rem 前端静态文件用 --add-data 一起打进去，运行时从 sys._MEIPASS 读取
cd /d "%~dp0"
python -m pip install --upgrade pyinstaller
if errorlevel 1 exit /b 1
pyinstaller --noconfirm --clean --console --name xiaor-remote --add-data "src\web;src\web" app.py
if errorlevel 1 exit /b 1
echo.
echo 打包完成：dist\xiaor-remote\xiaor-remote.exe
pause
