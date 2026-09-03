@echo off
rem 漫画发布器 · 浏览器前端（Web UI）一键启动（双击入口，转发给 start-web.ps1）
rem 带参数如：start.bat -Lan -Port 9000
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-web.ps1" %*
