#define AppVer Trim(FileRead(FileOpen("ui\VERSION")))
[Setup]
AppId={{D4EBA60F-A5AA-4583-876F-2B14AB862C75}
AppName=Replay Buffer
AppVersion={#AppVer}
VersionInfoVersion={#AppVer}.0
DefaultDirName={localappdata}\Programs\ReplayBuffer
DefaultGroupName=Replay Buffer
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UsePreviousAppDir=yes
AppMutex=ReplayBuffer_single_instance
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\ReplayBuffer.exe
SetupIconFile=icon.ico
OutputDir=release
OutputBaseFilename=ReplayBuffer-Setup-{#AppVer}-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "dist\ReplayBuffer\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "bin\MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: NeedsWebView

[Icons]
Name: "{group}\Replay Buffer"; Filename: "{app}\ReplayBuffer.exe"

[Run]
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Microsoft Edge WebView2"; Check: NeedsWebView; Flags: waituntilterminated
Filename: "{app}\ReplayBuffer.exe"; Description: "Replay Buffer"; Flags: postinstall skipifsilent nowait

[Code]
function NeedsWebView: Boolean;
var
  V: String;
  Key: String;
begin
  Key := 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  Result := not ((RegQueryStringValue(HKLM32, Key, 'pv', V) and (V <> '') and (V <> '0.0.0.0')) or
                 (RegQueryStringValue(HKCU, Key, 'pv', V) and (V <> '') and (V <> '0.0.0.0')));
end;
