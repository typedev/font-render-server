@echo off
setlocal EnableDelayedExpansion

:: =========================================================================
:: setup_service.bat — Full setup of Font Render Service on Windows
::
:: What it does:
::   1. Checks prerequisites (git, uv)
::   2. Installs Python dependencies (uv sync)
::   3. Installs Playwright browsers (chromium)
::   4. Downloads NSSM (service manager)
::   5. Reads FONT_RENDER_TOKEN from .env
::   6. Registers FontRenderService as a Windows service
::
:: Usage: Right-click -> Run as administrator
::        (re-running it reinstalls the service with the current settings)
:: =========================================================================

:: Check administrator privileges
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo ============================================
    echo  ERROR: Administrator privileges required!
    echo  Right-click and select "Run as administrator"
    echo ============================================
    pause
    exit /b 1
)

:: Configuration
set SERVICE_NAME=FontRenderService
:: Project root: this script lives in deploy\windows\
for %%i in ("%~dp0..\..") do set "SERVICE_DIR=%%~fi"
set NSSM_DIR=%SERVICE_DIR%\tools
set NSSM_EXE=%NSSM_DIR%\nssm.exe
set NSSM_URL=https://nssm.cc/release/nssm-2.24.zip
set NSSM_URL_MIRROR=https://chocolatey.org/api/v2/package/NSSM/2.24.101.20180116
set NSSM_ZIP=%NSSM_DIR%\nssm.zip
set LOG_DIR=%SERVICE_DIR%\logs
set ENV_FILE=%SERVICE_DIR%\.env

echo.
echo ============================================
echo  Font Render Service -- Full Setup
echo ============================================
echo  Service dir: %SERVICE_DIR%
echo.

:: -----------------------------------------------------------------
:: Step 1: Check prerequisites
:: -----------------------------------------------------------------
echo [1/6] Checking prerequisites...

where git >nul 2>&1
if %errorlevel% neq 0 (
    echo  ERROR: git not found in PATH.
    echo  Install from https://git-scm.com/download/win
    pause
    exit /b 1
)
echo  git ............. OK

where uv >nul 2>&1
if %errorlevel% neq 0 (
    echo  ERROR: uv not found in PATH.
    echo  Install: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
    pause
    exit /b 1
)
for /f "tokens=*" %%i in ('where uv') do set UV_PATH=%%i
echo  uv .............. OK (%UV_PATH%)

echo.

:: -----------------------------------------------------------------
:: Step 2: Install Python dependencies
:: -----------------------------------------------------------------
echo [2/6] Installing Python dependencies (uv sync)...
pushd "%SERVICE_DIR%"
uv sync
if %errorlevel% neq 0 (
    echo  ERROR: uv sync failed.
    pause
    exit /b 1
)
popd
echo  Dependencies .... OK
echo.

:: -----------------------------------------------------------------
:: Step 3: Install Playwright browsers
:: -----------------------------------------------------------------
echo [3/6] Installing Playwright browsers...
pushd "%SERVICE_DIR%"
uv run playwright install chromium
if %errorlevel% neq 0 (
    echo  ERROR: Playwright install failed.
    pause
    exit /b 1
)
popd
echo  Chromium ........ OK
echo.

:: -----------------------------------------------------------------
:: Step 4: Download NSSM
:: -----------------------------------------------------------------
echo [4/6] Setting up NSSM (service manager)...

if not exist "%NSSM_DIR%" mkdir "%NSSM_DIR%"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

if not exist "%NSSM_EXE%" (
    :: Try winget first (built into Windows 10/11)
    where winget >nul 2>&1
    if !errorlevel! equ 0 (
        echo  Trying winget...
        winget install --id NSSM.NSSM --accept-source-agreements --accept-package-agreements >nul 2>&1
        where nssm >nul 2>&1
        if !errorlevel! equ 0 (
            for /f "tokens=*" %%i in ('where nssm') do copy "%%i" "%NSSM_EXE%" >nul
        )
    )

    if not exist "%NSSM_EXE%" (
        echo  Downloading NSSM from nssm.cc...
        powershell -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%NSSM_URL%' -OutFile '%NSSM_ZIP%' -TimeoutSec 15" 2>nul

        if not exist "%NSSM_ZIP%" (
            echo  nssm.cc unavailable, trying mirror...
            powershell -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%NSSM_URL_MIRROR%' -OutFile '%NSSM_ZIP%' -TimeoutSec 15" 2>nul
        )

        if not exist "%NSSM_ZIP%" (
            echo.
            echo  ERROR: Could not download NSSM from any source.
            echo  Download manually from https://nssm.cc/download
            echo  and place nssm.exe into: %NSSM_DIR%\
            pause
            exit /b 1
        )

        echo  Extracting...
        powershell -Command "Expand-Archive -Path '%NSSM_ZIP%' -DestinationPath '%NSSM_DIR%\tmp' -Force"
        :: Find nssm.exe in extracted files (handles different archive structures)
        for /r "%NSSM_DIR%\tmp" %%f in (nssm.exe) do (
            if "%%~nxf"=="nssm.exe" (
                :: Prefer 64-bit version
                echo "%%~dpf" | findstr /i "win64 x64" >nul && copy "%%f" "%NSSM_EXE%" >nul
            )
        )
        :: Fallback: if no 64-bit found, take any nssm.exe
        if not exist "%NSSM_EXE%" (
            for /r "%NSSM_DIR%\tmp" %%f in (nssm.exe) do copy "%%f" "%NSSM_EXE%" >nul
        )
        rd /s /q "%NSSM_DIR%\tmp" 2>nul
        del "%NSSM_ZIP%" 2>nul
    )

    if exist "%NSSM_EXE%" (
        echo  NSSM ........... OK
    ) else (
        echo  ERROR: NSSM installation failed.
        pause
        exit /b 1
    )
) else (
    echo  NSSM ........... OK (already present)
)
echo.

