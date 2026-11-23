# Installs the freshly built setup the way users get it, checks it end to end, exercises the
# real self-update path, then uninstalls. Exits non-zero on the first failure.
#   powershell -ExecutionPolicy Bypass -File tools\smoke_installer.ps1 [-Python python]
param([string]$Python = "python")
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$root = Split-Path -Parent $PSScriptRoot
$setup = Get-ChildItem (Join-Path $root "build\installer\PyDM-Setup-*.exe") | Select-Object -First 1
if (-not $setup) { throw "no installer in build\installer" }
$setup = $setup.FullName
$tmp = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { $env:TEMP }
$dir = Join-Path $tmp "PyDM smoke install"
$fails = @()
function Check($name, $ok, $info = "") {
    if ($ok) { Write-Host "PASS $name $info" } else { Write-Host "FAIL $name $info"; $script:fails += $name }
}
function PyDMProcs { @(Get-Process PyDM -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$dir*" }) }

# --- fresh install (per-user, silent) ----------------------------------------------------
Get-Process PyDM -ErrorAction SilentlyContinue | Stop-Process -Force
$log1 = Join-Path $tmp "pydm-install.log"
$p = Start-Process $setup -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER",
    "/DIR=`"$dir`"", "/TASKS=startup,desktopicon", "/LOG=`"$log1`"" -Wait -PassThru
Check "installer exits cleanly" ($p.ExitCode -eq 0) "exit $($p.ExitCode)"
$exe = Join-Path $dir "PyDM.exe"
Check "PyDM.exe installed" (Test-Path $exe)
Check "bundled ffmpeg installed" (Test-Path (Join-Path $dir "ffmpeg\ffmpeg.exe"))
Check "ffmpeg license shipped" (Test-Path (Join-Path $dir "ffmpeg\LICENSE.txt"))
Check "uninstaller present" (Test-Path (Join-Path $dir "unins000.exe"))
$chrome = Get-Content (Join-Path $dir "browser-extension\manifest.json") -Raw | ConvertFrom-Json
Check "Chrome extension folder (MV3)" ($chrome.manifest_version -eq 3)
$fx = Get-Content (Join-Path $dir "browser-extension-firefox\manifest.json") -Raw
Check "Firefox extension folder" ($fx -match "gecko")
$run = (Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name PyDM -ErrorAction SilentlyContinue).PyDM
Check "starts at sign-in (tray)" ($run -like "*PyDM.exe*--hidden*") "$run"
$lnk = Join-Path ([Environment]::GetFolderPath("Programs")) "PyDM.lnk"
Check "Start menu shortcut" (Test-Path $lnk)
Check "desktop shortcut" (Test-Path (Join-Path ([Environment]::GetFolderPath("Desktop")) "PyDM.lnk"))

# --- installed app works -----------------------------------------------------------------
$enc = & (Join-Path $dir "ffmpeg\ffmpeg.exe") -hide_banner -encoders 2>$null | Out-String
Check "bundled ffmpeg can make MP3" ($enc -match "libmp3lame")
$deadline = (Get-Date).AddSeconds(20)
while (-not (PyDMProcs) -and (Get-Date) -lt $deadline) { Start-Sleep 1 }
Check "setup started PyDM afterwards" ([bool](PyDMProcs))
Start-Sleep 8
Check "PyDM keeps running" ([bool](PyDMProcs))

$srvDir = Join-Path $tmp "pydm-smoke-srv"
New-Item -ItemType Directory -Force $srvDir | Out-Null
$payload = Join-Path $srvDir "payload.bin"
$bytes = New-Object byte[] (12MB); (New-Object Random 7).NextBytes($bytes); [IO.File]::WriteAllBytes($payload, $bytes)
$port = Get-Random -Minimum 20000 -Maximum 40000
$srv = Start-Process $Python -ArgumentList "-m", "http.server", "$port", "--bind", "127.0.0.1", "--directory", "`"$srvDir`"" -PassThru -WindowStyle Hidden
Start-Sleep 2
$out = Join-Path $tmp "pydm-smoke-out"
Remove-Item -Recurse -Force $out -ErrorAction SilentlyContinue
$c = Start-Process $exe -ArgumentList "--cli", "http://127.0.0.1:$port/payload.bin", "-o", "`"$out`"" -Wait -PassThru
$got = Join-Path $out "payload.bin"
$same = (Test-Path $got) -and ((Get-FileHash $got).Hash -eq (Get-FileHash $payload).Hash)
Check "installed PyDM.exe downloads a file intact" ($c.ExitCode -eq 0 -and $same) "exit $($c.ExitCode)"
Stop-Process -Id $srv.Id -Force -ErrorAction SilentlyContinue

# --- self-update path: the exact code 'Install now' runs ----------------------------------
$old = (PyDMProcs)[0]
if ($old) {
    $log2 = Join-Path $tmp "pydm-update.log"
    & $Python -c "import sys; sys.path.insert(0, r'$root'); from pydm.core.updater import launch_installer; launch_installer(r'$setup', $($old.Id))"
    Check "update helper started" ($LASTEXITCODE -eq 0)
    Start-Sleep 3
    Check "update waits for PyDM to quit" ([bool](Get-Process -Id $old.Id -ErrorAction SilentlyContinue))
    Stop-Process -Id $old.Id                      # what 'Install now' does: PyDM quits
    $deadline = (Get-Date).AddSeconds(150)
    $new = $null
    while ((Get-Date) -lt $deadline) {
        Start-Sleep 2
        $new = PyDMProcs | Where-Object { $_.Id -ne $old.Id } | Select-Object -First 1
        $busy = Get-Process | Where-Object { $_.Path -like "*PyDM-Setup*" -or $_.Path -like "*\is-*.tmp\*" }
        if ($new -and -not $busy) { break }
    }
    Check "update installs in place and PyDM restarts" ([bool]$new) "$($new.Path)"
    Check "still one install, same folder" (Test-Path $exe)
} else {
    Check "self-update path (needs a running PyDM)" $false
}

# --- uninstall ---------------------------------------------------------------------------
PyDMProcs | Stop-Process -Force
Start-Sleep 2
$u = Start-Process (Join-Path $dir "unins000.exe") -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART" -Wait -PassThru
$deadline = (Get-Date).AddSeconds(60)
while ((Test-Path $exe) -and (Get-Date) -lt $deadline) { Start-Sleep 1 }   # uninstaller re-spawns itself
Start-Sleep 3
Check "uninstaller exits cleanly" ($u.ExitCode -eq 0) "exit $($u.ExitCode)"
Check "program files removed" (-not (Test-Path $exe))
$run = (Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name PyDM -ErrorAction SilentlyContinue).PyDM
Check "sign-in startup removed" (-not $run)
Check "Start menu shortcut removed" (-not (Test-Path $lnk))

if ($fails.Count) {
    Write-Host "--- install log (tail)"; Get-Content $log1 -Tail 40 -ErrorAction SilentlyContinue
    throw "$($fails.Count) smoke check(s) failed: $($fails -join '; ')"
}
Write-Host "All installer smoke checks passed"
