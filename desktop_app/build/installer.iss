; installer.iss -- Inno Setup script for the TANUH Renal Anonymizer.
;
; Produces ONE downloadable file that behaves the way people expect an
; application to: double-click it, answer the prompt, and afterwards the app
; is in the Start Menu and opens by typing its name.
;
; WHY AN INSTALLER AND NOT JUST THE ZIP
; -------------------------------------
; The zip was not merely inconvenient, it was broken. app.spec builds
; --onedir, so the app is an exe beside a 160-file `_internal` folder.
; Downloading that as a zip gives every extracted file a Mark-of-the-Web
; alternate data stream (Zone.Identifier, ZoneId=3), and .NET refuses to
; load a managed assembly that carries one -- which is how pywebview reaches
; WebView2. The app died on launch with Python.Runtime.Loader.Initialize
; until each file was unblocked by hand.
;
; Inno writes its payload out of its own compressed archive rather than
; letting Explorer extract it, so installed files carry no Zone.Identifier
; at all. Only the downloaded setup exe does, and that mark is consumed
; when it runs. The class of failure disappears rather than being papered
; over with instructions.
;
; WHAT THIS DOES NOT FIX
; ----------------------
; Code signing. This installer is unsigned, so SmartScreen still shows
; "Windows protected your PC" on first download and the user must click
; More info -> Run anyway. Nothing short of an Authenticode certificate
; changes that -- see IT_SECURITY_REVIEW_BRIEF.md.
;
; Build:  ISCC.exe installer.iss      (run AFTER `pyinstaller app.spec`)
; Output: Output\TANUH-Renal-Anonymizer-Setup-<version>.exe

#define AppName          "TANUH Renal Anonymizer"

; Overridable from the command line (ISCC /DAppVersion=1.2.3) so a release
; tag drives the version shown in Add/Remove Programs and in the installer
; filename, without this file having to be edited for every release. Must
; stay numeric dotted form -- Windows parses VersionInfoVersion.
#ifndef AppVersion
  #define AppVersion     "1.0.0"
#endif

#define AppPublisher     "TANUH - The AI CoE in Healthcare"
#define AppURL           "https://github.com/EswarMachara/NIMS_Anonymizer"
#define AppExeName       "TANUH-Renal-Anonymizer.exe"
#define SourceDir        "dist\TANUH-Renal-Anonymizer"

; Refuse to package a build that is missing the application itself. Inno
; happily compiles a perfectly valid installer out of whatever is in
; SourceDir, so without this check an incomplete dist\ silently produces an
; installer that installs 160 support files and no program. That is not
; hypothetical: an antivirus product quarantined the exe out of dist\
; between the PyInstaller build and this step, and the only visible symptom
; was an installer ~8 MB smaller than the last one.
#if !FileExists(SourceDir + "\" + AppExeName)
  #error dist\TANUH-Renal-Anonymizer\TANUH-Renal-Anonymizer.exe is missing. Run `pyinstaller app.spec` first -- or check whether antivirus quarantined it after the build.
#endif

[Setup]
; AppId is how Windows recognises one install as an upgrade of another, and
; how Add/Remove Programs finds it. It must NEVER change between releases --
; changing it turns every future update into a second, parallel install.
AppId={{7C4E2A11-9D63-4F5A-B8E7-2A6F1C0D9B34}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
VersionInfoVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}/releases

; Per-user by default, and NO administrator rights needed for it. This is a
; hospital: clinical staff routinely do not have local admin, and an
; installer that demands it turns "download and run" into an IT ticket.
; An admin running this still gets the choice, because
; PrivilegesRequiredOverridesAllowed shows the all-users/just-me dialog --
; picking all users is what raises the familiar UAC prompt and installs into
; Program Files for everyone on the machine.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

; {autopf} follows whichever of those two was chosen: Program Files for an
; all-users install, %LOCALAPPDATA%\Programs for a per-user one.
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes

