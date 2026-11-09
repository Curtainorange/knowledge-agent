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
rem
rem  KEEP THIS FILE ASCII-ONLY. cmd.exe parses batch files in the OEM code page
rem  (GBK on a Chinese Windows); UTF-8 Chinese comments get mangled into stray
rem  bytes, the line ends are swallowed, and comment fragments are EXECUTED as
rem  commands. Chinese documentation belongs in README.md, not here.
rem
rem  Design notes (each one is a bug we actually hit -- do not "simplify" them):
rem   1) Probe with --noproxy. Many machines set HTTP_PROXY; the proxy takes over
rem      http://127.0.0.1:8010/health and answers 502. Measured: nothing listens
rem      on 8010 and curl still returns 502. Without --noproxy the watchdog thinks
rem      the service is permanently down and restarts it every 5 minutes forever.
rem   2) Require 3 consecutive failures before acting. Restart windows and model
rem      cold starts return 5xx for a few seconds; one failed probe is not proof.
rem   3) /end BEFORE /run, and never match on the status text. If the process died
rem      while the task state is still "Running", /run is refused ("task is already
rem      running") and a run-only watchdog can never recover. Matching the English
rem      word "Running" also fails on a Chinese Windows, where the status field is
rem      localized. Do not match status text at all.
rem   4) Use ping for delays, not timeout: timeout errors out with no console (SYSTEM).
rem   5) Do not taskkill processes. /end is enough; SQLite is crash-safe.
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

rem Stage 1: up to 3 probes. Any 200 means healthy -> exit silently.
set "TRIES=0"
:probe_loop
call :probe
if not errorlevel 1 exit /b 0
set /a TRIES+=1
if %TRIES% GEQ 3 goto :act
ping -n 6 127.0.0.1 >nul
goto :probe_loop

rem Stage 2: really down -> restart
:act
echo [%date% %time%] health=%CODE% after %TRIES% probes -^> restarting %TASK% >> "%LOG%"
schtasks /end /tn "%TASK%" >> "%LOG%" 2>&1
call :wait_port_free 15
schtasks /run /tn "%TASK%" >> "%LOG%" 2>&1

rem Stage 3: wait for it. Cold start loads the local ONNX model; ~20s is normal.
set "TRIES=0"
:wait_loop
ping -n 11 127.0.0.1 >nul
call :probe
if not errorlevel 1 goto :recovered
set /a TRIES+=1
if %TRIES% GEQ 4 goto :gave_up
goto :wait_loop

:recovered
echo [%date% %time%] recovered health=200 (restart ok) >> "%LOG%"
exit /b 0

:gave_up
echo [%date% %time%] STILL DOWN health=%CODE% - manual check needed, not force-killing >> "%LOG%"
exit /b 0

rem ------------------------------- helpers ---------------------------------
:probe
rem One probe: 200 -> return 0 (CODE=200); otherwise return 1 (CODE=actual code)
set "CODE=000"
curl.exe -s --noproxy "*" -o nul -w "%%{http_code}" --max-time 5 "http://127.0.0.1:%PORT%/health" > "%TMPF%" 2>nul
set /p CODE=<"%TMPF%" 2>nul
if "%CODE%"=="" set "CODE=000"
if "%CODE%"=="200" exit /b 0
exit /b 1

:wait_port_free
rem Arg: max seconds to wait. /end does not release the port instantly, and
rem starting a new instance too early just makes it fail to bind.
set "W=0"
:fp_loop
set "BUSY="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT% " ^| findstr "LISTENING"') do set "BUSY=%%p"
if not defined BUSY exit /b 0
set /a W+=1
if %W% GEQ %1 exit /b 1
ping -n 2 127.0.0.1 >nul
goto :fp_loop

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
echo.
echo      Note: update.bat pauses this task while updating. If an update was
echo      interrupted and the task stays disabled, re-enable it with:
echo        schtasks /change /tn "%WD_TASK%" /enable
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
curl.exe -s --noproxy "*" -o nul -w "health=%%{http_code}\n" --max-time 5 "http://127.0.0.1:%PORT%/health"
echo.
echo ---- last 10 watchdog lines ----
if exist "%LOG%" powershell -NoProfile -Command "Get-Content '%LOG%' -Tail 10"
exit /b 0
