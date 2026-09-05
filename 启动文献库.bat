@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动化学文献库服务...
start "文献库服务" /D "%~dp0" cmd /c "chcp 65001>nul & .venv\Scripts\python.exe -m uvicorn app.web:app --host 0.0.0.0 --port 8011 >> server.log 2>&1"
timeout /t 4 /nobreak >nul
start "" http://127.0.0.1:8011
echo 服务已启动，浏览器已打开。关掉“文献库服务”窗口即停止服务。
pause
