; PyDM Windows installer (Inno Setup 6).  Built by tools/build_windows.ps1:
;   iscc /DAppVersion=4.1 /DSourceDir=<PyInstaller dist\PyDM> /DFfmpegDir=<dir> /DOutDir=<dir> pydm.iss
;
; Per-user install by default (no admin prompt), so signed updates can install silently:
;   PyDM-Setup.exe /SILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS
; "Install for all users" is still offered on the first page.

#ifndef AppVersion
  #define AppVersion "0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\build\dist\PyDM"
#endif
#ifndef FfmpegDir
  #define FfmpegDir "..\build\ffmpeg"
#endif
#ifndef DenoDir
  #define DenoDir "..\build\deno"
#endif
#ifndef OutDir
  #define OutDir "..\build\installer"
#endif

[Setup]
AppId={{8B5E2C7A-4F1D-4E93-9C6B-2D7F0A61E3B4}
AppName=PyDM
AppVersion={#AppVersion}
AppVerName=PyDM {#AppVersion}
AppPublisher=Yassin Mahgoub
AppPublisherURL=https://github.com/mahgob601/PyDM
AppSupportURL=https://github.com/mahgob601/PyDM/issues
AppUpdatesURL=https://github.com/mahgob601/PyDM/releases
VersionInfoVersion={#AppVersion}
VersionInfoDescription=PyDM download manager setup
DefaultDirName={autopf}\PyDM
DefaultGroupName=PyDM
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; PyDM creates this mutex while running: setup asks to close it instead of failing on locked files
AppMutex=PyDM.AppMutex
CloseApplications=yes
RestartApplications=no
SetupIconFile=pydm.ico
UninstallDisplayIcon={app}\PyDM.exe
UninstallDisplayName=PyDM
WizardStyle=modern
Compression=lzma2/ultra64
SolidCompression=yes
OutputDir={#OutDir}
OutputBaseFilename=PyDM-Setup-{#AppVersion}
ChangesAssociations=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked
Name: "startup"; Description: "Start PyDM in the tray when I sign in (catches browser downloads)"; GroupDescription: "Startup:"

[InstallDelete]
; upgrades: drop the previous build's libraries so nothing stale is loaded
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#FfmpegDir}\ffmpeg.exe"; DestDir: "{app}\ffmpeg"; Flags: ignoreversion
Source: "{#FfmpegDir}\LICENSE.txt"; DestDir: "{app}\ffmpeg"; Flags: ignoreversion
Source: "{#DenoDir}\deno.exe"; DestDir: "{app}\deno"; Flags: ignoreversion
Source: "{#DenoDir}\LICENSE.md"; DestDir: "{app}\deno"; Flags: ignoreversion
Source: "..\extension\*"; DestDir: "{app}\browser-extension"; Excludes: "manifest.firefox.json"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\extension\manifest.firefox.json"; DestDir: "{app}\browser-extension-firefox"; DestName: "manifest.json"; Flags: ignoreversion
Source: "..\extension\*"; DestDir: "{app}\browser-extension-firefox"; Excludes: "manifest.json,manifest.firefox.json"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\PyDM"; Filename: "{app}\PyDM.exe"
Name: "{autoprograms}\PyDM browser extension (folder)"; Filename: "{app}\browser-extension"
Name: "{autodesktop}\PyDM"; Filename: "{app}\PyDM.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "PyDM"; ValueData: """{app}\PyDM.exe"" --hidden"; Tasks: startup; Flags: uninsdeletevalue

[Run]
; no skipifsilent: after a silent update PyDM starts again by itself
Filename: "{app}\PyDM.exe"; Description: "Start PyDM now"; Flags: nowait postinstall

[UninstallRun]
Filename: "{cmd}"; Parameters: "/c reg delete HKCU\Software\Microsoft\Windows\CurrentVersion\Run /v PyDM /f"; Flags: runhidden; RunOnceId: "RemoveRunKey"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{%USERPROFILE}\.pydm');
    if DirExists(DataDir) and not UninstallSilent() then
      if MsgBox('Also delete your PyDM settings, download list and history?' + #13#10 +
                '(Your downloaded files are not touched.)',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;
