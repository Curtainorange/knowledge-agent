@echo off
rem 仅本机启动：绑定 127.0.0.1，不对局域网暴露（无手机接入需求时更收敛）。
pushd "%~dp0.."
echo ===== start-local %date% %time% ===== >> "%~dp0_srv8000.log"
"D:\Users\Curtain\anaconda3\python.exe" -u -m uvicorn app.main:app --host 127.0.0.1 --port 8000 >> "%~dp0_srv8000.log" 2>&1
popd
