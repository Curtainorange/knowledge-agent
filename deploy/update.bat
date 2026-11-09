@echo off
setlocal enabledelayedexpansion
rem ---------------------------------------------------------------------------
rem  Update the live service:
rem    backup -> pause watchdog -> stop -> git pull -> pip -> start -> health -> selfcheck
rem  Run in an ELEVATED cmd. Touches only this project's two scheduled tasks.
rem
rem  Why schtasks and not nssm: this box hosts the app as a scheduled task
rem  (cognition-copilot, SYSTEM / ONSTART). nssm.exe is NOT installed. The first
rem  version of this script used `sc query` + `nssm stop` and aborted on step one;
rem  "cannot update at all" is harder to diagnose than "update failed", so the
rem  script has to match reality.
rem
rem  KEEP THIS FILE ASCII-ONLY. cmd.exe parses batch files in the OEM code page
rem  (GBK on a Chinese Windows); UTF-8 Chinese comments get mangled into stray
rem  bytes, line ends get swallowed, and comment fragments are EXECUTED as
rem  commands. Chinese documentation belongs in README.md, not here.
rem ---------------------------------------------------------------------------

set "APP_DIR=E:\projects\renzhifujia"
set "TASK=cognition-copilot"
set "WD_TASK=cognition-watchdog"
set "PORT=8010"
set "PY=%APP_DIR%\.venv\Scripts\python.exe"

rem Non-empty only if we actually disabled the watchdog (so :fail can restore it)
set "WD_PAUSED="

net session >nul 2>&1
if errorlevel 1 (
  echo [FAIL] Administrator privileges required.
  echo        Right-click cmd - Run as administrator, then run this script again.
  exit /b 1
)

rem ---- Safety check: only ever touch our own task ----
schtasks /query /tn "%TASK%" >nul 2>&1
if errorlevel 1 (
  echo [FAIL] Scheduled task "%TASK%" not found.
  echo        Create it first (deploy\README.md section 5), then come back.
  exit /b 1
)
rem MUST use /fo list /v. Without /v the listing only has task name / next run
rem time / status -- no "Task To Run" line -- so find never sees the path and
rem this guard always reports "not this project" and exits on step one.
schtasks /query /tn "%TASK%" /fo list /v | find /i "%APP_DIR%" >nul
if errorlevel 1 (
  echo [FAIL] Task "%TASK%" exists but its command does not contain %APP_DIR%.
  echo        Nothing was changed.
  exit /b 1
)

if not exist "%APP_DIR%\logs" mkdir "%APP_DIR%\logs"

echo [1/8] Backup database and book files...
"%PY%" "%APP_DIR%\scripts\backup.py"
if errorlevel 1 (
  echo [FAIL] Backup failed - aborting. Never touch data without a backup.
  exit /b 1
)

rem ---- Pause the watchdog: this is the step that prevents a FAKE SUCCESS ----
rem Between stop and start there is a window of tens of seconds. The watchdog
rem fires every 5 minutes; if it lands in that window it sees 502 and runs the
rem task, bringing the service up on the OLD code. The worst outcome is not a
rem failure but this script probing 200 and reporting success while the old
rem process serves traffic. That is far harder to diagnose than a plain failure.
echo [2/8] Pausing watchdog...
schtasks /query /tn "%WD_TASK%" >nul 2>&1
if not errorlevel 1 (
  schtasks /change /tn "%WD_TASK%" /disable >nul 2>&1
  if errorlevel 1 (
    echo [FAIL] Could not disable "%WD_TASK%".
    echo        It would bring the service up on old code during the update and
    echo        make the result look successful. Run this first, then retry:
    echo          schtasks /change /tn "%WD_TASK%" /disable
    echo        Nothing was changed.
    exit /b 1
  )
  set "WD_PAUSED=1"
  echo        disabled (will be re-enabled before exit)
) else (
  echo        not installed, skipped
)

echo [3/8] Stopping service...
schtasks /end /tn "%TASK%" >nul 2>&1

