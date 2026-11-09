@echo off
rem ---------------------------------------------------------------------------
rem  Cognition watchdog.  Probe /health; if not 200, restart the EXISTING task.
rem  It NEVER touches nginx, other projects, or any other scheduled task.
rem
rem  Usage (elevated cmd on the server):
rem      watchdog.bat            -> run one patrol (what the timer calls)
rem      watchdog.bat install    -> create task "cognition-watchdog", every 5 min
rem      watchdog.bat remove     -> delete only "cognition-watchdog"
rem      watchdog.bat status     -> show task state + last health code
rem
rem  All output goes to E:\projects\renzhifujia\logs\watchdog.log
rem  Design notes:
rem   - uses ping for delay, NOT timeout: timeout fails under SYSTEM (no console)
rem   - never force-kills a hung process (/end would risk corrupting SQLite);
rem     if restart does not help it logs STILL DOWN and waits for a human
rem ---------------------------------------------------------------------------
setlocal
set "PORT=8010"
set "TASK=cognition-copilot"
set "BASE=E:\projects\renzhifujia"
set "LOG=%BASE%\logs\watchdog.log"
set "WD_TASK=cognition-watchdog"
set "TMPF=%TEMP%\_cog_wd_code.txt"
set "SELF=%~f0"

if /i "%~1"=="install" goto :install
if /i "%~1"=="remove"  goto :remove
if /i "%~1"=="status"  goto :status

rem ------------------------------- one patrol -------------------------------
if not exist "%BASE%\logs" mkdir "%BASE%\logs"

set "CODE=000"
curl.exe -s -o nul -w "%%{http_code}" --max-time 5 "http://127.0.0.1:%PORT%/health" > "%TMPF%" 2>nul
set /p CODE=<"%TMPF%" 2>nul
if "%CODE%"=="" set "CODE=000"

if "%CODE%"=="200" exit /b 0

echo [%date% %time%] health=%CODE% -> restarting %TASK% >> "%LOG%"
schtasks /run /tn "%TASK%" >> "%LOG%" 2>&1

ping -n 13 127.0.0.1 >nul

set "CODE2=000"
curl.exe -s -o nul -w "%%{http_code}" --max-time 5 "http://127.0.0.1:%PORT%/health" > "%TMPF%" 2>nul
set /p CODE2=<"%TMPF%" 2>nul
if "%CODE2%"=="" set "CODE2=000"

echo [%date% %time%] after restart health=%CODE2% >> "%LOG%"
if not "%CODE2%"=="200" (
  echo [%date% %time%] STILL DOWN - manual check needed, not force-killing >> "%LOG%"
)
exit /b 0

rem ------------------------------- install ---------------------------------
:install
net session >nul 2>&1
if errorlevel 1 (
  echo [FAIL] Administrator privileges required. Right-click cmd - Run as administrator.
  exit /b 1
)

rem Safety: never overwrite a task we do not own.
schtasks /query /tn "%WD_TASK%" >nul 2>&1
if not errorlevel 1 (
  echo [FAIL] Task "%WD_TASK%" already exists. Nothing was changed.
  echo        If it is ours, run: watchdog.bat remove
  exit /b 1
)

rem Safety: the task we are guarding must exist and belong to this project.
schtasks /query /tn "%TASK%" /fo list /v | find /i "%BASE%" >nul
if errorlevel 1 (
  echo [FAIL] Task "%TASK%" not found, or its command does not contain %BASE%.
  echo        Nothing was changed.
  exit /b 1
)

schtasks /create /tn "%WD_TASK%" /tr "cmd /c %SELF%" /sc minute /mo 5 /ru SYSTEM /rl HIGHEST /f
if errorlevel 1 (
  echo [FAIL] Could not create the task.
  echo        If the path below contains spaces, move this file to a path without spaces:
  echo        %SELF%
  exit /b 1
)

schtasks /run /tn "%WD_TASK%" >nul 2>&1
echo [OK] "%WD_TASK%" installed - runs every 5 minutes as SYSTEM.
echo      Log: %LOG%
exit /b 0

rem ------------------------------- remove ----------------------------------
:remove
net session >nul 2>&1
if errorlevel 1 (
  echo [FAIL] Administrator privileges required.
  exit /b 1
)
schtasks /query /tn "%WD_TASK%" >nul 2>&1
if errorlevel 1 (
  echo [INFO] "%WD_TASK%" does not exist. Nothing to remove.
  exit /b 0
)
schtasks /delete /tn "%WD_TASK%" /f
echo [OK] "%WD_TASK%" removed. "%TASK%" was not touched.
exit /b 0

rem ------------------------------- status ----------------------------------
:status
echo ---- watchdog task ----
schtasks /query /tn "%WD_TASK%" /fo list 2>nul || echo (not installed)
echo.
echo ---- upstream ----
netstat -ano | findstr ":%PORT% "
echo.
set "CODE=000"
curl.exe -s -o nul -w "health=%%{http_code}\n" --max-time 5 "http://127.0.0.1:%PORT%/health"
echo.
echo ---- last 10 watchdog lines ----
if exist "%LOG%" powershell -NoProfile -Command "Get-Content '%LOG%' -Tail 10"
exit /b 0
