# Font Render Service installer (docs/distribution.md).
#
#   irm https://github.com/typedev/font-render-server/releases/latest/download/install.ps1 | iex
#
# With options:
#   & ([scriptblock]::Create((irm https://github.com/typedev/font-render-server/releases/latest/download/install.ps1))) -Port 8080
#
# Installs (or repairs) the release into C:\Program Files\FontRender, data in
# C:\ProgramData\FontRender, and registers the FontRenderService Windows service. Running it
# again repairs the installation and keeps client tokens, settings and statistics.
# Everything after unpacking is done by `python -m font_render install`.

param(
    [string]$Version = '',                    # default: the release this script came from, else the latest
    [int]$Port = 0,                           # 0: keep the installed port, else 41800 (or the next free one)
    [switch]$NoFirewall,
    [ValidateSet('stable', 'beta')][string]$Channel = 'stable',
    [string]$InstallDir = "$env:ProgramFiles\FontRender",
    [string]$DataDir = "$env:ProgramData\FontRender",
    [string]$From = '',                       # folder with release assets instead of GitHub (CI, offline tests)
    [switch]$Force,                           # allow installing an older version
    [string]$ImportFrom = '',                 # checkout install whose data (clients, settings, stats) moves over
    [switch]$Relaunched                       # internal: set when the script restarted itself elevated
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'     # Invoke-WebRequest is many times faster without the progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Repo = 'typedev/font-render-server'
$ReleaseVersion = ''                          # set by the release workflow
$UvVersion = '0.12.21'
$UvSha256 = '5d223efa0bf00208c3853246af09420419dfbd352536aa6bb8163d6170e23890'
$PythonVersion = '3.12'
$Service = 'FontRenderService'

# Run as a file (-File, CI, the elevated relaunch) the script may `exit`; run with
# `irm | iex` or as a script block it runs inside the user's session, where `exit` would
# close their PowerShell window: there it returns or throws instead.
$AsFile = [bool]$PSCommandPath

function Say($text) { Write-Host $text }
function Fail($text) {
    Write-Host "ERROR: $text" -ForegroundColor Red
    if ($Relaunched) { Read-Host 'Press Enter to close' | Out-Null }
    if ($AsFile) { exit 1 }
    throw "Font Render Service was not installed: $text"
}

# --- Elevation ------------------------------------------------------------------------------
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Say 'Administrator rights are needed; asking for them...'
    if ($PSCommandPath) {
        $self = $PSCommandPath
    } else {
        # Piped into iex there is no file to relaunch: fetch this release's script again
        $self = Join-Path $env:TEMP 'font-render-install.ps1'
        $v = if ($Version) { $Version } else { $ReleaseVersion }
        $url = if ($v) { "https://github.com/$Repo/releases/download/v$v/install.ps1" }
               else { "https://github.com/$Repo/releases/latest/download/install.ps1" }
        Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $self
    }
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$self`"", '-Relaunched')
    foreach ($p in $PSBoundParameters.GetEnumerator()) {
        if ($p.Value -is [switch]) { if ($p.Value) { $argList += "-$($p.Key)" } }
        else { $argList += "-$($p.Key)"; $argList += "`"$($p.Value)`"" }
    }
    $proc = Start-Process powershell -Verb RunAs -ArgumentList $argList -Wait -PassThru
    if ($AsFile) { exit $proc.ExitCode }
    return
}

# --- Checks ---------------------------------------------------------------------------------
if (-not [Environment]::Is64BitOperatingSystem) { Fail 'a 64-bit Windows is required' }
if ([Environment]::OSVersion.Version.Build -lt 17763) { Fail 'Windows 10 1809 or newer is required' }

function Fetch($name, $dest) {
    if ($From) { Copy-Item -LiteralPath (Join-Path $From $name) -Destination $dest -Force }
    else { Invoke-WebRequest -UseBasicParsing -Uri "$script:BaseUrl/$name" -OutFile $dest }
}