rem Important: /end ends the task INSTANCE; sometimes a child process survives
rem and keeps holding the port. Then the new instance cannot bind, the old
rem process keeps serving, and it looks like "code updated but behaviour did not
rem change". This project has been fooled by that several times, so verify the
rem port is really free before pulling.
call :wait_port_free 10
if errorlevel 1 (
  echo [FAIL] Port %PORT% is still held after stopping the service.
  echo        Find the owner:  netstat -ano ^| findstr ":%PORT% "
  echo        If it is our old process:  taskkill /pid ^<PID^> /f /t
  goto :fail
)

echo [4/8] Pulling code...
pushd "%APP_DIR%"
git pull --ff-only
if errorlevel 1 (
  echo [WARN] git pull failed. Check the remote and the branch, or copy the new
  echo        code in by hand. When copying: NEVER touch .env / dev.db / data\ /
  echo        models\ - they are not in the repository.
  popd
  echo [FAIL] Code not updated. Bringing the service back up and aborting.
  schtasks /run /tn "%TASK%" >nul 2>&1
  goto :fail
)
echo [5/8] Installing dependencies...
"%PY%" -m pip install -e . --quiet
if errorlevel 1 (
  echo [WARN] pip install failed, starting anyway - this change adds no new
  echo        dependency, but if startup reports ModuleNotFoundError fix this first.
)
popd

echo [6/8] Starting service...
schtasks /run /tn "%TASK%" >nul 2>&1

echo [7/8] Waiting for health (cold start loads the ONNX model; up to 60s)...
set "CODE=000"
for /l %%i in (1,1,30) do (
  for /f %%c in ('curl.exe -s --noproxy "*" -o nul -w "%%{http_code}" --max-time 3 http://127.0.0.1:%PORT%/health') do set "CODE=%%c"
  if "!CODE!"=="200" goto :healthy
  ping -n 3 127.0.0.1 >nul
)
echo [FAIL] /health did not return 200 within 60s (last: !CODE!).
echo        Check the log: "%APP_DIR%\logs\app.log"
echo        Usual causes: wrong working directory, missing dependency, port busy,
echo                      syntax error in .env.
goto :fail

:healthy
echo        health OK (200).

echo [8/8] Re-enabling watchdog...
call :resume_wd

echo.
echo Deploy self-check (code version / config / database / web search):
"%PY%" "%APP_DIR%\scripts\check_deploy.py"
if errorlevel 1 (
  echo.
  echo [WARN] Some self-check items failed. The service is running - fix each
  echo        item marked with a cross above.
)
echo.
echo LAN entry : http://10.100.128.129/cognition/
echo Local     : http://127.0.0.1:%PORT%/
echo Rollback  : git -C "%APP_DIR%" reset --hard ^<previous commit^> and rerun.
endlocal
exit /b 0

:fail
rem Every failure path lands here: restore the watchdog first, otherwise it stays
rem disabled and the service is effectively unsupervised from then on.
echo.
call :resume_wd
echo Update NOT completed - handle the error above.
endlocal
exit /b 1

rem ---------------------------------------------------------------------------
:resume_wd
rem Only restores if we actually disabled it; safe to call twice.
if not defined WD_PAUSED exit /b 0
schtasks /change /tn "%WD_TASK%" /enable >nul 2>&1
if errorlevel 1 (
  echo [WARN] Could not re-enable "%WD_TASK%" - it is DISABLED right now.
  echo        Run by hand: schtasks /change /tn "%WD_TASK%" /enable
) else (
  echo        watchdog re-enabled
)
set "WD_PAUSED="
exit /b 0

:wait_port_free
rem Arg: max seconds. Returns 0 when the port is free, 1 if still busy.
for /l %%i in (1,1,%1) do (
  set "BUSY="
  for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT% " ^| findstr "LISTENING"') do set "BUSY=%%p"
  if not defined BUSY exit /b 0
  ping -n 2 127.0.0.1 >nul
)
exit /b 1
