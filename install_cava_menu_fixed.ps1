# Fixed installer for the CavaMenuWidget into YASB's library.zip.
#
# Fixes the two bugs in the original install_cava_menu.ps1:
#  1. YASB ships as a FROZEN app whose library.zip contains only compiled
#     .pyc bytecode (Python 3.14, magic 0x0e2b). Writing a plain .py file
#     into the zip does not work - importlib can't find it (hence the
#     "ModuleNotFoundError: No module named 'core.widgets.yasb.cava_menu'"
#     in yasb.log). We must place a compiled .pyc at that path.
#  2. library.zip lives under C:\Program Files\YASB\lib which needs
#     elevation to write. This script auto-elevates itself via a scheduled
#     task (the current account must be a member of Administrators).
#  3. YASB updates overwrite library.zip - re-run after every update.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File install_cava_menu_fixed.ps1
#   powershell -ExecutionPolicy Bypass -File install_cava_menu_fixed.ps1 -Restore

param(
    [switch]$Restore,
    [string]$Python314Dir = "$PSScriptRoot\.py314",
    [string]$InstallDir = "",
    [switch]$NoElevate
)

$ErrorActionPreference = "Stop"

# Tee output to a log so the elevated run can be inspected.
$LogFile = Join-Path $PSScriptRoot "install_cava_menu.log"
Start-Transcript -Path $LogFile -Append -ErrorAction SilentlyContinue | Out-Null

