@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "APP=app:app"
if exist "%~dp0auto_B.py" set "APP=auto_B:app"
if exist "%~dp0app.py"    set "APP=app:app"
if exist "%~dp0main.py"   set "APP=main:app"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

set "MODE=%~1"
set "HOST=127.0.0.1"
if /I "%MODE%"=="lan" set "HOST=0.0.0.0"

for /f "tokens=5" %%P in ('netstat -aon ^| findstr /r /c:":8000 .*LISTENING"') do (
  taskkill /F /PID %%P >nul 2>nul
)

if /I "%MODE%"=="lan" (
  for /f "tokens=2 delims=:" %%A in ('ipconfig ^| findstr /c:"IPv4 Address"') do set "IP=%%A"
  set "IP=!IP: =!"
  echo Open on phone: http://!IP!:8000
) else (
  echo Open on PC: http://localhost:8000
)

echo Starting: %APP% on %HOST%:8000
"%PY%" -m uvicorn %APP% --host %HOST% --port 8000 --reload --log-level info
echo.
echo Uvicorn exited with code: %ERRORLEVEL%
pause