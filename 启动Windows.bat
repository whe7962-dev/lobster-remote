@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
py -3 --version >nul 2>&1
if errorlevel 1 (
  echo 请先从 https://www.python.org/downloads/windows/ 安装 Python 3.10 或更高版本，保留 Python Launcher。
  pause
  exit /b 1
)
if not exist .venv\Scripts\python.exe py -3 -m venv .venv
.venv\Scripts\python.exe -c "import playwright" >nul 2>&1
if errorlevel 1 (
  .venv\Scripts\python.exe -m pip install -r requirements.txt --index-url https://pypi.org/simple
  if errorlevel 1 exit /b 1
)
.venv\Scripts\python.exe app.py
pause
