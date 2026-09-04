@echo off
rem Manga uploader - web UI one-click launcher (double-click entry, forwards to start-web.ps1)
rem Usage examples: start.bat -Lan -Port 9000
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-web.ps1" %*
