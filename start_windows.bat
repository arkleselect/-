@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo 未检测到 Python，请先安装 Python 3，并勾选 Add Python to PATH。
  pause
  exit /b 1
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo 未检测到 ffmpeg，请先安装 ffmpeg，并把 bin 目录加入 PATH。
  echo 推荐方式：winget install Gyan.FFmpeg
  pause
  exit /b 1
)

set LAN_IP=
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.PrefixOrigin -ne 'WellKnown' } | Select-Object -First 1 -ExpandProperty IPAddress)"`) do set LAN_IP=%%i

echo.
echo 视频压缩 Web 服务启动中...
echo 本机访问：http://localhost:8088
if not "%LAN_IP%"=="" echo 同事访问：http://%LAN_IP%:8088
echo.
echo 如同事无法访问，请允许 Windows 防火墙放行 Python，或开放 TCP 端口 8088。
echo 关闭服务请按 Ctrl+C。
echo.

set MAX_CONCURRENT_JOBS=3
python web_app.py
pause
