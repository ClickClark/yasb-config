# Installs the custom CavaMenuWidget into YASB's library.zip
# Run this script AS ADMINISTRATOR:
#   powershell -ExecutionPolicy Bypass -File "C:\Users\clark\.config\yasb\install_cava_menu.ps1"
#
# The script automatically stops YASB if it is running (library.zip is locked
# by it), patches the archive, verifies the entry, and restarts YASB.
#
# Restore the original archive with:
#   powershell -ExecutionPolicy Bypass -File "C:\Users\clark\.config\yasb\install_cava_menu.ps1" -Restore

param([switch]$Restore)

$ErrorActionPreference = "Stop"

# Auto-detect the YASB install directory (works for Program Files, Scoop, etc.)
$installDir = $null
$proc = Get-Process -Name yasb -ErrorAction SilentlyContinue | Select-Object -First 1
if ($proc -and $proc.Path -and (Test-Path -LiteralPath $proc.Path)) {
    $installDir = Split-Path -Path $proc.Path -Parent
} elseif (-not $installDir) {
    $cmd = Get-Command yasbc.exe -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -and $cmd.Source -match "[\\/]YASB[\\/]|yasb[\\/]\d") {
        $installDir = Split-Path -Path $cmd.Source -Parent
    }
}
if (-not $installDir) { $installDir = "C:\Program Files\YASB" }
if (-not (Test-Path -LiteralPath (Join-Path $installDir "lib\library.zip"))) {
    Write-Error "Could not locate library.zip - edit the installDir detection or pass it manually."
    exit 1
}
Write-Host "YASB install directory: $installDir"

$zipPath = Join-Path $installDir "lib\library.zip"
$entryName = "core/widgets/yasb/cava_menu.py"
$srcPath = Join-Path $PSScriptRoot "cava_menu.py"
$backupPath = "$zipPath.bak"
$yasbExe = Join-Path $installDir "yasb.exe"
$yasbcExe = Join-Path $installDir "yasbc.exe"

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

function Test-ZipLocked {
    try {
        $fs = [System.IO.File]::Open($zipPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::Read)
        $fs.Close()
        return $false
    } catch {
        return $true
    }
}

function Stop-Yasb {
    Write-Host "Stopping YASB..."
    try { & $yasbcExe stop 2>$null | Out-Null } catch {}
    Start-Sleep -Seconds 1
    $proc = Get-Process -Name yasb -ErrorAction SilentlyContinue
    if ($proc) {
        Stop-Process -Name yasb -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
    }
    # The lock can linger briefly after the process exits - wait for it to clear
    for ($i = 0; $i -lt 10; $i++) {
        if (-not (Test-ZipLocked)) { return $true }
        Start-Sleep -Milliseconds 500
    }
    Write-Host "library.zip is still locked. Quit YASB manually (tray icon -> Quit) and run this script again."
    return $false
}

function Start-Yasb {
    Write-Host "Restarting YASB..."
    try {
        schtasks /Create /TN "yasb-cava-menu" /TR "`"$yasbExe`"" /SC ONCE /ST 23:59 /IT /F | Out-Null
        schtasks /Run /TN "yasb-cava-menu" | Out-Null
        Start-Sleep -Seconds 2
        schtasks /Delete /TN "yasb-cava-menu" /F | Out-Null
        if (Get-Process -Name yasb -ErrorAction SilentlyContinue) {
            Write-Host "YASB is running again."
            return
        }
    } catch {}
    Write-Host "Please start YASB manually (start menu shortcut or: yasbc start)."
}

if (-not (Test-Path -LiteralPath $zipPath)) {
    Write-Error "library.zip not found at $zipPath"
    exit 1
}

if ($Restore) {
    if (-not (Test-Path -LiteralPath $backupPath)) {
        Write-Error "Backup not found at $backupPath"
        exit 1
    }
    Copy-Item -LiteralPath $backupPath -Destination $zipPath -Force
    Write-Host "Restored original library.zip"
    if (-not (Get-Process -Name yasb -ErrorAction SilentlyContinue)) { Start-Yasb }
    exit 0
}

if (-not (Test-Path -LiteralPath $srcPath)) {
    Write-Error "cava_menu.py not found next to this script"
    exit 1
}

# Back up the original archive once
if (-not (Test-Path -LiteralPath $backupPath)) {
    Copy-Item -LiteralPath $zipPath -Destination $backupPath -Force
    Write-Host "Backed up library.zip -> library.zip.bak"
}

# Open the archive for update; stop YASB first if it is locked
$fs = $null
if (Test-ZipLocked) {
    Write-Host "library.zip is locked by a running YASB."
    if (-not (Stop-Yasb)) { exit 1 }
}
try {
    $fs = [System.IO.File]::Open($zipPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite)
} catch {
    Write-Error "Cannot open library.zip: $($_.Exception.Message)"
    exit 1
}

$zip = New-Object System.IO.Compression.ZipArchive($fs, [System.IO.Compression.ZipArchiveMode]::Update)
try {
    $existing = $zip.GetEntry($entryName)
    if ($existing) { $existing.Delete() }
    $entry = $zip.CreateEntry($entryName, [System.IO.Compression.CompressionLevel]::Optimal)
    $writer = New-Object System.IO.StreamWriter($entry.Open())
    $writer.Write([System.IO.File]::ReadAllText($srcPath))
    $writer.Close()
    Write-Host "Installed $entryName into library.zip"
} finally {
    $zip.Dispose()
    $fs.Close()
}

# Verify the entry is present
$verify = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
$ok = $null -ne $verify.GetEntry($entryName)
$verify.Dispose()
if (-not $ok) {
    Write-Error "Verification failed - $entryName not found in archive"
    exit 1
}
Write-Host "Verified: $entryName present in archive."

Write-Host ""
Write-Host "Done."
if (-not (Get-Process -Name yasb -ErrorAction SilentlyContinue)) {
    Start-Yasb
} else {
    Write-Host "YASB is running. Fully restart it (tray icon -> Quit, then start YASB again) for the widget to load."
}
Write-Host ""
Write-Host "NOTE: YASB updates overwrite library.zip - re-run this script after every update."
