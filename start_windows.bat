@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo Python 설치를 확인해 주세요: https://www.python.org/downloads/
  pause
  exit /b 1
)
start "MULTI ARCADE SERVER" cmd /k "python server.py"
timeout /t 2 /nobreak >nul
start "" "http://localhost:8080"
echo 서버를 종료하려면 열린 MULTI ARCADE SERVER 창을 닫으세요.