:: -----------------------------------------------------------------
:: Step 5: Read token from .env
:: -----------------------------------------------------------------
echo [5/6] Reading configuration...

set RENDER_TOKEN=
if exist "%ENV_FILE%" (
    for /f "usebackq tokens=1,* delims==" %%a in ("%ENV_FILE%") do (
        set "_key=%%a"
        :: Skip comments
        if not "!_key:~0,1!"=="#" (
            if "%%a"=="FONT_RENDER_TOKEN" set RENDER_TOKEN=%%b
        )
    )
)

if "!RENDER_TOKEN!"=="" (
    echo  No FONT_RENDER_TOKEN found in .env
    echo.
    set /p RENDER_TOKEN="  Enter token (or press Enter to skip): "
)

if "!RENDER_TOKEN!"=="" (
    echo  WARNING: No token set. /update and /rollback will be disabled.
) else (
    echo  Token .......... OK
)
echo.

:: -----------------------------------------------------------------
:: Step 6: Install Windows service
:: -----------------------------------------------------------------
echo [6/6] Installing Windows service...

:: Stop and remove old service if exists
"%NSSM_EXE%" status %SERVICE_NAME% >nul 2>&1
if %errorlevel% equ 0 (
    echo  Stopping old service...
    "%NSSM_EXE%" stop %SERVICE_NAME% >nul 2>&1
    timeout /t 3 /nobreak >nul
    "%NSSM_EXE%" remove %SERVICE_NAME% confirm >nul
    echo  Old service removed.
)

:: Install
"%NSSM_EXE%" install %SERVICE_NAME% "%UV_PATH%" run font-render-server
"%NSSM_EXE%" set %SERVICE_NAME% AppDirectory "%SERVICE_DIR%"

:: Auto-restart on exit with 5 second delay
"%NSSM_EXE%" set %SERVICE_NAME% AppExit Default Restart
"%NSSM_EXE%" set %SERVICE_NAME% AppRestartDelay 5000

:: Logging with rotation (5 MB)
"%NSSM_EXE%" set %SERVICE_NAME% AppStdout "%LOG_DIR%\service.log"
"%NSSM_EXE%" set %SERVICE_NAME% AppStderr "%LOG_DIR%\service.log"
"%NSSM_EXE%" set %SERVICE_NAME% AppStdoutCreationDisposition 4
"%NSSM_EXE%" set %SERVICE_NAME% AppStderrCreationDisposition 4
"%NSSM_EXE%" set %SERVICE_NAME% AppRotateFiles 1
"%NSSM_EXE%" set %SERVICE_NAME% AppRotateBytes 5242880

:: Set token as environment variable for the service
if "!RENDER_TOKEN!" neq "" (
    "%NSSM_EXE%" set %SERVICE_NAME% AppEnvironmentExtra FONT_RENDER_TOKEN=!RENDER_TOKEN!
)

:: Start
echo  Starting service...
"%NSSM_EXE%" start %SERVICE_NAME%
timeout /t 3 /nobreak >nul

:: Verify
"%NSSM_EXE%" status %SERVICE_NAME% | findstr /i "running" >nul 2>&1
if %errorlevel% equ 0 (
    echo  Service ........ RUNNING
) else (
    echo  WARNING: Service may not have started. Check logs:
    echo    %LOG_DIR%\service.log
)

echo.
echo ============================================
echo  Setup complete!
echo ============================================
echo.
echo  Service name:  %SERVICE_NAME%
echo  Logs:          %LOG_DIR%\service.log
echo  Config:        %ENV_FILE%
echo.
echo  Commands:
echo    nssm status %SERVICE_NAME%
echo    nssm stop %SERVICE_NAME%
echo    nssm restart %SERVICE_NAME%
echo    nssm remove %SERVICE_NAME% confirm
echo.
echo  To update remotely (from Linux):
echo    uv run font-render --update --server http://THIS_PC:8000
echo.
pause
