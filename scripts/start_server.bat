@echo off
rem 默认启动：绑定 0.0.0.0，局域网/手机可达（详见 README「手机接入」）。
rem 只想本机使用时改跑 start_server_local.bat。
pushd "%~dp0.."
echo ===== start %date% %time% ===== >> "%~dp0_srv8000.log"
"D:\Users\Curtain\anaconda3\python.exe" -u -m uvicorn app.main:app --host 0.0.0.0 --port 8000 >> "%~dp0_srv8000.log" 2>&1
popd
