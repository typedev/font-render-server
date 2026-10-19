@echo off
rem Issue a new token for the admin client (Start menu: Font Render > Reset admin token).
rem Installed layout only: APP\bin\reset-admin.cmd, see docs/distribution.md.
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
set /p CUR=<"%~dp0..\current"
"%~dp0..\versions\%CUR%\.venv\Scripts\python.exe" -m font_render reset-admin %*
echo.
pause
