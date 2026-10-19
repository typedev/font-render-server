@echo off
cd /d "%~dp0..\.."
echo Font Render Service
echo ====================
echo Press Ctrl+C to stop completely.
echo.

:loop
echo Starting server...
uv run font-render-server
echo.
echo Server exited. Restarting in 3 seconds...
timeout /t 3 /nobreak >nul
goto loop
