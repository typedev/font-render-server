@echo off
rem Remove Font Render Service (Apps & features, Start menu). Options:
rem   --purge       also delete data (client tokens, settings, statistics, logs)
rem   --keep-data   keep data without asking;  --quiet  no questions
rem Installed layout only: APP\bin\uninstall.cmd, see docs/distribution.md.
net session >nul 2>&1
if errorlevel 1 (
    if "%~1"=="" (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    ) else (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
    )
    exit /b
)
rem Leave the program folder (a Start menu shortcut starts here), so it can be removed
for %%I in ("%~dp0..") do set "APPDIR=%%~fI"
cd /d "%TEMP%"
set /p CUR=<"%APPDIR%\current"
set PAUSE=pause
echo %* | findstr /i /c:"--quiet" >nul && set PAUSE=rem
rem One line, parsed before it runs: uninstall removes everything but the interpreter it
rem runs on; when it has exited and succeeded, the rest of the folder goes (retried once: rd may report success while a file is still busy)
"%APPDIR%\versions\%CUR%\.venv\Scripts\python.exe" -m font_render uninstall %* && (attrib -r "%APPDIR%\*" /s /d >nul 2>&1 & rd /s /q "%APPDIR%" 2>nul & if exist "%APPDIR%" (ping -n 3 127.0.0.1 >nul & rd /s /q "%APPDIR%" 2>nul)) & %PAUSE% & exit /b
