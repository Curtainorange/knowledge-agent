@echo off
setlocal
rem Install Cognitive Copilot as a Windows service.
rem Run in an elevated cmd: right-click -> "Run as administrator".
rem
rem Why a service and not a plain "start the server" window:
rem   this box is shared by stu1/stu2 through RDP, and only 2 sessions are allowed.
rem   Whatever is started inside an interactive session dies when that user logs off.
rem   A service keeps running with nobody logged in (and after reboot).

set "APP_DIR=E:\projects\renzhifujia"
set "NSSM=D:\Software\nssm\nssm.exe"
set "SVC=cognition-copilot"
set "PORT=8010"

set "PY=%APP_DIR%\.venv\Scripts\python.exe"

net session >nul 2>&1
if errorlevel 1 (
  echo [FAIL] Administrator privileges required.
  exit /b 1
)
if not exist "%NSSM%" (
  echo [FAIL] nssm.exe not found: %NSSM%
  exit /b 1
)
if not exist "%PY%" (
  echo [FAIL] venv python not found: %PY%
  echo        create it first: python -m venv "%APP_DIR%\.venv"
  exit /b 1
)
if not exist "%APP_DIR%\logs" mkdir "%APP_DIR%\logs"
if not exist "%APP_DIR%\.env" (
  echo [WARN] .env missing. JWT_SECRET must be set, otherwise every restart
  echo        invalidates all tokens and both users get logged out.
)

rem Safety: never stop or remove a service we do not own.
rem If the name is taken by someone else's service, refuse and change nothing.
sc query "%SVC%" >nul 2>&1
if not errorlevel 1 (
  reg query "HKLM\SYSTEM\CurrentControlSet\Services\%SVC%\Parameters" /v AppDirectory 2>nul | find /i "%APP_DIR%" >nul
  if errorlevel 1 (
    echo [FAIL] A service named "%SVC%" already exists and does not belong to this project.
    echo        Nothing was changed. Edit SVC in this script to a unique name, then rerun.
    exit /b 1
  )
)

rem Reinstall cleanly: running this twice is safe.
"%NSSM%" stop %SVC% >nul 2>&1
"%NSSM%" remove %SVC% confirm >nul 2>&1

"%NSSM%" install %SVC% "%PY%"
"%NSSM%" set %SVC% AppParameters "-m uvicorn app.main:app --host 127.0.0.1 --port %PORT%"
"%NSSM%" set %SVC% AppDirectory "%APP_DIR%"
"%NSSM%" set %SVC% DisplayName "Cognitive Copilot"
"%NSSM%" set %SVC% Description "Personal knowledge learning agent (FastAPI on 127.0.0.1:%PORT%)"
"%NSSM%" set %SVC% Start SERVICE_AUTO_START
"%NSSM%" set %SVC% AppStdout "%APP_DIR%\logs\app.log"
"%NSSM%" set %SVC% AppStderr "%APP_DIR%\logs\app.log"
"%NSSM%" set %SVC% AppRotateFiles 1
"%NSSM%" set %SVC% AppRotateOnline 1
"%NSSM%" set %SVC% AppRotateBytes 10485760
"%NSSM%" set %SVC% AppExit Default Restart
"%NSSM%" start %SVC%

echo.
echo Waiting for the app to come up...
timeout /t 5 >nul
curl.exe -s -o nul -w "health: %%{http_code}\n" http://127.0.0.1:%PORT%/health
echo.
echo If health is not 200: see "%APP_DIR%\logs\app.log"
echo Next: copy deploy\nginx\cognition.conf to %APP_DIR%\nginx\ and include it from nginx
endlocal
