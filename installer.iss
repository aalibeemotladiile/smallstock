; Cattle & Small Stock System — Windows installer
; Health Data Matrics (HDM Group)
;
; Built by .github/workflows/build-windows.yml after PyInstaller, or by hand:
;     "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
;
; The installer asks for the passkey before it will copy anything. It holds
; only the SHA-256 of that key — enough to recognise the right one, not
; enough to work out what it is. Change the key with make_key.py and paste
; the digest it prints at MyPasskeyHash below.

#define MyAppName        "Cattle & Small Stock System"
#define MyAppShortName   "CattleSmallStock"
#define MyAppVersion     "1.0.0"
#define MyAppPublisher   "Health Data Matrics (HDM Group)"
#define MyAppExeName     "CattleSmallStock.exe"
#define MyPasskeyHash    "ee4377831b0bbf0c9c7466875198157ef625751f075b96ab8375f168f05795dc"

; Inno Setup 6.3 renamed the 64-bit architecture values: what used to be
; "x64" is now "x64compatible". Both mean the same thing here — install as a
; 64-bit program on any 64-bit Windows. The compiler tells us its own
; version, so the script picks the spelling that compiler understands and
; builds on 6.0 through the current release without anyone editing it.
#if VER >= EncodeVer(6,3,0)
  #define MyArch "x64compatible"
  #pragma message "Inno Setup 6.3 or newer: using x64compatible"
#else
  #define MyArch "x64"
  #pragma message "Inno Setup older than 6.3: using x64"
#endif

[Setup]
AppId={{9E4C1A7B-3D62-4F18-9A55-CA71D0B8E4F2}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppShortName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=installer_output
OutputBaseFilename=CattleSmallStock-Setup-{#MyAppVersion}
SetupIconFile=logo.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode={#MyArch}
ArchitecturesAllowed={#MyArch}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; \
    GroupDescription: "Shortcuts:"

[Files]
; Everything PyInstaller produced, the folder and all of it.
Source: "dist\{#MyAppShortName}\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
Source: "logo.ico";   DestDir: "{app}"; Flags: ignoreversion
Source: "README.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
    IconFilename: "{app}\logo.ico"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
    IconFilename: "{app}\logo.ico"; Tasks: desktopicon

[Run]
; Started with the passkey that was just typed, so the app unlocks this
; computer without asking for it a second time. The key is handed over on the
; command line and never written to disk; the app turns it into a record tied
; to this machine and forgets it.
Filename: "{app}\{#MyAppExeName}"; \
    Parameters: "--key ""{code:TypedPasskey}"""; \
    Description: "Start {#MyAppName}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: files; Name: "{app}\passkey.hash"

[Code]
var
  PasskeyPage: TInputQueryWizardPage;
  PasskeyAccepted: Boolean;

procedure InitializeWizard;
begin
  PasskeyAccepted := False;
  PasskeyPage := CreateInputQueryPage(wpWelcome,
    'Installation passkey',
    'This copy is licensed. Enter the passkey to continue.',
    'The passkey was supplied with your copy of the system by ' +
    'Health Data Matrics (HDM Group). It is asked for once, here, when ' +
    'the system is installed on this computer.');
  { True = masked: the key is not left on screen for the room to read. }
  PasskeyPage.Add('Passkey:', True);
end;

function PasskeyIsCorrect(const Typed: String): Boolean;
var
  Cleaned: String;
begin
  { GetSHA256OfString hashes the bytes of an ANSI string, which matches what
    make_key.py produces for a plain-ASCII key. Keep the key to ASCII, or the
    installer and the app will disagree about what the right key is. }
  Cleaned := Trim(Typed);
  Result := (Cleaned <> '') and
            (Lowercase(GetSHA256OfString(Cleaned)) =
             Lowercase('{#MyPasskeyHash}'));
end;

function TypedPasskey(Param: String): String;
begin
  { Only ever read back to hand to the app we just installed. }
  Result := Trim(PasskeyPage.Values[0]);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = PasskeyPage.ID then
  begin
    if PasskeyIsCorrect(PasskeyPage.Values[0]) then
    begin
      PasskeyAccepted := True;
    end
    else
    begin
      PasskeyAccepted := False;
      MsgBox('That passkey is not right.' + #13#10#13#10 +
             'Check it and try again. If it has been lost, ask Health Data ' +
             'Matrics (HDM Group) for it.',
             mbError, MB_OK);
      Result := False;                 { stay on this page }
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  HashFile: String;
begin
  if CurStep = ssPostInstall then
  begin
    { A silent install never shows the wizard, so it never reaches the
      passkey page and PasskeyAccepted is still False here. That is on
      purpose: /SILENT is not a way past the gate. }
    if not PasskeyAccepted then
      RaiseException('Installation cannot continue without the passkey.');
    { The fingerprint only, beside the program — never the key itself. The
      app reads its own copy of this and unlocks the first time it runs. }
    HashFile := ExpandConstant('{app}\passkey.hash');
    SaveStringToFile(HashFile, '{#MyPasskeyHash}' + #13#10, False);
  end;
end;
