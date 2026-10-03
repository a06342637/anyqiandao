@echo off
cd /d D:\any-signin-assistant
set TEMP=D:\any-signin-assistant\.local\tmp
set TMP=D:\any-signin-assistant\.local\tmp
set PYTHONUTF8=1
set PYTHONDONTWRITEBYTECODE=1
set PLAYWRIGHT_BROWSERS_PATH=D:\any-signin-assistant\.local\browsers
set APP_SECRET_KEY_FILE=D:\any-signin-assistant\.local\dev\secrets\app.key
set APP_ADMIN_PASSWORD_HASH_FILE=D:\any-signin-assistant\.local\dev\secrets\admin.hash
set APP_DATA_DIR=D:\any-signin-assistant\.local\dev\data
set APP_PUBLIC_URL=http://localhost:5173
D:\any-signin-assistant\.venv\Scripts\python.exe -B -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
