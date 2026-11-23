# PyDM Windows test machine setup — run in PowerShell *as Administrator* on the EC2 instance.
# 1) Paste your runner token below (GitHub repo → Settings → Actions → Runners →
#    New self-hosted runner → Windows → copy the value after --token). It expires in 1 hour.
$Token = "PASTE_RUNNER_TOKEN_HERE"
$Repo  = "https://github.com/mahgob601/PyDM"

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"          # makes downloads ~10x faster in PowerShell 5
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
if ($Token -eq "PASTE_RUNNER_TOKEN_HERE") { throw "Paste the runner token into `$Token first." }
$ci = "C:\pydm-ci"; New-Item -ItemType Directory -Force $ci | Out-Null
function Get-File($url, $out) { Write-Host "Downloading $url"; Invoke-WebRequest $url -OutFile $out -UseBasicParsing }
function Add-MachinePath($dir) {
  $p = [Environment]::GetEnvironmentVariable("Path", "Machine")
  if ($p -notlike "*$dir*") { [Environment]::SetEnvironmentVariable("Path", "$p;$dir", "Machine") }
}
function Refresh-Path { $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User") }

Write-Host "== Visual C++ runtime"
Get-File "https://aka.ms/vs/17/release/vc_redist.x64.exe" "$ci\vc_redist.exe"
Start-Process "$ci\vc_redist.exe" -ArgumentList "/install /quiet /norestart" -Wait

Write-Host "== Python 3.12"
Get-File "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" "$ci\python.exe"
Start-Process "$ci\python.exe" -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0" -Wait

Write-Host "== Git"
Get-File "https://github.com/git-for-windows/git/releases/download/v2.47.1.windows.1/Git-2.47.1-64-bit.exe" "$ci\git.exe"
Start-Process "$ci\git.exe" -ArgumentList "/VERYSILENT /NORESTART /NOCANCEL /SP-" -Wait

Write-Host "== Node.js"
Get-File "https://nodejs.org/dist/v22.12.0/node-v22.12.0-x64.msi" "$ci\node.msi"
Start-Process msiexec.exe -ArgumentList "/i `"$ci\node.msi`" /qn /norestart" -Wait

Write-Host "== ffmpeg"
Get-File "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" "$ci\ffmpeg.zip"
Expand-Archive "$ci\ffmpeg.zip" -DestinationPath $ci -Force
$ffbin = (Get-ChildItem "$ci\ffmpeg-*\bin" | Select-Object -First 1).FullName
Add-MachinePath $ffbin

Write-Host "== 7-Zip"
Get-File "https://www.7-zip.org/a/7z2409-x64.exe" "$ci\7z.exe"
Start-Process "$ci\7z.exe" -ArgumentList "/S" -Wait
Add-MachinePath "C:\Program Files\7-Zip"

Refresh-Path
Write-Host "== Chrome for Testing (for the extension tests)"
npx --yes @puppeteer/browsers install chrome@stable --path "$ci\chrome"

Write-Host "== GitHub Actions runner"
$rel = Invoke-RestMethod "https://api.github.com/repos/actions/runner/releases/latest"
$asset = $rel.assets | Where-Object { $_.name -like "actions-runner-win-x64-*.zip" } | Select-Object -First 1
$runner = "C:\actions-runner"; New-Item -ItemType Directory -Force $runner | Out-Null
Get-File $asset.browser_download_url "$ci\runner.zip"
Expand-Archive "$ci\runner.zip" -DestinationPath $runner -Force
Set-Location $runner
.\config.cmd --url $Repo --token $Token --name pydm-ec2 --labels pydm --work _work --unattended --replace

Write-Host ""
Write-Host "Versions:" ; python --version; git --version; node --version; & "$ffbin\ffmpeg.exe" -version | Select-Object -First 1
Write-Host ""
Write-Host "All set. Starting the runner in a new window — keep this Remote Desktop session open"
Write-Host "(minimize it, don't sign out) so the app's window and tray tests have a real desktop."
Start-Process cmd.exe -ArgumentList "/k cd /d $runner && run.cmd" -WorkingDirectory $runner
