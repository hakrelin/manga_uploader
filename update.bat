@echo off
chcp 65001 >nul
rem Manga uploader - local overwrite update (keeps config.yaml, comics, .venv/.tools)
rem Close the running program window (start.bat / start-gui.bat) before updating.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update.ps1" %*
