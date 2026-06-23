# Build AmberPriceTray.exe and the installer.
# Usage:  .\build.ps1   (run from the repo root)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

# 1. Create/refresh a build venv with runtime + build deps.
# Use Python 3.11: PyInstaller doesn't yet bundle tkinter correctly under 3.14.
$venv = Join-Path $root ".venv"
if (-not (Test-Path $venv)) { py -3.11 -m venv $venv }
$py = Join-Path $venv "Scripts\python.exe"
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -r (Join-Path $root "requirements.txt") pyinstaller

# 2. Point Tcl/Tk at Python's real data dirs so PyInstaller bundles tkinter.
# (A venv has no tcl/ folder, and a stray system TCL_LIBRARY can break detection.)
$basePrefix = (& $py -c "import sys; print(sys.base_prefix)").Trim()
$env:TCL_LIBRARY = Join-Path $basePrefix "tcl\tcl8.6"
$env:TK_LIBRARY  = Join-Path $basePrefix "tcl\tk8.6"

# 3. Regenerate the icon.
& $py (Join-Path $root "make_ico.py")

# 4. Build the one-file windowed exe with version metadata.
& $py -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name AmberPriceTray `
    --icon       (Join-Path $root "assets\amber.ico") `
    --version-file (Join-Path $root "version_info.txt") `
    --add-data   ((Join-Path $root "assets\amber.ico") + ";.") `
    --distpath   (Join-Path $root "dist") `
    --workpath   (Join-Path $root "build") `
    --specpath   $root `
    (Join-Path $root "amber_price_tray.py")

# 5. Compile the installer.
$iscc = Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) { $iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" }
& $iscc (Join-Path $root "installer.iss")

Write-Host "`nDone. Installer is in installer\Output\" -ForegroundColor Green
