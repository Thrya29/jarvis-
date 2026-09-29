; Inno Setup script - builds JarvisSetup-<version>.exe from dist\jarvis.
; Usage: iscc /DAppVersion=0.1.0 packaging\jarvis.iss
; Per-user install: no admin rights required.

#ifndef AppVersion
  #error AppVersion must be defined, e.g. /DAppVersion=0.1.0
#endif

[Setup]
AppId={{6C1F0E5B-3F7A-4E0B-9E5C-4A7D2B8F9A11}
AppName=JARVIS
AppVersion={#AppVersion}
AppPublisher=Thrya29
AppPublisherURL=https://github.com/Thrya29/jarvis-
DefaultDirName={localappdata}\Programs\Jarvis
DefaultGroupName=JARVIS
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=Output
OutputBaseFilename=JarvisSetup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.19045
ChangesEnvironment=yes
WizardStyle=modern
UninstallDisplayName=JARVIS

[Tasks]
Name: "addtopath"; Description: "Add jarvis to my PATH"; Flags: checkedonce
Name: "autostart"; Description: "Start JARVIS when I sign in"; Flags: unchecked

[Files]
Source: "..\dist\jarvis\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\JARVIS"; Filename: "{app}\jarvis.exe"; Parameters: "run"
Name: "{group}\JARVIS Doctor"; Filename: "{cmd}"; Parameters: "/k ""{app}\jarvis.exe"" doctor"
Name: "{userstartup}\JARVIS"; Filename: "{app}\jarvis.exe"; Parameters: "run"; Tasks: autostart

[Registry]
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; \
  ValueData: "{olddata};{app}"; Tasks: addtopath; Check: NeedsAddPath(ExpandConstant('{app}'))

[Run]
Filename: "{app}\jarvis.exe"; Parameters: "config init"; Flags: runhidden

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
