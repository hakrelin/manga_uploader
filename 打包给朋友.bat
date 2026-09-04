@echo off
rem Pack a clean copy to share with friends (excludes .venv / config.yaml / output)
rem To let friends skip downloading Python: pack-for-friend.ps1 -IncludeTools
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0pack-for-friend.ps1" %*
pause
