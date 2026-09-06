@echo off
rem Manga uploader - 本地覆盖更新（保留 config.yaml、漫画数据与本机环境）
rem 更新前请先关闭正在运行的程序窗口（start.bat / start-gui.bat）
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update.ps1" %*
