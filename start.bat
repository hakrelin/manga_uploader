@echo off
rem 漫画发布器一键启动（双击入口，转发给 start.ps1）
rem 用法：双击 = 本机启动；带参数如：start.bat -Lan -Port 9000
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