function Hash-Of($path) { (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLower() }

function Version-Key($v) {
    # 1.3.0-beta.1 -> [version]1.3.0; $null if not a version
    $core = ($v -split '-')[0]
    try { return [version]$core } catch { return $null }
}

# --- Which release ----------------------------------------------------------------------------
if (-not $Version) { $Version = $ReleaseVersion }
if (-not $Version -and -not $From) {
    $latest = Invoke-RestMethod -UseBasicParsing -Uri "https://api.github.com/repos/$Repo/releases/latest"
    $Version = $latest.tag_name -replace '^v', ''
}
if (-not $Version) { Fail 'pass -Version' }
$script:BaseUrl = "https://github.com/$Repo/releases/download/v$Version"

$currentFile = Join-Path $InstallDir 'current'
if ((Test-Path $currentFile) -and -not $Force) {
    $current = (Get-Content $currentFile -Raw).Trim()
    $a = Version-Key $Version; $b = Version-Key $current
    if ($a -and $b -and $a -lt $b) { Fail "$current is installed; installing the older $Version needs -Force (or use rollback in the web admin)" }
}

Say "Font Render Service $Version"
$bin = Join-Path $InstallDir 'bin'
$versions = Join-Path $InstallDir 'versions'
$staging = Join-Path $InstallDir 'staging'
New-Item -ItemType Directory -Force -Path $bin, $versions, $staging | Out-Null

# --- Download and verify the release ----------------------------------------------------------
$zipName = "font-render-$Version.zip"
$zip = Join-Path $staging $zipName
$sums = Join-Path $staging 'SHA256SUMS'
Say "Downloading $zipName..."
Fetch 'SHA256SUMS' $sums
Fetch $zipName $zip
$expected = (Get-Content $sums | Where-Object { $_ -match "\s\*?$([regex]::Escape($zipName))$" } | ForEach-Object { ($_ -split '\s+')[0] }) | Select-Object -First 1
if (-not $expected) { Fail "$zipName is not listed in SHA256SUMS" }
if ((Hash-Of $zip) -ne $expected.ToLower()) { Fail "$zipName does not match SHA256SUMS (download damaged?)" }

# --- uv (pinned, private copy) ----------------------------------------------------------------
$uv = Join-Path $bin 'uv.exe'
$haveUv = (Test-Path $uv) -and ((& $uv --version) -match [regex]::Escape($UvVersion))
if (-not $haveUv) {
    Say "Downloading uv $UvVersion..."
    $uvZip = Join-Path $staging 'uv.zip'
    Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip" -OutFile $uvZip
    if ((Hash-Of $uvZip) -ne $UvSha256) { Fail 'uv download does not match its pinned SHA256' }
    $uvDir = Join-Path $staging 'uv'
    if (Test-Path $uvDir) { Remove-Item -Recurse -Force $uvDir }
    Expand-Archive -LiteralPath $uvZip -DestinationPath $uvDir
    Copy-Item (Join-Path $uvDir 'uv.exe') $uv -Force
}

# uv keeps Python and its cache inside the program folder; nothing goes to the user profile
$env:UV_PYTHON_INSTALL_DIR = Join-Path $InstallDir 'python'
$env:UV_CACHE_DIR = Join-Path $InstallDir 'cache\uv'
$env:UV_PYTHON_PREFERENCE = 'only-managed'
$env:UV_NO_CONFIG = '1'
Say "Installing Python $PythonVersion..."
& $uv python install $PythonVersion --quiet
if ($LASTEXITCODE -ne 0) { Fail 'uv python install failed' }

# --- Unpack and set up this version -----------------------------------------------------------
$target = Join-Path $versions $Version
$partial = "$target.partial"
if (Test-Path $partial) { Remove-Item -Recurse -Force $partial }
Expand-Archive -LiteralPath $zip -DestinationPath $partial

# The project is installed editable (config finds APP and HOME from its own path), so the
# venv is created in its final folder. Repairing the running version needs the service
# stopped; `install` below starts it again, a failure here restarts the old state.
$svc = Get-Service -Name $Service -ErrorAction SilentlyContinue
$wasRunning = $svc -and $svc.Status -eq 'Running'
$old = "$target.old"
if (Test-Path $old) { Remove-Item -Recurse -Force $old }
if (Test-Path $target) {
    if ($svc) { Stop-Service -Name $Service -Force -ErrorAction SilentlyContinue }
    Rename-Item -LiteralPath $target -NewName (Split-Path $old -Leaf)
}
Rename-Item -LiteralPath $partial -NewName (Split-Path $target -Leaf)
Say 'Installing dependencies...'
Push-Location $target
& $uv sync --frozen --no-dev --python $PythonVersion --quiet
$syncCode = $LASTEXITCODE
Pop-Location
if ($syncCode -ne 0) {
    Remove-Item -Recurse -Force $target -ErrorAction SilentlyContinue
    if (Test-Path $old) { Rename-Item -LiteralPath $old -NewName (Split-Path $target -Leaf) }
    if ($wasRunning) { Start-Service -Name $Service -ErrorAction SilentlyContinue }
    Fail 'uv sync failed'
}
if (Test-Path $old) { Remove-Item -Recurse -Force $old -ErrorAction SilentlyContinue }
Remove-Item -Recurse -Force $staging -ErrorAction SilentlyContinue

# --- Service, firewall, first admin, shortcuts, Apps & features -------------------------------
$python = Join-Path $target '.venv\Scripts\python.exe'
$installArgs = @('-m', 'font_render', 'install', '--app', $InstallDir, '--home', $DataDir,
                 '--channel', $Channel)
if ($Port) { $installArgs += @('--port', $Port) }
if ($NoFirewall) { $installArgs += '--no-firewall' }
if ($ImportFrom) { $installArgs += @('--import-from', $ImportFrom) }
& $python @installArgs
$code = $LASTEXITCODE
# A refused repair (e.g. a busy -Port) changed nothing: bring the stopped service back
if ($code -ne 0 -and $wasRunning -and (Get-Service -Name $Service -ErrorAction SilentlyContinue)) {
    Start-Service -Name $Service -ErrorAction SilentlyContinue
}
if ($Relaunched) { Read-Host 'Press Enter to close' | Out-Null }
if ($AsFile) { exit $code }
if ($code -ne 0) { throw "Font Render Service install failed (exit code $code), see the messages above" }