# ---------------------------------------------------------------------------
# Auto-elevation: rerun this script elevated via a scheduled task.
# ---------------------------------------------------------------------------
function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdmin) -and -not $NoElevate) {
    Write-Host "Not elevated. Launching UAC prompt to re-run as administrator..."
    Stop-Transcript -ErrorAction SilentlyContinue
    $argList = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    if ($Restore) { $argList += " -Restore" }
    if ($InstallDir) { $argList += " -InstallDir `"$InstallDir`"" }
    try {
        $p = Start-Process -FilePath "powershell.exe" -ArgumentList $argList -Verb RunAs -PassThru -Wait
        Write-Host "Elevated installer finished (exit: $($p.ExitCode))."
    } catch {
        Write-Host "Could not elevate: $($_.Exception.Message)"
        Write-Host "Please right-click the script and choose 'Run with PowerShell' as Administrator,"
        Write-Host "or run from an elevated (Administrator) PowerShell window:"
        Write-Host "  powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    }
    exit 0
}

# ---------------------------------------------------------------------------
# Locate YASB install directory.
# ---------------------------------------------------------------------------
if (-not $InstallDir) {
    $proc = Get-Process -Name yasb -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($proc -and $proc.Path -and (Test-Path -LiteralPath $proc.Path)) {
        $InstallDir = Split-Path -Path $proc.Path -Parent
    }
}
if (-not $InstallDir) {
    $cmd = Get-Command yasbc.exe -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) { $InstallDir = Split-Path -Path $cmd.Source -Parent }
}
if (-not $InstallDir) { $InstallDir = "C:\Program Files\YASB" }
Write-Host "YASB install directory: $InstallDir"

$zipPath = Join-Path $InstallDir "lib\library.zip"
if (-not (Test-Path -LiteralPath $zipPath)) {
    Write-Error "library.zip not found at $zipPath"
    exit 1
}
$backupPath = "$zipPath.bak"
$entryPyc = "core/widgets/yasb/cava_menu.pyc"
$srcPath = Join-Path $PSScriptRoot "cava_menu.py"
$yasbExe = Join-Path $InstallDir "yasb.exe"
$yasbcExe = Join-Path $InstallDir "yasbc.exe"

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

# ---------------------------------------------------------------------------
# Stop YASB (the zip is locked while it runs).
# ---------------------------------------------------------------------------
function Test-ZipLocked {
    try {
        # Probe for FULL write access - a read-only probe can succeed even when
        # YASB holds a handle that would deny an update to the archive.
        $fs = [System.IO.File]::Open($zipPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
        $fs.Close()
        return $false
    } catch { return $true }
}

function Stop-Yasb {
    Write-Host "Stopping YASB..."
    try { & $yasbcExe stop 2>$null | Out-Null } catch {}
    Start-Sleep -Seconds 1
    $p = Get-Process -Name yasb -ErrorAction SilentlyContinue
    if ($p) { Stop-Process -Name yasb -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 1 }
    for ($i = 0; $i -lt 12; $i++) {
        if (-not (Get-Process -Name yasb -ErrorAction SilentlyContinue) -and -not (Test-ZipLocked)) { return $true }
        Start-Sleep -Milliseconds 500
    }
    Write-Host "Could not clear the library.zip lock. Quit YASB from the tray and re-run."
    return $false
}

# ---------------------------------------------------------------------------
# Compile cava_menu.py -> cava_menu.pyc using a real Python 3.14 interpreter.
# ---------------------------------------------------------------------------
function Compile-Pyc {
    $py = Join-Path $Python314Dir "python.exe"
    if (-not (Test-Path -LiteralPath $py)) {
        Write-Error "Python 3.14 embeddable not found at $py. Set -Python314Dir or copy the python-3.14.x-embed-amd64 package into $Python314Dir"
        exit 1
    }
    Write-Host "Compiling $srcPath with $py ..."
    $builder = Join-Path $Python314Dir "build_pyc.py"
    if (-not (Test-Path -LiteralPath $builder)) {
        Write-Error "build_pyc.py helper missing at $builder"
        exit 1
    }
    $env:CAVA_SRC = $srcPath
    & $py $builder | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Error "Compilation failed"; exit 1 }
    return (Join-Path $Python314Dir "cava_menu.pyc")
}

# ---------------------------------------------------------------------------
# Restore the shipped archive from backup.
# ---------------------------------------------------------------------------
if ($Restore) {
    if (-not (Test-Path -LiteralPath $backupPath)) {
        Write-Error "Backup not found at $backupPath"
        exit 1
    }
    if (Test-ZipLocked) { if (-not (Stop-Yasb)) { exit 1 } }
    Copy-Item -LiteralPath $backupPath -Destination $zipPath -Force
    Write-Host "Restored original library.zip"
    exit 0
}

# --- Validate prerequisites ---
if (-not (Test-Path -LiteralPath $srcPath)) {
    Write-Error "cava_menu.py not found next to this script"
    exit 1
}

$pycPath = Compile-Pyc

# --- Back up the current archive once (only if it does not already contain our entry) ---
$isInstalled = $false
try {
    $probe = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
    $isInstalled = $null -ne $probe.GetEntry($entryPyc)
    $probe.Dispose()
} catch { $isInstalled = $false }

if (-not $isInstalled -and -not (Test-Path -LiteralPath $backupPath)) {
    Copy-Item -LiteralPath $zipPath -Destination $backupPath -Force
    Write-Host "Backed up library.zip -> library.zip.bak"
} elseif (-not $isInstalled -and (Test-Path -LiteralPath $backupPath)) {
    Write-Host "Using existing library.zip.bak backup"
} else {
    Write-Host "cava_menu.pyc already installed - replacing it with fresh build."
}

# --- Patch the archive ---
$running = Get-Process -Name yasb -ErrorAction SilentlyContinue
if ($running) {
    Write-Host "YASB is running - stopping it to unlock library.zip."
    if (-not (Stop-Yasb)) { exit 1 }
} elseif (Test-ZipLocked) {
    Write-Host "library.zip is locked by another process."
    if (-not (Stop-Yasb)) { exit 1 }
}
# Retry the read-write open (the OS may take a moment to release the handle).
$fs = $null
for ($i = 0; $i -lt 10; $i++) {
    try {
        $fs = [System.IO.File]::Open($zipPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite)
        break
    } catch {
        Start-Sleep -Milliseconds 500
    }
}
if ($null -eq $fs) {
    Write-Error "Cannot open library.zip for writing - is YASB still running?"
    exit 1
}
$zip = New-Object System.IO.Compression.ZipArchive($fs, [System.IO.Compression.ZipArchiveMode]::Update)
try {
    $existing = $zip.GetEntry($entryPyc)
    if ($existing) { $existing.Delete() }
    # Also remove any stray plain-.py entry the old buggy script may have added
    $oldPy = $zip.GetEntry("core/widgets/yasb/cava_menu.py")
    if ($oldPy) { $oldPy.Delete() }
    $entry = $zip.CreateEntry($entryPyc, [System.IO.Compression.CompressionLevel]::Optimal)
    $writer = New-Object System.IO.BinaryWriter($entry.Open())
    $writer.Write([System.IO.File]::ReadAllBytes($pycPath))
    $writer.Close()
    Write-Host "Installed $entryPyc into library.zip"
} finally {
    $zip.Dispose()
    $fs.Close()
}

# --- Verify ---
$verify = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
$ok = $null -ne $verify.GetEntry($entryPyc)
$verify.Dispose()
if (-not $ok) {
    Write-Error "Verification failed - $entryPyc not found in archive"
    exit 1
}
Write-Host "Verified: $entryPyc present in archive."

# --- Restart YASB ---
Write-Host ""
Write-Host "Done. Restarting YASB..."
try { & $yasbcExe start 2>$null | Out-Null } catch {}
Start-Sleep -Seconds 2
if (-not (Get-Process -Name yasb -ErrorAction SilentlyContinue)) {
    Write-Host "YASB did not auto-start. Start it from the Start Menu or run: yasbc start"
} else {
    Write-Host "YASB is running."
}
Write-Host ""
Write-Host "NOTE: YASB updates overwrite library.zip - re-run this script after every update."