OutputDir=Output
OutputBaseFilename=TANUH-Renal-Anonymizer-Setup-{#AppVersion}
SetupIconFile=app_icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
WizardStyle=modern

; ~108 MB of payload, most of it numpy/PyMuPDF/pydicom binaries. lzma2/max
; with solid compression takes it to roughly a third of that, which matters
; on a hospital connection.
Compression=lzma2/max
SolidCompression=yes

; PyInstaller builds a 64-bit app here, so install as one: without this
; {autopf} would resolve to the 32-bit Program Files (x86) on 64-bit Windows.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; Upgrading while the app is open would otherwise fail on locked files.
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
; recursesubdirs/createallsubdirs is not optional: this is a --onedir build
; and the exe cannot start without its sibling _internal\ tree intact.
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; The Start Menu entry is the whole point -- it is what makes the app
; findable by typing its name, rather than by remembering a folder path.
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Open {#AppName} now"; Flags: nowait postinstall skipifsilent

[Code]

// WebView2 runtime check.
//
// pywebview renders this app's whole interface through Microsoft Edge
// WebView2. Windows 11 ships it; a Windows 10 machine that has never had
// Edge updated may not have it, and without it the app installs perfectly
// and then does nothing visible when opened -- the worst possible failure
// for a clinical user, because there is nothing to report.
//
// Checked here so it is said once, plainly, at install time. Deliberately
// NOT a hard stop: a machine can legitimately have the runtime present in a
// form this lookup misses, and refusing to install over a registry read
// would be worse than a warning.
//

const
  WebView2ClientKey = 'Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WebView2Download  = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703';

function VersionPresent(RootKey: Integer; SubKey: String): Boolean;
var
  Version: String;
begin
  Result := RegQueryStringValue(RootKey, SubKey, 'pv', Version)
            and (Version <> '') and (Version <> '0.0.0.0');
end;

function WebView2Installed(): Boolean;
begin
  // Machine-wide installs land under HKLM (and under WOW6432Node on 64-bit
  // Windows, which is where the runtime actually registers); a per-user
  // install lands under HKCU. Any one of them is enough.
  Result := VersionPresent(HKLM, 'SOFTWARE\WOW6432Node\' + WebView2ClientKey)
            or VersionPresent(HKLM, 'SOFTWARE\' + WebView2ClientKey)
            or VersionPresent(HKCU, 'SOFTWARE\' + WebView2ClientKey);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ErrorCode: Integer;
begin
  if (CurStep = ssPostInstall) and not WebView2Installed() then
  begin
    if MsgBox('This computer does not appear to have the Microsoft Edge WebView2 Runtime,'
              + ' which ' + '{#AppName}' + ' uses to draw its screen. Without it the app will'
              + ' start but show nothing.' + #13#10#13#10
              + 'Open the Microsoft download page for it now?'
              + ' (It is a free Microsoft component and does not need this installer to be re-run.)',
              mbConfirmation, MB_YESNO) = IDYES then
      ShellExec('open', WebView2Download, '', '', SW_SHOWNORMAL, ewNoWait, ErrorCode);
  end;
end;

// Uninstall notice.
//
// Uninstalling removes the program, never the data: the mapping CSVs, the
// repeat-submission ledgers and the anonymized output all live under
// %LOCALAPPDATA%\TANUH-Renal-Anonymizer, outside {app}, so Inno does not
// touch them. That silence cuts both ways -- someone may assume their
// mappings are gone when they are not, and a mapping CSV is the one file in
// this workflow that still links an Anonymized ID to a real patient. Said
// out loud rather than left to be discovered.
//

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\TANUH-Renal-Anonymizer');
    if DirExists(DataDir) then
      MsgBox('The program has been removed.' + #13#10#13#10
             + 'Your data has NOT been deleted. It is still at:' + #13#10#13#10
             + DataDir + #13#10#13#10
             + 'That folder holds the ID-mapping CSVs, which still link Anonymized IDs to real'
             + ' patient names and CR numbers. Keep it access-restricted, or delete it'
             + ' deliberately if this workstation is being handed on.',
             mbInformation, MB_OK);
  end;
end;
