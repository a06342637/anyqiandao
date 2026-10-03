@echo off
cd /d D:\any-signin-assistant\frontend
set TEMP=D:\any-signin-assistant\.local\tmp
set TMP=D:\any-signin-assistant\.local\tmp
set NODE_DISABLE_COMPILE_CACHE=1
D:\any-signin-assistant\.local\node.exe D:\any-signin-assistant\frontend\node_modules\vite\bin\vite.js --host 127.0.0.1 --port 5173 --strictPort
