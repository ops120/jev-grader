@echo off
rem JEV Grader - one-click start for Windows.
rem First run installs dependencies automatically. The server listens on 127.0.0.1 only.
rem Messages are ASCII on purpose: .bat parsing breaks on UTF-8 Chinese under a GBK console.
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found. Install Python 3.11+ and add it to PATH.
  pause
  exit /b 1
)

python -c "import fastapi, uvicorn, httpx, pydantic" >nul 2>nul
if errorlevel 1 (
  echo [INFO] Installing dependencies for the first run...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [ERROR] Dependency install failed.
    pause
    exit /b 1
  )
)

if not exist ".env" (
  echo [INFO] .env not found. Browsing and uploading banks work without it;
  echo        real solving needs the JEV API key in .env - see .env.example
)

if "%JEV_GRADER_PORT%"=="" set JEV_GRADER_PORT=8848
start "" "http://127.0.0.1:%JEV_GRADER_PORT%/"
python src/server.py
pause
