@echo off
rem Manga uploader - legacy tkinter GUI launcher (double-click entry, forwards to start-gui.ps1)
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-gui.ps1"
