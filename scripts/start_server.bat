@echo off
pushd "%~dp0.."
echo ===== start %date% %time% ===== >> "%~dp0_srv8000.log"
"D:\Users\Curtain\anaconda3\python.exe" -u -m uvicorn app.main:app --host 127.0.0.1 --port 8000 >> "%~dp0_srv8000.log" 2>&1
popd
