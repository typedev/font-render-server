# Install -> use -> repair -> reset-admin -> uninstall --purge on a throwaway Windows machine
# (the CI runner), then check that nothing is left. Never run it on a real server: it
# removes FontRenderService and its data.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_test.ps1 -Dist dist

param([Parameter(Mandatory = $true)][string]$Dist)

# Continue: with Stop, PowerShell 5.1 turns a native command's stderr line into an error
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
$Dist = (Resolve-Path $Dist).Path
$App = "$env:ProgramFiles\FontRender"
$Home_ = "$env:ProgramData\FontRender"
$Base = 'http://127.0.0.1:41800'
$Font = "$env:WINDIR\Fonts\arial.ttf"

function Check($ok, $what) {
    if (-not $ok) { throw "FAILED: $what" }
    Write-Host "ok  $what"
}

function Install-Release {
    $out = & powershell -NoProfile -ExecutionPolicy Bypass -File "$Dist\install.ps1" -From $Dist 2>&1 | Out-String
    $code = $LASTEXITCODE
    Write-Host $out
    Check ($code -eq 0) 'install.ps1 exit code 0'
    return $out
}

function Render($token) {
    $png = Join-Path $env:TEMP 'smoke.png'
    Remove-Item $png -ErrorAction SilentlyContinue
    $code = & curl.exe -s -o $png -w '%{http_code}' -H "Authorization: Bearer $token" `
        -F "fonts=@$Font" -F 'ppem=16' -F 'layout=plain' -F 'text=Hamburgefonstiv' "$Base/render"
    $magic = if (Test-Path $png) { [IO.File]::ReadAllBytes($png)[0..3] -join ',' } else { '' }
    return @{ code = $code; png = ($magic -eq '137,80,78,71') }
}

# --- fresh install ---
$out = Install-Release
Check ($out -match 'Admin token: (\S+)') 'install printed an admin token'
$token = $Matches[1]
$health = Invoke-RestMethod "$Base/health"
Check ($health.status -eq 'ok') "/health ok (release $($health.release), version $($health.version))"
Check ((Get-Service FontRenderService).Status -eq 'Running') 'service running'
$r = Render $token
Check ($r.code -eq '200' -and $r.png) 'render with the admin token returns a PNG'
$r = Render 'wrong'
Check ($r.code -eq '401') 'anonymous rendering is closed (401)'
$info = Invoke-RestMethod "$Base/info" -Headers @{ Authorization = "Bearer $token" }
Check ($info.layout -eq 'installed') '/info layout = installed'
$usersAces = @((Get-Acl $Home_).Access | Where-Object { $_.IdentityReference -match 'Users' }).Count
Check ($usersAces -eq 0) 'data dir not readable by Users'
Check (Test-Path 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\FontRenderService') 'Apps & features entry'
Check ((Get-ItemProperty 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\FontRenderService').Publisher -eq 'Alexander Lubovenko') 'publisher'
Check ($null -ne (Get-NetFirewallRule -DisplayName 'Font Render Service' -ErrorAction SilentlyContinue)) 'firewall rule'

# Like `irm | iex`: the script runs inside the caller's session, which must survive it
function Install-InSession {
    $cmd = "& ([scriptblock]::Create((Get-Content -Raw '$Dist\install.ps1'))) -From '$Dist'; 'SESSION-STILL-ALIVE'"
    $out = & powershell -NoProfile -ExecutionPolicy Bypass -Command $cmd 2>&1 | Out-String
    Write-Host $out
    Check ($out -match 'SESSION-STILL-ALIVE') 'install inside a session leaves the session open'
    return $out
}

# --- repair keeps clients (run in-session, as irm | iex does) ---
$out = Install-InSession
Check ($out -match 'Admin token: unchanged') 'repair keeps the admin token'
Check ((Invoke-RestMethod "$Base/health").status -eq 'ok') '/health ok after repair'
Check ((Render $token).code -eq '200') 'old token still works after repair'

# --- reset-admin ---
$cur = (Get-Content "$App\current" -Raw).Trim()
$out = & "$App\versions\$cur\.venv\Scripts\python.exe" -m font_render reset-admin 2>&1 | Out-String
Check ($out -match '\n\s+(\S{40,})\s*\n') 'reset-admin printed a token'
$newToken = $Matches[1]
Check ((Render $newToken).code -eq '200') 'new token works'
Check ((Render $token).code -eq '401') 'old token rejected'
Check ((Get-Content "$Home_\logs\audit.log" -Raw) -match '"reset-admin"') 'audit log records reset-admin'

# --- developer mode: deploy a tree (the update pipeline: uv sync, Chromium, selfcheck,
#     switch, NSSM restart, launcher, trial), then back to the release ---
function Wait-Version($v) {
    for ($i = 0; $i -lt 120; $i++) {
        Start-Sleep 2
        try { $h = Invoke-RestMethod "$Base/health" -TimeoutSec 5; if ($h.version -eq $v) { return $true } } catch {}
    }
    return $false
}
$code = & curl.exe -s -o NUL -w '%{http_code}' -H "Authorization: Bearer $newToken" "$Base/dev/status"
Check ($code -eq '404') '/dev/status 404 while developer mode is off'
$out = & cmd /c "`"$App\bin\font-render-server.cmd`" dev-mode on --client ci --reason smoke-test --days 1" 2>&1 | Out-String
Write-Host $out
Check ($out -match '\n\s+(\S{40,})\s*\n') 'dev-mode on created a dev client'
$devToken = $Matches[1]
$zip = (Get-ChildItem "$Dist\font-render-*.zip").FullName
$code = & curl.exe -s -o deploy.json -w '%{http_code}' -H "Authorization: Bearer $devToken" `
    -F "archive=@$zip" -F 'commit=abcdef1234567' -F 'dirty=false' "$Base/dev/deploy"
Check ($code -eq '202') "dev deploy accepted ($code)"
Check (Wait-Version 'dev-abcdef1') 'server restarted into dev-abcdef1'
for ($i = 0; $i -lt 60 -and (Test-Path "$App\trial"); $i++) { Start-Sleep 2 }
Check (-not (Test-Path "$App\trial")) 'dev build proven healthy (trial removed)'
Check ((Render $newToken).code -eq '200') 'render on the dev build'
$code = & curl.exe -s -o release.json -w '%{http_code}' -X POST -H "Authorization: Bearer $devToken" "$Base/dev/release"
Check ($code -eq '200') "back to the release ($code)"
Check (Wait-Version $health.version) "server restarted into $($health.version)"
$out = & cmd /c "`"$App\bin\font-render-server.cmd`" dev-mode off" 2>&1 | Out-String
Check ($out -match 'switched off') 'dev-mode off'
$code = & curl.exe -s -o NUL -w '%{http_code}' -H "Authorization: Bearer $devToken" "$Base/dev/status"
Check ($code -eq '404') '/dev/status 404 after dev-mode off'

# --- uninstall --purge ---
& cmd /c "`"$App\bin\uninstall.cmd`" --purge --quiet"
for ($i = 0; $i -lt 90 -and (Test-Path $App); $i++) { Start-Sleep 1 }
Check (-not (Get-Service FontRenderService -ErrorAction SilentlyContinue)) 'service removed'
Check (-not (Get-NetFirewallRule -DisplayName 'Font Render Service' -ErrorAction SilentlyContinue)) 'firewall rule removed'
Check (-not (Test-Path 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\FontRenderService')) 'Apps & features entry removed'
Check (-not (Test-Path "$env:ProgramData\Microsoft\Windows\Start Menu\Programs\Font Render")) 'Start menu folder removed'
if (Test-Path $App) {
    Write-Host 'left in the program folder:'
    Get-ChildItem $App -Recurse -Force | Select-Object -First 30 | ForEach-Object { Write-Host "  $($_.FullName) $($_.Attributes)" }
    Get-Process | Where-Object { $_.Path -like "$App*" } | ForEach-Object { Write-Host "  process $($_.Id) $($_.Path)" }
}
Check (-not (Test-Path $App)) 'program folder removed'
Check (-not (Test-Path $Home_)) 'data folder removed'
# --- port 41800 taken by another program: install moves on to 41801; an explicit busy port stops ---
$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Any, 41800)
$listener.Start()
try {
    $out = Install-Release
    Check ($out -match 'Port 41800 is in use by .*using port 41801') 'busy 41800: install picks 41801 and says why'
    Check ((Invoke-RestMethod 'http://127.0.0.1:41801/health').status -eq 'ok') 'service answers on 41801'
    $out = & powershell -NoProfile -ExecutionPolicy Bypass -File "$Dist\install.ps1" -From $Dist -Port 41800 2>&1 | Out-String
    $code = $LASTEXITCODE
    Write-Host $out
    Check ($code -ne 0 -and $out -match 'port 41800 is in use') 'explicit busy -Port 41800 is refused'
    Check ((Invoke-RestMethod 'http://127.0.0.1:41801/health').status -eq 'ok') 'the installed service is untouched (still 41801)'
} finally {
    $listener.Stop()
}
& cmd /c "`"$App\bin\uninstall.cmd`" --purge --quiet"
for ($i = 0; $i -lt 90 -and (Test-Path $App); $i++) { Start-Sleep 1 }
Check (-not (Test-Path $App) -and -not (Test-Path $Home_)) 'removed again'

Write-Host 'smoke test passed'
