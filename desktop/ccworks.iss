#if Ver < EncodeVer(6, 7, 4, 0) || Ver >= EncodeVer(6, 7, 5, 0)
  #error The release build requires Inno Setup 6.7.4.
#endif

#ifndef AppVersion
  #error AppVersion must be supplied by build-windows.ps1
#endif

[Setup]
AppId=io.github.pu-shd.ccworks.desktop
AppName=ccworks Desktop
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\ccworks
UsePreviousAppDir=no
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\ccworks-desktop.exe
OutputDir=..\dist
OutputBaseFilename=ccworks-desktop-{#AppVersion}-windows-x64
Compression=lzma2
SolidCompression=yes
#ifdef ReleaseSigning
SignTool=ccworks
SignedUninstaller=yes
#endif

[Files]
Source: "..\dist\ccworks-desktop\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{userprograms}\ccworks Desktop"; Filename: "{app}\ccworks-desktop.exe"

[Code]
function PurgeRequested(): Boolean;
var
  Index: Integer;
begin
  Result := True;
  for Index := 1 to ParamCount do
    if CompareText(ParamStr(Index), '/PURGECCWORKSDATA') = 0 then
      Exit;
  Result := False;
end;

function InitializeUninstall(): Boolean;
var
  RemoveData: Boolean;
  ExitStatus: Integer;
begin
  Result := True;
  if UninstallSilent then
    RemoveData := PurgeRequested()
  else
    RemoveData := MsgBox(
      'Remove the saved ccworks sign-in and local ccworks data?',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2
    ) = IDYES;

  if not RemoveData then
    Exit;
  Result := Exec(
    ExpandConstant('{app}\ccworks.exe'), 'desktop remove-data',
    ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ExitStatus
  ) and (ExitStatus = 0);
  if not Result then
    SuppressibleMsgBox(
      'ccworks data could not be removed. Close ccworks Desktop and finish any open Work operation, then retry.',
      mbError, MB_OK, IDOK
    );
end;
