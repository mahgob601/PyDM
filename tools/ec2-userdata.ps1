<powershell>
# PyDM Windows test machine — fully unattended first-boot setup (EC2 user data).
# Installs the toolchain, registers a GitHub Actions runner, signs a local test user in
# automatically so the tests get a real desktop, and turns itself off after 5 hours.
$Token = "__RUNNER_TOKEN__"
$Repo  = "https://github.com/mahgob601/PyDM"
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ci = "C:\pydm-ci"; New-Item -ItemType Directory -Force $ci | Out-Null
Start-Transcript -Path "$ci\setup.log" -Append
# Safety net that survives reboots: power off (= terminate, given the launch setting) 5 hours from now.
Register-ScheduledTask -TaskName "PyDM-killswitch" -User SYSTEM -RunLevel Highest -Force `
  -Trigger (New-ScheduledTaskTrigger -Once -At (Get-Date).AddHours(5)) `
  -Action (New-ScheduledTaskAction -Execute "shutdown.exe" -Argument "/s /f /t 0") | Out-Null
function Get-File($url, $out) {
  for ($i = 0; $i -lt 3; $i++) {
    try { Invoke-WebRequest $url -OutFile $out -UseBasicParsing; return } catch { Start-Sleep 5 }
  }
  throw "download failed: $url"
}
function Add-MachinePath($dir) {
  $p = [Environment]::GetEnvironmentVariable("Path", "Machine")
  if ($p -notlike "*$dir*") { [Environment]::SetEnvironmentVariable("Path", "$p;$dir", "Machine") }
}
Get-File "https://aka.ms/vs/17/release/vc_redist.x64.exe" "$ci\vc_redist.exe"
Start-Process "$ci\vc_redist.exe" -ArgumentList "/install /quiet /norestart" -Wait
Get-File "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" "$ci\python.exe"
Start-Process "$ci\python.exe" -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0" -Wait
Get-File "https://github.com/git-for-windows/git/releases/download/v2.47.1.windows.1/Git-2.47.1-64-bit.exe" "$ci\git.exe"
Start-Process "$ci\git.exe" -ArgumentList "/VERYSILENT /NORESTART /NOCANCEL /SP-" -Wait
Get-File "https://nodejs.org/dist/v22.12.0/node-v22.12.0-x64.msi" "$ci\node.msi"
Start-Process msiexec.exe -ArgumentList "/i `"$ci\node.msi`" /qn /norestart" -Wait
Get-File "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" "$ci\ffmpeg.zip"
Expand-Archive "$ci\ffmpeg.zip" -DestinationPath $ci -Force
Add-MachinePath (Get-ChildItem "$ci\ffmpeg-*\bin" | Select-Object -First 1).FullName
Get-File "https://www.7-zip.org/a/7z2409-x64.exe" "$ci\7z.exe"
Start-Process "$ci\7z.exe" -ArgumentList "/S" -Wait
Add-MachinePath "C:\Program Files\7-Zip"
$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine")
& "C:\Program Files\nodejs\npx.cmd" --yes @puppeteer/browsers install chrome@stable --path "$ci\chrome"

# local test user that signs in automatically (gives the tests a real desktop session)
$pw = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 24 | ForEach-Object {[char]$_}) + "!a1"
net user pydmci $pw /add /y | Out-Null
net localgroup Administrators pydmci /add | Out-Null
$wl = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
Set-ItemProperty $wl AutoAdminLogon "1"; Set-ItemProperty $wl DefaultUserName "pydmci"
Set-ItemProperty $wl DefaultPassword $pw; Set-ItemProperty $wl DefaultDomainName $env:COMPUTERNAME
# full admin rights for the test user (needed for powercfg /requests and Defender scans)
Set-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" EnableLUA 0
powercfg /change monitor-timeout-ac 0; powercfg /change standby-timeout-ac 0

$rel = Invoke-RestMethod "https://api.github.com/repos/actions/runner/releases/latest"
$asset = $rel.assets | Where-Object { $_.name -like "actions-runner-win-x64-*.zip" } | Select-Object -First 1
$runner = "C:\actions-runner"; New-Item -ItemType Directory -Force $runner | Out-Null
Get-File $asset.browser_download_url "$ci\runner.zip"
Expand-Archive "$ci\runner.zip" -DestinationPath $runner -Force
Set-Location $runner
& .\config.cmd --url $Repo --token $Token --name pydm-ec2 --labels pydm --work _work --unattended --replace
icacls $runner /grant "pydmci:(OI)(CI)F" /T /Q | Out-Null
icacls $ci /grant "pydmci:(OI)(CI)F" /T /Q | Out-Null

# start the runner in pydmci's desktop session at every sign-in
$act = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c cd /d $runner && run.cmd > $ci\runner.log 2>&1"
$trg = New-ScheduledTaskTrigger -AtLogOn -User "pydmci"
$prn = New-ScheduledTaskPrincipal -UserId "pydmci" -LogonType Interactive -RunLevel Highest
Register-ScheduledTask -TaskName "PyDM-runner" -Action $act -Trigger $trg -Principal $prn -Force | Out-Null
"setup complete $(Get-Date)" | Out-File "$ci\setup-done.txt"
Stop-Transcript
# EC2Launch v2 ignores Restart-Computer inside user data; exit code 3010 asks it to reboot.
exit 3010
</powershell>
<persist>false</persist>
