# Build AmberPriceTray.exe and the installer.
# Usage:  .\build.ps1                    (run from the repo root)
#         .\build.ps1 -Python python     (use a specific interpreter, e.g. in CI)
#         .\build.ps1 -SkipTests
param(
    [string]$Python = "",
    [switch]$SkipTests
)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

# Native commands don't trip $ErrorActionPreference, so check exit codes.
# (A plain function, so flags like -m pass straight through in $args.)
function Invoke-Checked {
    $exe = $args[0]
    # Keep $rest an array even with one argument, or splatting a lone string
    # passes it one character at a time.
    $rest = @(if ($args.Count -gt 1) { $args[1..($args.Count - 1)] })
    & $exe @rest
    if ($LASTEXITCODE -ne 0) { throw "'$exe $rest' failed with exit code $LASTEXITCODE" }
}

# 1. Create/refresh a build venv with pinned runtime + build deps.
# Use Python 3.11: PyInstaller doesn't yet bundle tkinter correctly under 3.14.
$venv = Join-Path $root ".venv"
if (-not (Test-Path $venv)) {
    if ($Python) { Invoke-Checked $Python -m venv $venv }
    else { Invoke-Checked py -3.11 -m venv $venv }
}
$py = Join-Path $venv "Scripts\python.exe"
Invoke-Checked $py -m pip install --quiet --upgrade pip
Invoke-Checked $py -m pip install --quiet -r (Join-Path $root "requirements-dev.txt")

# 2. Run the unit tests.
if (-not $SkipTests) {
    Push-Location $root
    try { Invoke-Checked $py -m pytest -q } finally { Pop-Location }
}

# 3. Version: APP_VERSION in amber_core.py is the single source of truth.
$versionFile = Join-Path $root "build\version_info.txt"
New-Item -ItemType Directory -Force (Join-Path $root "build") | Out-Null
$version = (& $py (Join-Path $root "tools\make_version_info.py") $versionFile).Trim()
if ($LASTEXITCODE -ne 0) { throw "make_version_info.py failed" }
Write-Host "Building version $version"

# 4. Point Tcl/Tk at Python's real data dirs so PyInstaller bundles tkinter.
# (A venv has no tcl/ folder, and a stray system TCL_LIBRARY can break detection.)
$basePrefix = (& $py -c "import sys; print(sys.base_prefix)").Trim()
$env:TCL_LIBRARY = Join-Path $basePrefix "tcl\tcl8.6"
$env:TK_LIBRARY  = Join-Path $basePrefix "tcl\tk8.6"

# 5. Regenerate the .ico from assets\amber.png.
Invoke-Checked $py (Join-Path $root "tools\make_ico.py")

# 6. Build the one-file windowed exe with version metadata.
Invoke-Checked $py -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name AmberPriceTray `
    --icon       (Join-Path $root "assets\amber.ico") `
    --version-file $versionFile `
    --add-data   ((Join-Path $root "assets\amber.ico") + ";.") `
    --distpath   (Join-Path $root "dist") `
    --workpath   (Join-Path $root "build") `
    --specpath   (Join-Path $root "build") `
    (Join-Path $root "amber_price_tray.py")

# 7. Compile the installer.
$iscc = Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) { $iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" }
if (-not (Test-Path $iscc)) { throw "Inno Setup 6 (ISCC.exe) not found" }
Invoke-Checked $iscc "/DAppVersion=$version" (Join-Path $root "installer.iss")

Write-Host "`nDone. Installer is in installer\Output\AmberPriceTray-Setup-$version.exe" -ForegroundColor Green
