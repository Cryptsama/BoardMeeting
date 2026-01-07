@echo off
setlocal
REM Always run from the PROJECT ROOT (parent of this folder)
for %%I in ("%~dp0..") do set "ROOT=%%~fI"
cd /d "%ROOT%"

REM Activate venv from root
call "%ROOT%\.venv\Scripts\activate.bat"

REM Kill anything listening on 8000
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8000 ^| findstr LISTENING') do taskkill /PID %%a /F >nul 2>&1

REM Show your IPv4 (use it on your phone)
for /f "tokens=2 delims=:" %%i in ('ipconfig ^| findstr /R /C:"IPv4 Address"') do set IP=%%i
set IP=%IP: =%

echo.
echo Open on phone: http://%IP%:8000
echo Local:         http://127.0.0.1:8000
echo.

start "" http://127.0.0.1:8000
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
