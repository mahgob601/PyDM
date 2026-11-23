# Builds PyDM.exe (PyInstaller) and PyDM-Setup-<version>.exe (Inno Setup) on Windows.
#   powershell -ExecutionPolicy Bypass -File tools\build_windows.ps1
# Needs Python 3.12 on PATH; installs Inno Setup with winget/choco if it's missing.
# Output: build\installer\PyDM-Setup-<version>.exe
param([string]$Python = "python")
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$root = Split-Path -Parent $PSScriptRoot
$build = Join-Path $root "build"
Set-Location $root

$version = & $Python -c "import sys; sys.path.insert(0, r'$root'); from pydm.core.util import VERSION; print(VERSION)"
if ($LASTEXITCODE -ne 0) { throw "can't read VERSION" }
Write-Host "== PyDM $version"
New-Item -ItemType Directory -Force $build | Out-Null

# --- 1. PyDM.exe -------------------------------------------------------------------------
& $Python -m pip install --disable-pip-version-check -q -r requirements.txt pyinstaller
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
& $Python -m PyInstaller --noconfirm --clean --windowed --name PyDM `
    --icon (Join-Path $root "installer\pydm.ico") `
    --collect-submodules pydm --collect-submodules yt_dlp `
    --distpath (Join-Path $build "dist") --workpath (Join-Path $build "work") `
    --specpath $build (Join-Path $root "run.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
$exe = Join-Path $build "dist\PyDM\PyDM.exe"
if (-not (Test-Path $exe)) { throw "PyDM.exe missing" }

# --- 2. ffmpeg (LGPL build, checksum-verified) ---------------------------------------------
$ff = Join-Path $build "ffmpeg"
if (-not (Test-Path (Join-Path $ff "ffmpeg.exe"))) {
    New-Item -ItemType Directory -Force $ff | Out-Null
    $base = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest"
    $ok = $false
    foreach ($flavor in @("lgpl", "gpl")) {     # LGPL preferred; GPL only if LGPL lacks MP3
        $name = "ffmpeg-master-latest-win64-$flavor"
        $zip = Join-Path $build "$name.zip"
        Invoke-WebRequest "$base/$name.zip" -OutFile $zip -UseBasicParsing
        $sums = (Invoke-WebRequest "$base/checksums.sha256" -UseBasicParsing).Content
        $line = ($sums -split "`n") | Where-Object { $_ -match " \*?$name\.zip\s*$" } | Select-Object -First 1
        if (-not $line) { throw "no published checksum for $name.zip" }
        $want = ($line -split "\s+")[0].ToLower()
        $got = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
        if ($want -ne $got) { throw "ffmpeg checksum mismatch: $got vs $want" }
        $x = Join-Path $build "ffx"
        Remove-Item -Recurse -Force $x -ErrorAction SilentlyContinue
        Expand-Archive $zip $x
        $bin = Join-Path $x "$name\bin\ffmpeg.exe"
        $enc = & $bin -hide_banner -encoders 2>$null | Out-String
        if ($enc -match "libmp3lame") {
            Copy-Item $bin (Join-Path $ff "ffmpeg.exe")
            Copy-Item (Join-Path $x "$name\LICENSE.txt") (Join-Path $ff "LICENSE.txt")
            Add-Content (Join-Path $ff "LICENSE.txt") "`r`n`r`nBuild: $name.zip from https://github.com/BtbN/FFmpeg-Builds (source: https://github.com/FFmpeg/FFmpeg)`r`n"
            Write-Host "== ffmpeg: $name"
            $ok = $true
            break
        }
    }
    if (-not $ok) { throw "no ffmpeg build with libmp3lame" }
}

# --- 3. Installer ------------------------------------------------------------------------
$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
          "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
    if (Get-Command choco -ErrorAction SilentlyContinue) { choco install innosetup -y --no-progress | Out-Null }
    else { winget install --id JRSoftware.InnoSetup -e --silent --accept-package-agreements --accept-source-agreements | Out-Null }
    $iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
              "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $iscc) { throw "Inno Setup not found" }
}
& $iscc /Q "/DAppVersion=$version" "/DSourceDir=$build\dist\PyDM" "/DFfmpegDir=$ff" `
    "/DOutDir=$build\installer" (Join-Path $root "installer\pydm.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
$setup = Join-Path $build "installer\PyDM-Setup-$version.exe"
if (-not (Test-Path $setup)) { throw "installer missing" }
"{0} ({1:N1} MB)" -f $setup, ((Get-Item $setup).Length / 1MB)
