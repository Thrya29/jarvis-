; Inno Setup script - builds JarvisSetup-<version>.exe from dist\jarvis.
; Usage: iscc /DAppVersion=1.0.0 packaging\jarvis.iss
; Per-user install: no admin rights required.

#ifndef AppVersion
  #error AppVersion must be defined, e.g. /DAppVersion=1.0.0
#endif

[Setup]
AppId={{6C1F0E5B-3F7A-4E0B-9E5C-4A7D2B8F9A11}
AppName=JARVIS
AppVersion={#AppVersion}
AppVerName=JARVIS {#AppVersion}
AppPublisher=Thrya29
AppPublisherURL=https://github.com/Thrya29/jarvis-
DefaultDirName={localappdata}\Programs\Jarvis
DefaultGroupName=JARVIS
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=Output
OutputBaseFilename=JarvisSetup-{#AppVersion}
SetupIconFile=jarvis.ico
UninstallDisplayIcon={app}\jarvisw.exe
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.19045
ChangesEnvironment=yes
; Close a running JARVIS before upgrading its files.
CloseApplications=force
RestartApplications=no
WizardStyle=modern
UninstallDisplayName=JARVIS

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
Name: "autostart"; Description: "Start JARVIS in the tray when I sign in"; Flags: unchecked
Name: "addtopath"; Description: "Add the jarvis command to my PATH"; Flags: checkedonce

[Files]
Source: "..\dist\jarvis\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Remove files from older versions that are no longer shipped.
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{group}\JARVIS"; Filename: "{app}\jarvisw.exe"; Parameters: "app"; IconFilename: "{app}\jarvisw.exe"
Name: "{group}\JARVIS Doctor"; Filename: "{cmd}"; Parameters: "/k ""{app}\jarvis.exe"" doctor"
Name: "{group}\Uninstall JARVIS"; Filename: "{uninstallexe}"
Name: "{userdesktop}\JARVIS"; Filename: "{app}\jarvisw.exe"; Parameters: "app"; Tasks: desktopicon
Name: "{userstartup}\JARVIS"; Filename: "{app}\jarvisw.exe"; Parameters: "app --minimized"; Tasks: autostart

[Registry]
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; \
  ValueData: "{olddata};{app}"; Tasks: addtopath; Check: NeedsAddPath(ExpandConstant('{app}'))

[Run]
Filename: "{app}\jarvis.exe"; Parameters: "config init"; Flags: runhidden
Filename: "{app}\jarvisw.exe"; Parameters: "app"; Description: "Start JARVIS"; \
  Flags: postinstall nowait skipifsilent

[UninstallRun]
; Stop a running JARVIS so its files can be removed (user data is kept).
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM jarvisw.exe /T"; Flags: runhidden; RunOnceId: "StopJarvisw"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM jarvis.exe /T"; Flags: runhidden; RunOnceId: "StopJarvis"
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM jarvis-overlay.exe"; Flags: runhidden; RunOnceId: "StopOverlay"

[Code]
function NeedsAddPath(Dir: string): Boolean;
var
  Path: string;
begin
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', Path) then
  begin
    Result := True;
    exit;
  end;
  Result := Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Path) + ';') = 0;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Path, Dir: string;
  P: Integer;
begin
  if CurUninstallStep <> usPostUninstall then exit;
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', Path) then exit;
  Dir := ExpandConstant('{app}');
  P := Pos(';' + Uppercase(Dir), Uppercase(Path));
  if P > 0 then
  begin
    Delete(Path, P, Length(Dir) + 1);
    RegWriteExpandStringValue(HKCU, 'Environment', 'Path', Path);
  end;
end;
