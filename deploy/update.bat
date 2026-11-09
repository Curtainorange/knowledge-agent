@echo off
setlocal enabledelayedexpansion
rem 更新线上服务：备份 -> 停 -> 拉代码 -> 装依赖 -> 起 -> 等健康 -> 自检。
rem 在**管理员 cmd** 里执行。只动本项目这一个计划任务，别的项目不受影响。
rem
rem 为什么用 schtasks 而不是 nssm：这台服务器上实际托管本项目的就是任务计划
rem （cognition-copilot，SYSTEM / ONSTART），nssm.exe 根本没装。
rem 早期版本的脚本写的是 nssm + `sc query`，在这台机器上会第一步就 abort——
rem 「更新不了」比「更新出错」更难查，所以脚本必须和现实一致。

set "APP_DIR=E:\projects\renzhifujia"
set "TASK=cognition-copilot"
set "PORT=8010"
set "PY=%APP_DIR%\.venv\Scripts\python.exe"

net session >nul 2>&1
if errorlevel 1 (
  echo [FAIL] 需要管理员权限，请用「以管理员身份运行」打开 cmd。
  exit /b 1
)

rem ---- 安全检查：只碰自己的任务 ----
schtasks /query /tn "%TASK%" >nul 2>&1
if errorlevel 1 (
  echo [FAIL] 找不到计划任务 "%TASK%"。
  echo        先按 deploy\README.md 第 5 节建任务，再回来跑这个脚本。
  exit /b 1
)
rem 用 /fo list /v：不带 /v 的 list 只输出任务名/下次运行时间/状态，
rem 不含「要运行的任务」，find 永远找不到路径，这道校验会永远误报「不是本项目」。
schtasks /query /tn "%TASK%" /fo list /v | find /i "%APP_DIR%" >nul
if errorlevel 1 (
  echo [FAIL] 任务 "%TASK%" 存在，但命令里不含 %APP_DIR%，不是本项目。
  echo        什么都没改。
  exit /b 1
)

if not exist "%APP_DIR%\logs" mkdir "%APP_DIR%\logs"

echo [1/6] 备份数据库与书籍文件...
"%PY%" "%APP_DIR%\scripts\backup.py"
if errorlevel 1 (
  echo [FAIL] 备份失败，已中止——不在没备份的情况下动数据。
  exit /b 1
)

echo [2/6] 停服务...
schtasks /end /tn "%TASK%" >nul 2>&1

rem 关键一步：/end 结束的是任务实例，个别情况下子进程会活着并继续占着端口。
rem 那时新实例起不来、旧进程继续服务，表现为「代码更新了但行为没变」——
rem 本项目为这类「新旧两个服务器」误判过多次，所以这里必须实测端口确实空出来。
call :wait_port_free 10
if errorlevel 1 (
  echo [FAIL] 停服务后 %PORT% 仍被占用，可能有残留进程。
  echo        查占用：netstat -ano ^| findstr ":%PORT% "
  echo        确认是本项目的旧进程后：taskkill /pid ^<PID^> /f /t
  exit /b 1
)

echo [3/6] 拉取代码...
pushd "%APP_DIR%"
git pull --ff-only
if errorlevel 1 (
  echo [WARN] git pull 失败。请确认远端与分支，或手工把新代码覆盖进来。
  echo        覆盖时**不要动** .env / dev.db / data\ / models\ —— 它们不在版本库里。
  popd
  echo [FAIL] 代码没更新，为免「停着不起来」，这里直接中止。
  schtasks /run /tn "%TASK%" >nul 2>&1
  echo        已尝试把服务重新拉起。
  exit /b 1
)
echo [4/6] 安装依赖...
"%PY%" -m pip install -e . --quiet
if errorlevel 1 (
  echo [WARN] pip install 失败，继续启动——本次改动没有新增依赖，
  echo        但若启动报 ModuleNotFoundError，先回来解决依赖。
)
popd

echo [5/6] 启动服务...
schtasks /run /tn "%TASK%" >nul 2>&1

echo [6/6] 等健康检查...
set "CODE=000"
for /l %%i in (1,1,15) do (
  for /f %%c in ('curl.exe -s -o nul -w "%%{http_code}" http://127.0.0.1:%PORT%/health') do set "CODE=%%c"
  if "!CODE!"=="200" goto :healthy
  timeout /t 2 >nul
)
echo [FAIL] 60 秒内 /health 没返回 200（最后一次：!CODE!）。
echo        看日志："%APP_DIR%\logs\app.log"
echo        常见原因：工作目录不对、依赖缺失、端口被占、.env 有语法错误。
exit /b 1

:healthy
echo         健康检查通过（200）。
echo.
echo 上线自检（核对代码版本 / 配置 / 数据库 / 联网检索）：
"%PY%" "%APP_DIR%\scripts\check_deploy.py"
if errorlevel 1 (
  echo.
  echo [WARN] 自检有未通过项。服务已经在跑，但请按上面的 ✗ 逐条修。
)
echo.
echo 内网入口：http://10.100.128.129/cognition/
echo 本机入口：http://127.0.0.1:%PORT%/
echo 回滚：git -C "%APP_DIR%" reset --hard ^<上一个提交^> 后重新执行本脚本
endlocal
exit /b 0

rem ---------------------------------------------------------------------------
:wait_port_free
rem 参数：最多等几秒。端口空出来返回 0，仍被占用返回 1。
for /l %%i in (1,1,%1) do (
  set "BUSY="
  for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT% " ^| findstr "LISTENING"') do set "BUSY=%%p"
  if not defined BUSY exit /b 0
  timeout /t 1 >nul
)
exit /b 1
