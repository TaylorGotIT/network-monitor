$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$versionLine = Select-String -Path (Join-Path $root "netmon\__init__.py") -Pattern '__version__ = "([^"]+)"'
$version = $versionLine.Matches[0].Groups[1].Value
$python = Join-Path $root "venv\Scripts\python.exe"

& $python -m PyInstaller `
    --noconfirm --clean --onefile --name NetworkMonitor `
    --icon (Join-Path $root "static\favicon.ico") `
    --add-data ((Join-Path $root "static") + ";static") `
    --distpath (Join-Path $root "dist") `
    --workpath (Join-Path $root "build\pyinstaller") `
    --specpath (Join-Path $root "build") `
    (Join-Path $root "app.py")

$asset = "NetworkMonitor-$version-win64.exe"
Copy-Item (Join-Path $root "dist\NetworkMonitor.exe") (Join-Path $root "dist\$asset") -Force
Write-Host "built: dist\$asset"
