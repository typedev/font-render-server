@echo off
rem Run font-render-server of the current version (installed layout, docs/deployment.md):
rem   "C:\Program Files\FontRender\bin\font-render-server.cmd" dev-mode status
set /p CUR=<"%~dp0..\current"
"%~dp0..\versions\%CUR%\.venv\Scripts\python.exe" -m font_render %*
