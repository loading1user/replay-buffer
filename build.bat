@echo off
cd /d "%~dp0"
rem proccap: helper on C# for per-process audio (Discord track). Build: dotnet build proccap -c Release -o proccap\out
if exist proccap\out\proccap.exe xcopy /y /q proccap\out\proccap.* bin\proccap\ >nul
python -m PyInstaller --noconfirm --windowed --name ReplayBuffer --icon icon.ico --add-data "bin\ffmpeg.exe;bin" --add-data "bin\proccap;bin\proccap" --add-data "icon.ico;." --add-data "ui;ui" replay.py
